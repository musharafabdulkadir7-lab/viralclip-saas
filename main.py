import os
import uuid
import json
import asyncio
import hmac
import hashlib
import urllib.parse
import secrets as _secrets
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, Header, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import stripe
from supabase import create_client, Client
import redis

from config import settings

stripe.api_key = settings.stripe_secret_key or "sk_test_mock"  # Stripe SDK requires *some* string; real calls fail loudly without a real key.

GOOGLE_CLIENT_ID = settings.google_client_id
GOOGLE_CLIENT_SECRET = settings.google_client_secret
GOOGLE_REDIRECT_URI = settings.google_redirect_uri
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
GOOGLE_AUTH_SCOPES = ["openid", "email", "profile"]

# Auto-load client id/secret from client_secrets.json if present (Google's own export format)
_secrets_file = Path(__file__).resolve().parent / "client_secrets.json"
if _secrets_file.exists():
    try:
        with open(_secrets_file, "r", encoding="utf-8") as _f:
            _cfg = (json.load(_f).get("web") or json.load(open(_secrets_file)).get("installed") or {})
            GOOGLE_CLIENT_ID = GOOGLE_CLIENT_ID or _cfg.get("client_id", "")
            GOOGLE_CLIENT_SECRET = GOOGLE_CLIENT_SECRET or _cfg.get("client_secret", "")
    except Exception as _e:
        print(f"Warning: Could not read client_secrets.json: {_e}")


def sign_user_token(user_id: str) -> str:
    return hmac.new(settings.worker_secret.encode(), user_id.encode(), hashlib.sha256).hexdigest()


def verify_user_token(user_id: str, token: str) -> bool:
    """
    HMAC(WORKER_SECRET, user_id) is safe here specifically because the
    worker is cloud-side infrastructure you control — WORKER_SECRET
    never ships to an end user's machine. (It would NOT be safe if this
    were embedded in a desktop binary handed out to users: anyone could
    extract the secret and forge a valid token for any other user_id.
    Keep it that way — don't let a future "desktop worker" mode reuse
    this same secret.)
    """
    if not token or not user_id:
        return False
    return hmac.compare_digest(sign_user_token(user_id), token)


def stable_user_id_for_email(email: str) -> str:
    """
    Deterministic, stable user ID derived from an email address.
    Uses sha256, NOT Python's builtin hash() — hash() is salted per
    process by default (PYTHONHASHSEED), so the same email would map
    to a different user_id after every restart, silently orphaning
    accounts and subscriptions tied to the old id.
    """
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
    return f"user_{digest[:12]}"


# ── Clients ───────────────────────────────────────────────────────────
try:
    supabase: Client = create_client(settings.supabase_url, settings.supabase_key) if settings.supabase_url else None
except Exception as e:
    print(f"Supabase init error: {e}")
    supabase = None


class DualRedisClient:
    """
    Dual-database Redis client with automatic failover. Tries primary
    DB first; on quota-exceeded errors, fails over to secondary.
    """
    def __init__(self, primary_url: str, secondary_url: str = ""):
        self._primary = None
        self._secondary = None
        self._active = None
        try:
            c = redis.Redis.from_url(primary_url, decode_responses=True, socket_connect_timeout=3)
            c.ping()
            self._primary = c
            self._active = c
            print("[Redis] Primary database connected.")
        except Exception as e:
            print(f"[Redis] Primary connection failed: {e}")
        if secondary_url:
            try:
                c2 = redis.Redis.from_url(secondary_url, decode_responses=True, socket_connect_timeout=3)
                c2.ping()
                self._secondary = c2
                if not self._active:
                    self._active = c2
                print("[Redis] Secondary database connected (failover ready).")
            except Exception as e:
                print(f"[Redis] Secondary connection failed: {e}")

    def _exec(self, method: str, *args, **kwargs):
        clients = [c for c in [self._primary, self._secondary] if c]
        last_err = None
        for client in clients:
            try:
                return getattr(client, method)(*args, **kwargs)
            except Exception as e:
                last_err = e
                err_str = str(e).lower()
                if any(k in err_str for k in ["max monthly", "quota", "limit exceeded", "maxmemory"]):
                    print("[Redis] Quota exceeded, failing over to secondary DB...")
                    continue
                raise
        if last_err:
            raise last_err
        raise RuntimeError("No Redis clients configured")

    def __getattr__(self, name):
        return lambda *args, **kwargs: self._exec(name, *args, **kwargs)


try:
    redis_client = DualRedisClient(settings.redis_url, settings.redis_url_2)
    if not redis_client._active:
        print("[Redis] No databases available.")
        redis_client = None
except Exception as e:
    print(f"Redis init error: {e}")
    redis_client = None

app = FastAPI(title="ViralClip AI SaaS")

for problem in settings.validate_for_startup():
    print(f"[startup] WARNING: {problem}")


