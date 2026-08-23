import os
import uuid
import json
import asyncio
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import stripe
from supabase import create_client, Client
import redis

# Configuration & Environment Variables
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "sk_test_mock")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "whsec_mock")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://your-supabase-url.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "your-supabase-service-key")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Google OAuth Config
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "https://viralclip-saas.onrender.com/api/v1/auth/youtube/callback")
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
stripe.api_key = STRIPE_SECRET_KEY

# Clients
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"Supabase init error: {e}")
    supabase = None

try:
    redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()
except Exception as e:
    print(f"Redis init error: {e}")
    redis_client = None

app = FastAPI(title="ViralClip AI SaaS")

# Auto-Post Background Task
async def auto_post_scheduler():
    while True:
        try:
            # Check every minute, sleeping exactly to the start of the next minute
            now = datetime.utcnow()
            sleep_time = 60 - now.second
            await asyncio.sleep(sleep_time)
            
            now = datetime.utcnow()
            current_time_str = now.strftime("%H:%M")
            print(f"[Scheduler] Checking auto-post schedules for time {current_time_str} UTC")
            
            if redis_client:
                current_day = now.strftime("%a") # e.g. "Mon"
                
                # Use SCAN to find all autopost settings
                for key in redis_client.scan_iter("user:*:autopost"):
                    user_id = key.split(":")[1]
                    data = redis_client.hgetall(key)
                    
                    if data.get("enabled") != "True":
                        continue
                        
                    try:
                        days = json.loads(data.get("days", '[]'))
                        times = json.loads(data.get("times", '[]'))
                    except:
                        continue
                        
                    if current_day not in days:
                        continue
                        
                    if current_time_str not in times:
                        continue
                        
                    niche = data.get("niche", "motivation")
                    
                    # Generate a job
                    job_id = str(uuid.uuid4())
                    redis_client.hset(f"job:{job_id}", mapping={
                        "status": "queued",
                        "progress": 0,
                        "message": "Auto-Post Scheduled Job queued...",
                        "url": ""
                    })
                    redis_client.expire(f"job:{job_id}", 86400)
                    
                    # Push to worker
                    redis_client.lpush(f"worker_queue:{user_id}", json.dumps({
                        "job_id": job_id,
                        "niche": niche,
                        "user_id": user_id,
                        "is_auto_post": True
                    }))
                    print(f"[Scheduler] Triggered auto-post job {job_id} for user {user_id}")
                    
        except Exception as e:
            print(f"[Scheduler] Error in background loop: {e}")
            await asyncio.sleep(60)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(auto_post_scheduler())

# Static files and templates
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Schemas
class ClipRequest(BaseModel):
    niche: str

class AutoPostSettings(BaseModel):
    enabled: bool
    time: str = "12:00"
    times: list[str] = []
    niche: str
    days: list[str] = []

# Helper functions
def get_or_create_user(user_id: str):
    if not supabase:
        return {"id": user_id, "free_clip_used": False, "license": "free_tier"}
    try:
        res = supabase.table("users").select("*").eq("id", user_id).execute()
        if res.data:
            return res.data[0]
        new_user = {"id": user_id, "free_clip_used": False, "license": "free_tier"}
        supabase.table("users").insert(new_user).execute()
        return new_user
    except Exception as e:
        print(f"DB error for user {user_id}: {e}")
        return {"id": user_id, "free_clip_used": False, "license": "free_tier"}

@app.get("/")
async def render_index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

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

