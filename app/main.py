from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .logging_conf import get_logger, request_id_var
from .redis_client import ping as redis_ping
from .routers import auth, billing, jobs, presence, profile, worker_api
from .services.scheduler import start_scheduler, stop_scheduler

settings = get_settings()
log = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    for problem in _startup_checks():
        log.warning("[startup] %s", problem)
    await start_scheduler()
    log.info("ClipAI backend started (env=%s)", settings.env)
    yield
    await stop_scheduler()


def _startup_checks() -> list[str]:
    problems = []
    if not settings.worker_secret:
        problems.append("WORKER_SECRET is not set.")
    if not settings.supabase_url:
        problems.append("SUPABASE_URL is not set — running with in-memory user defaults only.")
    if not settings.redis_url:
        problems.append("REDIS_URL is not set — queueing/rate-limiting disabled.")
    return problems


def create_app() -> FastAPI:
    app = FastAPI(title="ViralClip AI SaaS", version="3.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("X-Request-Id", str(uuid.uuid4())[:8])
        request_id_var.set(rid)
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' https: data:; "
            "script-src 'self' https://cdn.jsdelivr.net; style-src 'self' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; frame-src https://www.youtube.com"
        )
        return response

    app.include_router(auth.router)
    app.include_router(jobs.router)
    app.include_router(worker_api.router)
    app.include_router(billing.router)
    app.include_router(profile.router)
    app.include_router(presence.router)

    base_dir = Path(__file__).resolve().parent.parent / "frontend"
    if not base_dir.exists():
        base_dir = Path(__file__).resolve().parents[2] / "frontend"
    if (base_dir / "static").exists():
        app.mount("/static", StaticFiles(directory=base_dir / "static"), name="static")

    @app.get("/")
    async def index():
        from fastapi.responses import FileResponse
        index_path = base_dir / "index.html"
        if index_path.exists():
            return FileResponse(index_path)
        return {"status": "ClipAI API — frontend not built in this environment"}

    @app.get("/health")
    async def health():
        return {"status": "ok", "redis": await redis_ping()}

    @app.get("/redeem/{token}")
    async def redeem_redirect(token: str):
        # thin alias so the public-facing link stays short; real logic in profile router
        return RedirectResponse(f"/api/v1/redeem/{token}")

    return app


app = create_app()