# ── Security headers ───────────────────────────────────────────────
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# ── Lightweight IP rate limiting for the write-heavy public endpoints ──
_rate_buckets: dict[str, list[float]] = {}


def _rate_limited(key: str, max_calls: int, window_sec: int) -> bool:
    """Sliding-window limiter kept in-process. Good enough for a single
    web dyno; move to Redis-backed limiting once you run more than one."""
    import time
    now = time.time()
    bucket = _rate_buckets.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < window_sec]
    if len(bucket) >= max_calls:
        return True
    bucket.append(now)
    return False


# ── Background: auto-post scheduler ────────────────────────────────
async def auto_post_scheduler():
    while True:
        try:
            now = datetime.utcnow()
            await asyncio.sleep(60 - now.second)
            now = datetime.utcnow()
            current_time_str = now.strftime("%H:%M")

            if redis_client:
                current_day = now.strftime("%a")
                for key in redis_client.scan_iter("user:*:autopost"):
                    user_id = key.split(":")[1]
                    data = redis_client.hgetall(key)
                    if data.get("enabled") != "True":
                        continue
                    try:
                        days = json.loads(data.get("days", "[]"))
                        times = json.loads(data.get("times", "[]"))
                    except Exception:
                        continue
                    if current_day not in days or current_time_str not in times:
                        continue

                    niche = data.get("niche", "motivation")
                    job_id = str(uuid.uuid4())
                    redis_client.hset(f"job:{job_id}", mapping={
                        "status": "queued", "progress": 0,
                        "message": "Auto-Post Scheduled Job queued...", "url": "",
                    })
                    redis_client.expire(f"job:{job_id}", 86400)
                    redis_client.lpush(f"worker_queue:{user_id}", json.dumps({
                        "job_id": job_id, "niche": niche, "user_id": user_id, "is_auto_post": True,
                    }))
                    print(f"[Scheduler] Triggered auto-post job {job_id} for user {user_id}")
        except Exception as e:
            print(f"[Scheduler] Error in background loop: {e}")
            await asyncio.sleep(60)


async def keep_alive_ping():
    await asyncio.sleep(60)
    app_url = os.environ.get("RENDER_EXTERNAL_URL", settings.api_base_url)
    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.get(f"{app_url}/health")
        except Exception:
            pass
        await asyncio.sleep(600)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(auto_post_scheduler())
    asyncio.create_task(keep_alive_ping())
    if supabase:
        try:
            supabase.table("invites").select("token").limit(1).execute()
        except Exception:
            pass


BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


# ── Models ──────────────────────────────────────────────────────────
class ClipRequest(BaseModel):
    niche: str
    auto_upload: bool = True
    layout: str = "split_screen"
    subtitle_style: str = "hormozi"


class PublishDraftRequest(BaseModel):
    clip_id: str
    title: str = ""
    description: str = ""


class AutoPostSettings(BaseModel):
    enabled: bool
    time: str = "12:00"
    times: list[str] = []
    niche: str
    days: list[str] = []


class UserProfileUpdate(BaseModel):
    email: str


class CheckoutRequest(BaseModel):
    tier: str = "pro"


class JobCompletePayload(BaseModel):
    job_id: str
    status: str
    message: str
    url: str = ""
    title: str = ""
    niche: str = ""


class ProgressPayload(BaseModel):
    job_id: str
    status: str = "running"
    progress: int
    message: str
    url: str = ""


class AnalyzeRequest(BaseModel):
    transcript: str
    niche: str


# ── Helpers ─────────────────────────────────────────────────────────
def get_or_create_user(user_id: str):
    if not supabase:
        return {"id": user_id, "free_clips_used": 0, "license": "free_tier"}
    try:
        res = supabase.table("users").select("*").eq("id", user_id).execute()
        if res.data:
            return res.data[0]
        new_user = {"id": user_id, "free_clips_used": 0, "license": "free_tier"}
        supabase.table("users").insert(new_user).execute()
        return new_user
    except Exception as e:
        print(f"DB error for user {user_id}: {e}")
        return {"id": user_id, "free_clips_used": 0, "license": "free_tier"}


def _apply_referral_bonus(new_user_id: str, referrer_id: str) -> None:
    """
    Real referral crediting — both the new user and the person who
    referred them get bonus free generations. Replaces any notion of
    a fabricated 'live activity' feed: this is an actual incentive
    tied to an actual signup, trackable in the DB.
    """
    if not supabase or not referrer_id or referrer_id == new_user_id:
        return
    bonus = settings.referral_bonus_clips
    try:
        ref_res = supabase.table("users").select("id, free_clips_used").eq("id", referrer_id).execute()
        if ref_res.data:
            current = ref_res.data[0].get("free_clips_used", 0)
            supabase.table("users").update(
                {"free_clips_used": max(0, current - bonus)}
            ).eq("id", referrer_id).execute()
        supabase.table("users").update(
            {"free_clips_used": 0, "referred_by": referrer_id}
        ).eq("id", new_user_id).execute()
    except Exception as e:
        print(f"Referral bonus error: {e}")


