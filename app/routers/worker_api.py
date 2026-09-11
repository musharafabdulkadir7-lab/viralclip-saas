from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..config import get_settings
from ..db import ClipRepo, UserRepo
from ..logging_conf import get_logger
from ..schemas import AnalyzeRequest, JobCompletePayload, ProgressPayload
from ..security import verify_worker_token
from ..services import job_queue

router = APIRouter(prefix="/api/v1/worker", tags=["worker"])
log = get_logger("worker_api")
settings = get_settings()


def _auth(user_id: str, token: str, purpose: str = "poll") -> None:
    if not verify_worker_token(user_id, token, purpose=purpose):
        raise HTTPException(status_code=403, detail="Invalid or missing worker token.")


@router.get("/poll")
async def worker_poll(user_id: str, token: str = "", consumer: str = "worker-1"):
    _auth(user_id, token)
    job = await job_queue.claim_next(consumer_name=consumer)
    if job:
        await job_queue.set_status(job.job_id, "processing", 5, "Cloud worker started pipeline...")
        return {"job": job.payload, "stream_id": job.stream_id}
    return {"job": None}


@router.post("/ack/{stream_id}")
async def worker_ack(stream_id: str, job_id: str, user_id: str, token: str = ""):
    _auth(user_id, token, purpose="ack")
    from ..services.job_queue import QueuedJob, ack
    await ack(QueuedJob(job_id=job_id, payload={}, stream_id=stream_id))
    return {"status": "ok"}


@router.post("/complete")
async def worker_complete(payload: JobCompletePayload, user_id: str):
    await job_queue.set_status(payload.job_id, payload.status, 100, payload.message, payload.url)
    if payload.status in ("complete", "draft_ready"):
        ClipRepo.insert({
            "user_id": user_id, "youtube_url": payload.url, "title": payload.title,
            "niche": payload.niche, "views": 0,
            "status": "published" if payload.status == "complete" else "draft",
        })
    return {"status": "ok"}


@router.post("/progress")
async def worker_progress(payload: ProgressPayload):
    await job_queue.set_status(payload.job_id, payload.status, payload.progress, payload.message, payload.url)
    return {"status": "ok"}


@router.get("/youtube-creds")
async def get_youtube_creds(user_id: str, token: str = ""):
    _auth(user_id, token, purpose="creds")
    user = UserRepo.get_or_create(user_id)
    if not user.get("youtube_refresh_token"):
        return {"error": "YouTube not connected for this user"}
    return {
        "token": user.get("youtube_access_token"),
        "refresh_token": user.get("youtube_refresh_token"),
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "user_id": user_id,
    }


@router.post("/analyze-transcript")
async def analyze_transcript(payload: AnalyzeRequest, user_id: str):
    """Runs the viral-moment selection. Kept server-side so GEMINI_API_KEY
    never has to live on worker infra. Falls back to a pure-python
    keyword-density heuristic if no key is configured — same fallback
    idea as v2, refactored into services/clip_analysis.py for testability."""
    from ..services.clip_analysis import analyze
    return await analyze(payload.transcript, payload.niche)


@router.get("/scripts")
async def get_worker_scripts(user_id: str, token: str = ""):
    _auth(user_id, token, purpose="scripts")
    import pathlib
    base = pathlib.Path(__file__).resolve().parents[3] / "worker" / "pipeline"
    scripts = {}
    for p in base.glob("*.py"):
        try:
            scripts[p.name] = p.read_text(encoding="utf-8")
        except Exception:
            pass
    return {"scripts": scripts}


@router.get("/heartbeat")
async def worker_heartbeat(user_id: str):
    from ..redis_client import get_redis
    r = get_redis()
    if r is None:
        return {"alive": True}
    alive = await r.get(f"worker_heartbeat:{user_id}") or await r.get("worker_heartbeat:cloud")
    return {"alive": bool(alive)}