@app.get("/api/v1/analytics")
async def get_analytics(request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")
    if not supabase:
        return {"videos": [], "total_views": 0, "total_videos": 0, "avg_views": 0}
    try:
        res = supabase.table("clips").select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
        videos = res.data or []
        total_views = sum(v.get("views", 0) for v in videos)
        return {
            "videos": videos,
            "total_views": total_views,
            "total_videos": len(videos),
            "avg_views": total_views // len(videos) if videos else 0
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

@app.get("/api/v1/auto-post/settings")
async def get_auto_post_settings(request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")
    default_settings = {"enabled": False, "time": "12:00", "times": ["12:00"], "niche": "motivation", "days": ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]}
    
    if redis_client:
        try:
            data = redis_client.hgetall(f"user:{user_id}:autopost")
            if data:
                return {
                    "enabled": data.get("enabled") == "True",
                    "time": data.get("time", "12:00"),
                    "times": json.loads(data.get("times", '["12:00"]')),
                    "niche": data.get("niche", "motivation"),
                    "days": json.loads(data.get("days", '["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]'))
                }
        except Exception as e:
            print(f"Redis fetch error: {e}")
            
    # Fallback to Supabase if Redis is empty
    if supabase:
        try:
            res = supabase.table("users").select("auto_post_enabled, auto_post_time, auto_post_niche").eq("id", user_id).execute()
            if res.data:
                d = res.data[0]
                default_settings.update({
                    "enabled": d.get("auto_post_enabled", False),
                    "time": d.get("auto_post_time", "12:00"),
                    "times": [d.get("auto_post_time", "12:00")],
                    "niche": d.get("auto_post_niche", "motivation")
                })
        except Exception as e:
            print(f"Error fetching auto-post settings: {e}")
            
    return default_settings

@app.post("/api/v1/auto-post/settings")
async def save_auto_post_settings(settings: AutoPostSettings, request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")
    
    times_list = settings.times if settings.times else [settings.time]
    
    if redis_client:
        try:
            redis_client.hset(f"user:{user_id}:autopost", mapping={
                "enabled": str(settings.enabled),
                "time": times_list[0] if times_list else "12:00",
                "times": json.dumps(times_list),
                "niche": settings.niche,
                "days": json.dumps(settings.days)
            })
        except Exception as e:
            print(f"Redis save error: {e}")
            
    if supabase:
        try:
            supabase.table("users").update({
                "auto_post_enabled": settings.enabled,
                "auto_post_time": times_list[0] if times_list else "12:00",
                "auto_post_niche": settings.niche
            }).eq("id", user_id).execute()
        except Exception as e:
            print(f"Error saving auto-post settings to DB: {e}")
            
    return {"status": "success"}

@app.post("/api/v1/generate-clip")
async def generate_clip(payload: ClipRequest, request: Request):
    # Retrieve user ID (mock session ID for demonstration)
    user_id = request.cookies.get("user_id", "demo_user_123")
    user = get_or_create_user(user_id)

    # Enforce paywall on free tier
    if user.get("free_clip_used") and user.get("license") == "free_tier":
        raise HTTPException(status_code=402, detail="Free trial clip used. Upgrade required.")

    # Create job ID and store status in Redis
    import threading
    job_id = str(uuid.uuid4())
    if redis_client:
        redis_client.hset(f"job:{job_id}", mapping={
            "status": "queued",
            "progress": 0,
            "message": "Job queued for processing...",
            "url": ""
        })
        redis_client.expire(f"job:{job_id}", 86400)

    # Mark free clip as used if on free tier (non-blocking)
    if supabase and user.get("license") == "free_tier":
        try:
            supabase.table("users").update({"free_clip_used": True}).eq("id", user_id).execute()
        except Exception as e:
            print(f"Warning: Could not update free_clip_used: {e}")

    # Queue the job for the client worker
    if redis_client:
        redis_client.lpush(f"worker_queue:{user_id}", json.dumps({
            "job_id": job_id,
            "niche": payload.niche,
            "user_id": user_id
        }))
        print(f"[Queue] Job {job_id} pushed to worker_queue:{user_id}")
    else:
        print("[Queue] WARNING: redis_client is None — job not queued!")

    return {"status": "success", "job_id": job_id}

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
        "url": job_data.get("url", "")
    }

class JobCompletePayload(BaseModel):
    job_id: str
    status: str
    message: str
    url: str = ""
    title: str = ""
    niche: str = ""

@app.get("/api/v1/user/youtube-creds")
async def get_youtube_creds(user_id: str):
    """Called by the desktop worker to get YouTube OAuth credentials."""
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
                "user_id": user_id
            }
        return {"error": "YouTube not connected for this user"}
    except Exception as e:
        print(f"Error fetching YouTube creds: {e}")
        return {"error": str(e)}

@app.get("/api/v1/debug/queue")
async def debug_queue(user_id: str):
    """Diagnostic endpoint to check Redis queue state."""
    if not redis_client:
        return {"error": "Redis not connected"}
    try:
        queue_len = redis_client.llen(f"worker_queue:{user_id}")
        heartbeat = redis_client.get(f"worker_heartbeat:{user_id}")
        # Peek at the queue without consuming
        items = redis_client.lrange(f"worker_queue:{user_id}", 0, -1)
        return {
            "queue_length": queue_len,
            "worker_alive": bool(heartbeat),
            "queue_items": [json.loads(i) if i else None for i in items]
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/v1/worker/poll")
async def worker_poll(user_id: str):
    if not redis_client:
        return {"job": None}
    
    try:
        # Heartbeat
        redis_client.setex(f"worker_heartbeat:{user_id}", 30, "alive")

        job = redis_client.rpop(f"worker_queue:{user_id}")
        if job:
            # Decode bytes if needed
            if isinstance(job, bytes):
                job = job.decode("utf-8")
            job_data = json.loads(job)
            redis_client.hset(f"job:{job_data['job_id']}", mapping={
                "status": "processing",
                "message": "Local worker started pipeline...",
                "progress": 5
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
        "status": payload.status,
        "progress": 100,
        "message": payload.message,
        "url": payload.url
    })
    
    # Save to supabase if success
    if payload.status == "complete" and supabase:
        try:
            supabase.table("clips").insert({
                "user_id": user_id,
                "youtube_url": payload.url,
                "title": payload.title,
                "niche": payload.niche,
                "views": 0,
            }).execute()
        except Exception as e:
            print(f"Analytics save error: {e}")
            
    return {"status": "ok"}

@app.get("/api/v1/worker/heartbeat")
async def worker_heartbeat(user_id: str):
    if not redis_client:
        return {"alive": False}
    alive = redis_client.get(f"worker_heartbeat:{user_id}")
    return {"alive": bool(alive)}

class ProgressPayload(BaseModel):
    job_id: str
    status: str = "running"
    progress: int
    message: str
    url: str = ""

@app.post("/api/v1/worker/progress")
async def worker_progress(payload: ProgressPayload):
    if redis_client:
        redis_client.hset(f"job:{payload.job_id}", mapping={
            "progress": payload.progress,
            "message": payload.message,
            "status": payload.status,
            "url": payload.url
        })
    return {"status": "ok"}

class AnalyzeRequest(BaseModel):
    transcript: str
    niche: str

@app.post("/api/v1/worker/analyze-transcript")
async def analyze_transcript(payload: AnalyzeRequest, user_id: str):
    """
    Accepts a transcript from the worker, asks Gemini for the best segment,
    and returns the timestamps. This protects the GEMINI_API_KEY on the server.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        # Heuristic fallback: pick the densest part of the transcript (most words per minute)
        # Parse timestamps from transcript lines like "[MM:SS] text"
        import re as re_mod
        lines = payload.transcript.strip().split("\n")
        entries = []
        for line in lines:
            m = re_mod.match(r"\[(\d+):(\d+)\]\s+(.*)", line)
            if m:
                t = int(m.group(1))*60 + int(m.group(2))
                entries.append((t, m.group(3)))
        
        best_start, best_end = 60, 240
        if len(entries) >= 4:
            # Slide a 3-minute window and find max word density
            best_words = 0
            for i in range(len(entries)):
                window_start = entries[i][0]
                window_end = window_start + 180
                words = sum(len(e[1].split()) for e in entries if window_start <= e[0] < window_end)
                if words > best_words:
                    best_words = words
                    best_start = window_start
                    best_end = window_end
        
        return {
            "start_sec": best_start,
            "end_sec": best_end,
            "caption": payload.niche.title()
        }
        
    try:
        from google import genai
        from google.genai import types
        import re
        
        client = genai.Client(api_key=api_key)
        
        prompt = f"""You are an expert YouTube Shorts creator in the '{payload.niche}' niche.

Below is a timestamped transcript from a long-form YouTube video.
Your job is to find the SINGLE most compelling complete segment — a story, life lesson, or argument that has:
- A clear beginning (hook/setup)
- A middle (buildup/details)  
- A natural ending (conclusion/punchline/resolution)

The segment should ideally be 30 to 55 seconds long (1 Part). Very rarely, if a story is too compelling to cut, you can choose a 60-110 second segment (2 Parts) or 120-165s (3 Parts). 90% of the time, find a single part.

Rules:
- Pick where someone is telling a complete story or making a full point — NOT just a random window
- The start should be a natural hook (a question, a surprising claim, or a story setup)
- The end should be a natural resolution (not mid-sentence)

Transcript:
{payload.transcript}

You MUST respond in EXACTLY this format, nothing else, no markdown, no bullet points:
START: 120
END: 170
PARTS: 1
CAPTION: How I Built My First Million
REASON: This segment tells a complete rags-to-riches story with a clear arc."""

        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=256)
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
        end_m   = re.search(r"END:\s*([\d:]+)", text)
        parts_m = re.search(r"PARTS:\s*(\d+)", text)
        caption_m = re.search(r"CAPTION:\s*(.+)", text)

        if not start_m or not end_m:
            return {"error": "Could not parse Gemini output", "raw": text}

        start = parse_ts(start_m.group(1))
        end   = parse_ts(end_m.group(1))
        parts = int(parts_m.group(1)) if parts_m else max(1, round((end - start) / 55))
        
        return {
            "start_sec": start,
            "end_sec": end,
            "num_parts": parts,
            "caption": caption_m.group(1).strip() if caption_m else payload.niche.title()
        }
    except Exception as e:
        print(f"Analyze error: {e}")
        return {"error": str(e)}

@app.post("/api/v1/create-checkout-session")
async def create_checkout_session(request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")
    domain = str(request.base_url).rstrip("/")

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        client_reference_id=user_id,
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {
                    "name": "ViralClip AI - Lifetime Access",
                    "description": "Unlimited viral clip generation and YouTube auto-posting."
                },
                "unit_amount": 4900,
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=f"{domain}/?payment=success",
        cancel_url=f"{domain}/?payment=cancel",
    )
    return {"checkout_url": session.url}

@app.post("/api/v1/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session.get("client_reference_id")
        if user_id and supabase:
            try:
                supabase.table("users").update({"license": "lifetime"}).eq("id", user_id).execute()
            except Exception as e:
                print(f"Stripe webhook DB error: {e}")

    return {"status": "success"}

from fastapi.responses import RedirectResponse
import google_auth_oauthlib.flow

@app.get("/api/v1/auth/youtube")
async def auth_youtube(request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")
    
    import urllib.parse
    import uuid
    state = str(uuid.uuid4())
    
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(YOUTUBE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state
    }
    
    authorization_url = "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params)
    
    # Store state in redis with user_id to verify later
    if redis_client:
        redis_client.setex(f"oauth_state:{state}", 600, user_id)
        
    return RedirectResponse(authorization_url)

@app.get("/api/v1/auth/youtube/callback")
async def auth_youtube_callback(request: Request, state: str = None, code: str = None):
    if not state or not code:
        return {"error": "Missing state or code"}
        
    user_id = redis_client.get(f"oauth_state:{state}") if redis_client else "demo_user_123"
    
    try:
        import httpx
        token_data = {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": GOOGLE_REDIRECT_URI
        }
        
        # Exchange authorization code for tokens
        with httpx.Client() as client:
            r = client.post("https://oauth2.googleapis.com/token", data=token_data)
            
        if r.status_code != 200:
            raise Exception(f"Google Token API returned {r.status_code}: {r.text}")
            
        token_json = r.json()
        access_token = token_json.get("access_token")
        refresh_token = token_json.get("refresh_token")
        
        # Save to Supabase
        if supabase:
            update_data = {
                "youtube_access_token": access_token,
                "youtube_connected": True
            }
            # Only update refresh_token if Google actually sent one (it only sends on first consent)
            if refresh_token:
                update_data["youtube_refresh_token"] = refresh_token
                
            supabase.table("users").update(update_data).eq("id", user_id).execute()
            
        return RedirectResponse("/?youtube=connected")
    except Exception as e:
        import urllib.parse
        error_msg = urllib.parse.quote(str(e))
        print(f"OAuth Error: {e}")
        return RedirectResponse(f"/?youtube=error&detail={error_msg}")