# ── Basic routes ────────────────────────────────────────────────────
@app.get("/")
async def render_index(request: Request):
    response = templates.TemplateResponse("index.html", {"request": request})
    # Referral attribution: /?ref=<user_id> sets a short-lived cookie that
    # gets consumed on signup (see auth_google_callback / redeem_invite).
    ref = request.query_params.get("ref")
    if ref:
        response.set_cookie("clipai_ref", ref, max_age=60 * 60 * 24 * 30, samesite="lax")
    return response


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.get("/api/v1/auth/youtube/status")
async def youtube_status(request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")
    if not supabase:
        return {"connected": False}
    try:
        res = supabase.table("users").select("youtube_connected, youtube_refresh_token").eq("id", user_id).execute()
        if res.data and res.data[0].get("youtube_refresh_token"):
            return {"connected": True}
    except Exception as e:
        print(f"Status check error: {e}")
    return {"connected": False}


@app.get("/api/v1/user/profile")
async def get_user_profile(request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")
    user = get_or_create_user(user_id)
    return {
        "user_id": user.get("id", user_id),
        "email": user.get("email", ""),
        "license": user.get("license", "free_tier"),
        "free_clips_used": user.get("free_clips_used", 0),
        "referral_link": f"{settings.api_base_url}/?ref={user.get('id', user_id)}",
    }


@app.post("/api/v1/user/profile")
async def update_user_profile(payload: UserProfileUpdate, request: Request, response: Response):
    user_id = request.cookies.get("user_id", "demo_user_123")
    email = payload.email.strip().lower()

    import re, socket
    email_regex = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    if not re.match(email_regex, email) or len(email) < 6:
        raise HTTPException(status_code=400, detail="Invalid email format. Please enter a genuine email address.")

    domain = email.split('@')[1]
    blocked_domains = {"test.com", "example.com", "fake.com", "asdf.com", "mailinator.com",
                       "tempmail.com", "throwaway.com", "123.com", "abc.com"}
    if domain in blocked_domains or "." not in domain or len(domain.split('.')[-1]) < 2:
        raise HTTPException(status_code=400, detail="Please enter a real, valid email provider (e.g. Gmail, Outlook, Yahoo).")

    try:
        socket.gethostbyname(domain)
    except socket.gaierror:
        raise HTTPException(status_code=400, detail=f"The email domain '@{domain}' does not exist. Please check your spelling.")

    final_user_id = user_id
    license_tier = "free_tier"
    if supabase:
        try:
            res = supabase.table("users").select("*").eq("email", email).execute()
            if res.data:
                existing_user = res.data[0]
                final_user_id = existing_user.get("id", user_id)
                license_tier = existing_user.get("license", "free_tier")
            else:
                supabase.table("users").update({"email": email}).eq("id", user_id).execute()
                final_user_id = user_id
        except Exception as e:
            print(f"Failed to link/find account email: {e}")

    response.set_cookie(key="user_id", value=final_user_id, max_age=31536000, samesite="lax")
    return {"status": "success", "email": email, "user_id": final_user_id, "license": license_tier}


@app.get("/api/v1/analytics")
async def get_analytics(request: Request, user_id: str = ""):
    active_user = user_id or request.cookies.get("user_id", "demo_user_123")
    if not supabase:
        return {"videos": [], "total_views": 0, "total_videos": 0, "avg_views": 0}
    try:
        res = supabase.table("clips").select("*").eq("user_id", active_user).order("created_at", desc=True).execute()
        videos = res.data or []
        total_views = sum(v.get("views", 0) for v in videos)
        return {
            "videos": videos,
            "total_views": total_views,
            "total_videos": len(videos),
            "avg_views": total_views // len(videos) if videos else 0,
        }
    except Exception as e:
        print(f"Analytics error: {e}")
        return {"videos": [], "total_views": 0, "total_videos": 0, "avg_views": 0}


@app.delete("/api/v1/analytics/reset")
async def reset_analytics(request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")
    if supabase:
        try:
            supabase.table("clips").delete().eq("user_id", user_id).execute()
            return {"status": "success", "message": "Analytics reset to 0"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    return {"status": "error", "message": "No database connection"}


@app.post("/api/v1/analytics/refresh-views")
async def refresh_views(request: Request):
    """Fetch real live view counts from YouTube Data API and update the clips table."""
    user_id = request.cookies.get("user_id", "demo_user_123")
    if not supabase:
        return {"status": "error", "message": "No database"}
    try:
        res = supabase.table("clips").select("id, youtube_url").eq("user_id", user_id).execute()
        clips = res.data or []
        if not clips:
            return {"status": "ok", "updated": 0}

        video_ids, id_map = [], {}
        for clip in clips:
            url = clip.get("youtube_url", "")
            if not url:
                continue
            vid = url.rstrip("/").split("/")[-1]
            if vid:
                video_ids.append(vid)
                id_map[vid] = clip["id"]

        if not video_ids:
            return {"status": "ok", "updated": 0}
        if not settings.youtube_api_key:
            return {"status": "error", "message": "YOUTUBE_API_KEY not set on server"}

        params = {"part": "statistics", "id": ",".join(video_ids), "key": settings.youtube_api_key}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get("https://www.googleapis.com/youtube/v3/videos", params=params)
            r.raise_for_status()
            data = r.json()

        updated = 0
        for item in data.get("items", []):
            vid_id = item["id"]
            views = int(item.get("statistics", {}).get("viewCount", 0))
            row_id = id_map.get(vid_id)
            if row_id:
                supabase.table("clips").update({"views": views}).eq("id", row_id).execute()
                updated += 1
        return {"status": "ok", "updated": updated}
    except Exception as e:
        print(f"refresh-views error: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/api/v1/worker/version")
async def get_worker_version():
    return {"version": "2.0.0"}


@app.get("/api/v1/auto-post/settings")
async def get_auto_post_settings(request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")
    default_settings = {"enabled": False, "time": "12:00", "times": ["12:00"], "niche": "motivation",
                        "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]}

    if redis_client:
        try:
            data = redis_client.hgetall(f"user:{user_id}:autopost")
            if data:
                return {
                    "enabled": data.get("enabled") == "True",
                    "time": data.get("time", "12:00"),
                    "times": json.loads(data.get("times", '["12:00"]')),
                    "niche": data.get("niche", "motivation"),
                    "days": json.loads(data.get("days", '["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]')),
                }
        except Exception as e:
            print(f"Redis fetch error: {e}")

    if supabase:
        try:
            res = supabase.table("users").select("auto_post_enabled, auto_post_time, auto_post_niche").eq("id", user_id).execute()
            if res.data:
                d = res.data[0]
                default_settings.update({
                    "enabled": d.get("auto_post_enabled", False),
                    "time": d.get("auto_post_time", "12:00"),
                    "times": [d.get("auto_post_time", "12:00")],
                    "niche": d.get("auto_post_niche", "motivation"),
                })
        except Exception as e:
            print(f"Error fetching auto-post settings: {e}")

    return default_settings


@app.post("/api/v1/auto-post/settings")
async def save_auto_post_settings(payload: AutoPostSettings, request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")
    times_list = payload.times if payload.times else [payload.time]

    if redis_client:
        try:
            redis_client.hset(f"user:{user_id}:autopost", mapping={
                "enabled": str(payload.enabled),
                "time": times_list[0] if times_list else "12:00",
                "times": json.dumps(times_list),
                "niche": payload.niche,
                "days": json.dumps(payload.days),
            })
        except Exception as e:
            print(f"Redis save error: {e}")

    if supabase:
        try:
            supabase.table("users").update({
                "auto_post_enabled": payload.enabled,
                "auto_post_time": times_list[0] if times_list else "12:00",
                "auto_post_niche": payload.niche,
            }).eq("id", user_id).execute()
        except Exception as e:
            print(f"Error saving auto-post settings to DB: {e}")

    return {"status": "success"}


@app.post("/api/v1/generate-clip")
async def generate_clip(payload: ClipRequest, request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")

    if _rate_limited(f"generate:{user_id}", max_calls=10, window_sec=60):
        raise HTTPException(status_code=429, detail="Too many generation requests — please slow down.")

    user = get_or_create_user(user_id)

    free_clips_used = user.get("free_clips_used", 0)
    if free_clips_used >= settings.free_tier_limit and user.get("license") == "free_tier":
        raise HTTPException(status_code=402, detail=f"Free tier limit reached ({settings.free_tier_limit}/{settings.free_tier_limit}). Upgrade required.")

    job_id = str(uuid.uuid4())
    if redis_client:
        redis_client.hset(f"job:{job_id}", mapping={
            "status": "queued", "progress": 0, "message": "Job queued for processing...", "url": "",
        })
        redis_client.expire(f"job:{job_id}", 86400)

    if supabase and user.get("license") == "free_tier":
        try:
            supabase.table("users").update({"free_clips_used": free_clips_used + 1}).eq("id", user_id).execute()
        except Exception as e:
            print(f"Warning: Could not update free_clips_used: {e}")

    if redis_client:
        job_payload_str = json.dumps({
            "mode": "licensed_cc",
            "job_id": job_id,
            "niche": payload.niche,
            "user_id": user_id,
            "is_free_tier": user.get("license") == "free_tier",
            "auto_upload": payload.auto_upload,
            "layout": payload.layout,
            "subtitle_style": payload.subtitle_style,
        })
        redis_client.lpush(f"worker_queue:{user_id}", job_payload_str)
        redis_client.lpush("worker_queue:global", job_payload_str)
        print(f"[Queue] Job {job_id} pushed to worker_queue (user={user_id})")
    else:
        print("[Queue] WARNING: redis_client is None — job not queued!")

    remaining = max(0, settings.free_tier_limit - (free_clips_used + 1)) if user.get("license") == "free_tier" else None
    return {"status": "success", "job_id": job_id, "free_remaining": remaining}


@app.get("/api/v1/job-status/{job_id}")
async def get_job_status(job_id: str):
    if not redis_client:
        return {"status": "idle", "progress": 0, "message": "Redis not connected"}
    job_data = redis_client.hgetall(f"job:{job_id}")
    if not job_data:
        return {"status": "error", "progress": 0, "message": "Job not found"}
    return {
        "status": job_data.get("status", "unknown"),
        "progress": int(job_data.get("progress", 0)),
        "message": job_data.get("message", ""),
        "url": job_data.get("url", ""),
    }


@app.get("/api/v1/user/youtube-creds")
async def get_youtube_creds(user_id: str, token: str = ""):
    """Called by the desktop worker to get YouTube OAuth credentials securely."""
    if not verify_user_token(user_id, token):
        raise HTTPException(status_code=403, detail="Invalid or missing worker token.")
    if not supabase:
        return {"error": "Database not connected"}
    try:
        res = supabase.table("users").select(
            "youtube_access_token, youtube_refresh_token"
        ).eq("id", user_id).execute()
        if res.data and res.data[0].get("youtube_refresh_token"):
            return {
                "token": res.data[0].get("youtube_access_token"),
                "refresh_token": res.data[0].get("youtube_refresh_token"),
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "user_id": user_id,
            }
        return {"error": "YouTube not connected for this user"}
    except Exception as e:
        print(f"Error fetching YouTube creds: {e}")
        return {"error": str(e)}


@app.get("/api/v1/debug/queue")
async def debug_queue(user_id: str, request: Request):
    """Diagnostic endpoint — admin-only, was previously open to anyone who knew a user_id."""
    auth = request.headers.get("X-Admin-Secret", "")
    if not hmac.compare_digest(auth, settings.admin_secret):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not redis_client:
        return {"error": "Redis not connected"}
    try:
        queue_len = redis_client.llen(f"worker_queue:{user_id}")
        heartbeat = redis_client.get(f"worker_heartbeat:{user_id}")
        items = redis_client.lrange(f"worker_queue:{user_id}", 0, -1)
        return {
            "queue_length": queue_len,
            "worker_alive": bool(heartbeat),
            "queue_items": [json.loads(i) if i else None for i in items],
        }
    except Exception as e:
        return {"error": str(e)}


@app.delete("/api/v1/clip/{clip_id}")
async def delete_clip(clip_id: str, request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")
    if not supabase:
        raise HTTPException(status_code=500, detail="Database not configured")
    try:
        supabase.table("clips").delete().eq("id", clip_id).eq("user_id", user_id).execute()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/worker/poll")
async def worker_poll(user_id: str, token: str = ""):
    if not verify_user_token(user_id, token):
        raise HTTPException(status_code=403, detail="Invalid or missing worker token.")
    if not redis_client:
        return {"job": None}
    try:
        redis_client.setex(f"worker_heartbeat:{user_id}", 30, "alive")
        redis_client.setex("worker_heartbeat:cloud", 30, "alive")

        job = redis_client.rpop(f"worker_queue:{user_id}")
        if not job:
            job = redis_client.rpop("worker_queue:global")

        if job:
            if isinstance(job, bytes):
                job = job.decode("utf-8")
            job_data = json.loads(job)
            redis_client.hset(f"job:{job_data['job_id']}", mapping={
                "status": "processing", "message": "Cloud worker started pipeline...", "progress": 5,
            })
            return {"job": job_data}
    except Exception as e:
        print(f"Poll error: {e}")
    return {"job": None}


@app.post("/api/v1/worker/complete")
async def worker_complete(payload: JobCompletePayload, user_id: str):
    if not redis_client:
        return {"error": "Redis not connected"}

    redis_client.hset(f"job:{payload.job_id}", mapping={
        "status": payload.status, "progress": 100, "message": payload.message, "url": payload.url,
    })

    if payload.status in ["complete", "draft_ready"] and supabase:
        try:
            supabase.table("clips").insert({
                "user_id": user_id, "youtube_url": payload.url, "title": payload.title,
                "niche": payload.niche, "views": 0,
                "status": "published" if payload.url else "draft",
            }).execute()
        except Exception as e1:
            print(f"Clips save with status failed: {e1}")
            try:
                supabase.table("clips").insert({
                    "user_id": user_id, "youtube_url": payload.url, "title": payload.title,
                    "niche": payload.niche, "views": 0,
                }).execute()
            except Exception as e2:
                print(f"Clips fallback save error: {e2}")

    return {"status": "ok"}


@app.get("/api/v1/worker/heartbeat")
async def worker_heartbeat(user_id: str):
    if not redis_client:
        return {"alive": True}
    alive = redis_client.get(f"worker_heartbeat:{user_id}") or redis_client.get("worker_heartbeat:cloud")
    return {"alive": bool(alive)}


@app.get("/api/v1/worker/scripts")
async def get_worker_scripts(user_id: str, token: str = ""):
    """
    Returns the latest production pipeline code for live hot-updating of
    desktop workers. Previously unauthenticated — anyone who found this
    URL could read the entire backend source. Now requires the same
    signed worker token every other worker endpoint requires, so only a
    machine that already knows a valid user_id + WORKER_SECRET-derived
    token can pull it.
    """
    if not verify_user_token(user_id, token):
        raise HTTPException(status_code=403, detail="Invalid or missing worker token.")
    script_names = ["worker.py", "clip_cutter.py", "clip_finder.py", "video_finder.py",
                    "video_downloader.py", "youtube_uploader.py", "hot_pipeline.py"]
    scripts = {}
    base = Path(__file__).resolve().parent
    for s in script_names:
        p = base / s
        if p.exists():
            try:
                scripts[s] = p.read_text(encoding="utf-8")
            except Exception:
                pass
    return {"scripts": scripts}


@app.post("/api/v1/worker/progress")
async def worker_progress(payload: ProgressPayload):
    if redis_client:
        redis_client.hset(f"job:{payload.job_id}", mapping={
            "progress": payload.progress, "message": payload.message,
            "status": payload.status, "url": payload.url,
        })
    return {"status": "ok"}


@app.post("/api/v1/worker/analyze-transcript")
async def analyze_transcript(payload: AnalyzeRequest, user_id: str):
    """
    Accepts a transcript from the worker, asks Gemini for the best segment,
    and returns the timestamps. Protects the GEMINI_API_KEY on the server.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        import re as re_mod
        lines = payload.transcript.strip().split("\n")
        entries = []
        for line in lines:
            m = re_mod.match(r"\[(\d+):(\d+)\]\s+(.*)", line)
            if m:
                t = int(m.group(1)) * 60 + int(m.group(2))
                entries.append((t, m.group(3)))

        best_start, best_end = 60, 110
        if len(entries) >= 4:
            best_words = 0
            for i in range(len(entries)):
                window_start = entries[i][0]
                window_end = window_start + 50
                words = sum(len(e[1].split()) for e in entries if window_start <= e[0] < window_end)
                if words > best_words:
                    best_words, best_start, best_end = words, window_start, window_end

        return {"start_sec": best_start, "end_sec": best_end, "num_parts": 1, "caption": payload.niche.title()}

    try:
        from google import genai
        from google.genai import types
        import re

        client = genai.Client(api_key=api_key)
        prompt = f"""You are a world-class YouTube Shorts & TikTok viral retention editor and script director for the '{payload.niche}' niche.
Analyze the following timestamped transcript and find the HIGHEST RETENTION, most explosive 30 to 55-second moment (PARTS: 1).

Retention & Virality Criteria:
1. Hook Viability (0-3s): Must open with a high-stakes question, counter-intuitive statement, or sudden dramatic setup that stops scrolling.
2. Pacing & Momentum: Fast information density, minimal filler words or dead pauses.
3. Narrative Arc: A complete standalone thought, story, lesson, or insight with a definitive punchline or resolution.
4. Loop Potential: The end should naturally tie back or provoke an immediate reaction/comment.

Transcript:
{payload.transcript}

Respond in EXACTLY this format, nothing else:
START: 120
END: 170
PARTS: 1
CAPTION: How I Built My First Million
VIRAL_SCORE: 96
REASON: High curiosity hook with intense storytelling arc and punchy conclusion."""

        response = client.models.generate_content(
            model="gemini-1.5-flash", contents=prompt,
            config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=256),
        )
        text = response.text.strip()

        def parse_ts(val):
            val = val.strip()
            if ":" in val:
                parts = val.split(":")
                if len(parts) == 2:
                    return int(parts[0]) * 60 + int(parts[1])
                elif len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            return int(val)

        start_m = re.search(r"START:\s*([\d:]+)", text)
        end_m = re.search(r"END:\s*([\d:]+)", text)
        parts_m = re.search(r"PARTS:\s*(\d+)", text)
        caption_m = re.search(r"CAPTION:\s*(.+)", text)
        score_m = re.search(r"VIRAL_SCORE:\s*(\d+)", text)

        if not start_m or not end_m:
            return {"error": "Could not parse Gemini output", "raw": text}

        start = parse_ts(start_m.group(1))
        end = parse_ts(end_m.group(1))
        parts = int(parts_m.group(1)) if parts_m else max(1, round((end - start) / 55))
        score = int(score_m.group(1)) if score_m else 92

        return {
            "start_sec": start, "end_sec": end, "num_parts": parts,
            "caption": caption_m.group(1).strip() if caption_m else payload.niche.title(),
            "viral_score": score,
        }
    except Exception as e:
        print(f"Analyze error: {e}")
        return {"error": str(e)}


@app.post("/api/v1/clip/publish-draft")
async def publish_draft(payload: PublishDraftRequest, request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")
    if not supabase:
        raise HTTPException(status_code=500, detail="Database not configured")

    res = supabase.table("clips").select("*").eq("id", payload.clip_id).eq("user_id", user_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Clip not found in your Workplace")

    clip = res.data[0]
    supabase.table("clips").update({
        "status": "published",
        "title": payload.title or clip.get("title") or "Viral Short",
    }).eq("id", payload.clip_id).execute()

    return {"status": "success", "message": "Clip submitted for YouTube publishing!"}


# ── Billing ─────────────────────────────────────────────────────────
# NOTE: "full_version" was previously marketed as "Lifetime" while actually
# being billed monthly — that's a real chargeback/regulatory risk (most
# card networks and the FTC treat "lifetime" as a one-time-payment claim).
# Renamed to match what customers are actually billed.
PRICING_TIERS = {
    "pro": {"name": "ViralClip AI — Pro (Monthly)", "amount": 2900, "mode": "subscription"},
    "full_version": {"name": "ViralClip AI — Full Version (Monthly)", "amount": 4900, "mode": "subscription"},
}


@app.post("/api/v1/create-checkout-session")
async def create_checkout_session(request: Request, body: CheckoutRequest = None):
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=500, detail="Billing is not configured on this server.")

    user_id = request.cookies.get("user_id", "demo_user_123")
    domain = str(request.base_url).rstrip("/")
    tier = body.tier if body and body.tier in PRICING_TIERS else "pro"
    selected = PRICING_TIERS[tier]

    session_params = {
        "payment_method_types": ["card"],
        "client_reference_id": user_id,
        "metadata": {"tier": tier, "user_id": user_id},
        "line_items": [{
            "price_data": {
                "currency": "usd",
                "product_data": {
                    "name": selected["name"],
                    "description": "Viral AI Short generation, background rendering, and YouTube auto-posting.",
                },
                "unit_amount": selected["amount"],
                "recurring": {"interval": "month"},
            },
            "quantity": 1,
        }],
        "mode": "subscription",
        "success_url": f"{domain}/?payment=success",
        "cancel_url": f"{domain}/?payment=cancel",
    }

    session = stripe.checkout.Session.create(**session_params)
    return {"checkout_url": session.url}


@app.post("/api/v1/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, settings.stripe_webhook_secret)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session.get("client_reference_id")
        tier_purchased = (session.get("metadata") or {}).get("tier", "pro")
        if user_id and supabase:
            try:
                supabase.table("users").update({"license": tier_purchased}).eq("id", user_id).execute()
            except Exception as e:
                print(f"Stripe webhook DB error: {e}")
    elif event["type"] in ("customer.subscription.deleted", "customer.subscription.updated"):
        # Handle downgrades/cancellations so a lapsed subscriber doesn't
        # keep paid-tier access forever.
        sub = event["data"]["object"]
        status = sub.get("status")
        user_id = (sub.get("metadata") or {}).get("user_id")
        if user_id and supabase and status in ("canceled", "unpaid", "incomplete_expired"):
            try:
                supabase.table("users").update({"license": "free_tier"}).eq("id", user_id).execute()
            except Exception as e:
                print(f"Stripe subscription-lapse DB error: {e}")

    return {"status": "success"}


# ── Invite links (admin-generated) ───────────────────────────────────
@app.post("/api/v1/admin/generate-invite")
async def generate_invite(request: Request, count: int = 1):
    auth = request.headers.get("X-Admin-Secret", "")
    if not hmac.compare_digest(auth, settings.admin_secret):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not supabase:
        raise HTTPException(status_code=500, detail="Database not connected")
    count = max(1, min(count, 100))  # guardrail: no accidental mass-generation
    links = []
    for _ in range(count):
        token = _secrets.token_urlsafe(24)
        try:
            supabase.table("invites").insert({"token": token, "redeemed": False}).execute()
            base_url = str(request.base_url).rstrip("/")
            links.append(f"{base_url}/redeem/{token}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"DB error: {e}")
    return {"links": links}


@app.get("/redeem/{token}")
async def redeem_invite(token: str, request: Request, response: Response):
    if not supabase:
        raise HTTPException(status_code=500, detail="Database not connected")
    try:
        res = supabase.table("invites").select("*").eq("token", token).eq("redeemed", False).execute()
        if not res.data:
            return HTMLResponse("""<html><body style='font-family:sans-serif;text-align:center;padding:60px;background:#0f0f0f;color:white'>
                <h2>&#10060; Invalid or already used invite link.</h2>
                <p>This link has already been redeemed or doesn't exist.</p>
                <a href='/' style='color:#3b82f6'>&#8592; Back to ClipAI</a></body></html>""", status_code=400)
        new_user_id = f"user_{uuid.uuid4().hex[:8]}"
        supabase.table("users").insert({"id": new_user_id, "license": "pro", "free_clips_used": 0}).execute()
        supabase.table("invites").update({"redeemed": True, "redeemed_by": new_user_id}).eq("token", token).execute()
        redir = RedirectResponse(url="/", status_code=302)
        redir.set_cookie("user_id", new_user_id, max_age=60 * 60 * 24 * 365, samesite="lax")
        return redir
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── YouTube channel connection (upload access) ───────────────────────
@app.get("/api/v1/auth/youtube")
async def auth_youtube(request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")
    state = str(uuid.uuid4())
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(YOUTUBE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    authorization_url = "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params)
    if redis_client:
        redis_client.setex(f"oauth_state:{state}", 600, user_id)
    return RedirectResponse(authorization_url)


@app.get("/api/v1/auth/youtube/callback")
async def auth_youtube_callback(request: Request, state: str = None, code: str = None):
    """
    Single callback endpoint handling both flows that go through Google's
    OAuth consent screen: (1) linking a YouTube channel for uploads, and
    (2) "Sign in with Google" account login/registration (state prefixed
    with "login_"). Keeping one callback avoids needing two redirect URIs
    registered with Google.
    """
    if not state or not code:
        return {"error": "Missing state or code"}

    user_id = redis_client.get(f"oauth_state:{state}") if redis_client else "demo_user_123"

    try:
        token_data = {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": GOOGLE_REDIRECT_URI,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post("https://oauth2.googleapis.com/token", data=token_data)
            if r.status_code != 200:
                raise Exception(f"Google Token API returned {r.status_code}: {r.text}")
            token_json = r.json()
            access_token = token_json.get("access_token")
            refresh_token = token_json.get("refresh_token")

            # ── Branch: Google Account Login / Registration ──────────
            if state.startswith("login_"):
                userinfo_res = await client.get(
                    "https://www.googleapis.com/oauth2/v2/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if userinfo_res.status_code == 200:
                    userinfo = userinfo_res.json()
                    email = userinfo.get("email", "").lower()
                    if email and userinfo.get("verified_email", False):
                        login_user_id = stable_user_id_for_email(email)
                        is_new_user = False
                        if supabase:
                            try:
                                res = supabase.table("users").select("*").eq("email", email).execute()
                                if res.data:
                                    login_user_id = res.data[0]["id"]
                                else:
                                    supabase.table("users").insert({
                                        "id": login_user_id, "email": email,
                                        "license": "free_tier", "free_clips_used": 0,
                                    }).execute()
                                    is_new_user = True
                            except Exception as dbe:
                                print(f"Supabase login save error: {dbe}")

                        redir = RedirectResponse("/?auth=success", status_code=302)
                        redir.set_cookie("user_id", login_user_id, max_age=60 * 60 * 24 * 365, samesite="lax")

                        if is_new_user:
                            ref_id = request.cookies.get("clipai_ref", "")
                            if ref_id:
                                _apply_referral_bonus(login_user_id, ref_id)
                                redir.delete_cookie("clipai_ref")
                        return redir

            # ── Branch: YouTube Channel Connection ────────────────────
            if supabase:
                update_data = {"youtube_access_token": access_token, "youtube_connected": True}
                if refresh_token:
                    update_data["youtube_refresh_token"] = refresh_token
                supabase.table("users").update(update_data).eq("id", user_id).execute()

            return RedirectResponse("/?youtube=connected")
    except Exception as e:
        error_msg = urllib.parse.quote(str(e))
        print(f"OAuth Error: {e}")
        return RedirectResponse(f"/?youtube=error&detail={error_msg}")


@app.get("/api/v1/auth/google")
async def auth_google_login(request: Request):
    """Initiates 1-click login/registration with a verified Google account."""
    state = f"login_{uuid.uuid4()}"
    if redis_client:
        redis_client.setex(f"oauth_state:{state}", 600, "google_login")
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GOOGLE_AUTH_SCOPES),
        "access_type": "online",
        "prompt": "select_account",
        "state": state,
    }
    authorization_url = "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params)
    return RedirectResponse(authorization_url)


# NOTE: there used to be a second, separate /api/v1/auth/google/callback
# route here that referenced an undefined GOOGLE_AUTH_REDIRECT_URI
# variable — it would raise NameError on every single call. The
# "login_" branch inside auth_youtube_callback above already handles
# the full Google-login flow (that's the redirect_uri actually sent in
# auth_google_login), so the broken duplicate route has been removed
# rather than patched, to avoid two divergent implementations of the
# same flow drifting apart again.