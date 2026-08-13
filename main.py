import os
import uuid
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

# Static files and templates
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Schemas
class ClipRequest(BaseModel):
    niche: str

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

@app.post("/api/v1/generate-clip")
async def generate_clip(payload: ClipRequest, request: Request):
    # Retrieve user ID (mock session ID for demonstration)
    user_id = request.cookies.get("user_id", "demo_user_123")
    user = get_or_create_user(user_id)

    # Enforce paywall on free tier
    if user["free_clip_used"] and user["license"] == "free_tier":
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

    # Mark free clip as used if on free tier
    if supabase and user["license"] == "free_tier":
        supabase.table("users").update({"free_clip_used": True}).eq("id", user_id).execute()

    # Run pipeline in background thread (worker process handles heavy lifting)
    from worker import run_clip_pipeline
    thread = threading.Thread(target=run_clip_pipeline, args=(payload.niche, user_id, job_id), daemon=True)
    thread.start()

    return {"status": "success", "job_id": job_id}

@app.get("/api/v1/job-status/{job_id}")
async def get_job_status(job_id: str):
    job_data = redis_client.hgetall(f"job:{job_id}")
    if not job_data:
        return {"status": "idle", "progress": 0, "message": "Job not found"}
    
    return {
        "status": job_data.get("status", "idle"),
        "progress": int(job_data.get("progress", 0)),
        "message": job_data.get("message", ""),
        "url": job_data.get("url", "")
    }

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
        if user_id:
            supabase.table("users").update({"license": "lifetime"}).eq("id", user_id).execute()

    return {"status": "success"}

from fastapi.responses import RedirectResponse
import google_auth_oauthlib.flow

@app.get("/api/v1/auth/youtube")
async def auth_youtube(request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")
    
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    
    flow = google_auth_oauthlib.flow.Flow.from_client_config(
        client_config,
        scopes=YOUTUBE_SCOPES
    )
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent"
    )
    
    # Store state in redis with user_id to verify later
    if redis_client:
        redis_client.setex(f"oauth_state:{state}", 600, user_id)
        
    return RedirectResponse(authorization_url)

@app.get("/api/v1/auth/youtube/callback")
async def auth_youtube_callback(request: Request, state: str = None, code: str = None):
    if not state or not code:
        return {"error": "Missing state or code"}
        
    user_id = redis_client.get(f"oauth_state:{state}") if redis_client else "demo_user_123"
    
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    
    flow = google_auth_oauthlib.flow.Flow.from_client_config(
        client_config,
        scopes=YOUTUBE_SCOPES,
        state=state
    )
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    
    try:
        flow.fetch_token(code=code)
        credentials = flow.credentials
        
        # Save to Supabase
        if supabase:
            supabase.table("users").update({
                "youtube_access_token": credentials.token,
                "youtube_refresh_token": credentials.refresh_token,
                "youtube_connected": True
            }).eq("id", user_id).execute()
            
        return RedirectResponse("/?youtube=connected")
    except Exception as e:
        import urllib.parse
        error_msg = urllib.parse.quote(str(e))
        print(f"OAuth Error: {e}")
        return RedirectResponse(f"/?youtube=error&detail={error_msg}")
