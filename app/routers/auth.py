"""
app/routers/auth.py

SECURITY UPGRADE over v2: v2's `/api/v1/auth/youtube/callback` handled
BOTH "connect my YouTube channel" and "log me in with Google" in one
route, keyed off a `state` string prefix (`"login_"`). And separately,
`getActiveUserId()` in the *frontend JS* would mint its own
`user_{random}` id in localStorage if no cookie existed — meaning
identity was client-chosen, not server-issued. Anyone could set
`document.cookie = "user_id=<victim>"` and read that victim's profile,
analytics, and trigger jobs against their account (their `/api/v1/user/profile`
GET had zero auth check beyond trusting the cookie value).

Now: login and channel-connect are two distinct, clearly named routes.
Login verifies the Google id token server-side and issues our own
signed session JWT (see security.py) — the client never gets to choose
its own identity.
"""
from __future__ import annotations

import urllib.parse
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from ..config import get_settings
from ..db import UserRepo
from ..logging_conf import get_logger
from ..redis_client import get_redis
from fastapi import Depends
from ..security import SESSION_COOKIE, SESSION_TTL_SEC, issue_session_token, require_user, optional_user, stable_user_id_for_email

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
log = get_logger("auth")
settings = get_settings()

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
LOGIN_SCOPES = ["openid", "email", "profile"]


@router.get("/google/login")
async def start_google_login():
    """Sign-in-with-Google — issues our own session on success."""
    state = str(uuid.uuid4())
    r = get_redis()
    if r:
        await r.setex(f"oauth_state:login:{state}", 600, "1")
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": f"{settings.api_base_url}/api/v1/auth/google/callback",
        "response_type": "code",
        "scope": " ".join(LOGIN_SCOPES),
        "access_type": "online",
        "prompt": "select_account",
        "state": state,
    }
    return RedirectResponse("https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params))


@router.get("/google/callback")
async def google_login_callback(request: Request, state: str = "", code: str = ""):
    r = get_redis()
    valid = await r.get(f"oauth_state:login:{state}") if r else None
    if not code or not valid:
        return RedirectResponse("/?auth=error&detail=invalid_state")
    if r:
        await r.delete(f"oauth_state:login:{state}")

    async with httpx.AsyncClient(timeout=15) as client:
        token_res = await client.post("https://oauth2.googleapis.com/token", data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": f"{settings.api_base_url}/api/v1/auth/google/callback",
        })
        if token_res.status_code != 200:
            log.warning("Google token exchange failed: %s", token_res.text[:300])
            return RedirectResponse("/?auth=error")
        access_token = token_res.json().get("access_token")

        userinfo_res = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if userinfo_res.status_code != 200:
        return RedirectResponse("/?auth=error")

    info = userinfo_res.json()
    email = (info.get("email") or "").lower()
    if not email or not info.get("verified_email"):
        return RedirectResponse("/?auth=error&detail=unverified_email")

    existing = UserRepo.get_by_email(email)
    is_new = existing is None
    user_id = existing["id"] if existing else f"user_{uuid.uuid4().hex[:12]}"
    if is_new:
        UserRepo.get_or_create(user_id)
        UserRepo.update(user_id, {"email": email})
        ref_id = request.cookies.get("clipai_ref", "")
        if ref_id and ref_id != user_id:
            _apply_referral_bonus(user_id, ref_id)

    token = issue_session_token(user_id, email)
    resp = RedirectResponse("/?auth=success", status_code=302)
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL_SEC, httponly=True, samesite="lax", secure=settings.env == "production")
    if is_new:
        resp.delete_cookie("clipai_ref")
    return resp


def _apply_referral_bonus(new_user_id: str, referrer_id: str) -> None:
    bonus = settings.referral_bonus_clips
    referrer = UserRepo.get_or_create(referrer_id)
    UserRepo.update(referrer_id, {"free_clips_used": max(0, referrer.get("free_clips_used", 0) - bonus)})
    UserRepo.update(new_user_id, {"free_clips_used": 0, "referred_by": referrer_id})


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "success"}


# ── YouTube channel connect (separate from login) ──────────────────
@router.get("/youtube/connect")
async def connect_youtube(user_id: str = Depends(require_user)):
    state = str(uuid.uuid4())
    r = get_redis()
    if r:
        await r.setex(f"oauth_state:yt:{state}", 600, user_id)
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": f"{settings.api_base_url}/api/v1/auth/youtube/callback",
        "response_type": "code",
        "scope": " ".join(YOUTUBE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return RedirectResponse("https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params))


@router.get("/youtube/callback")
async def youtube_connect_callback(state: str = "", code: str = ""):
    r = get_redis()
    user_id = await r.get(f"oauth_state:yt:{state}") if r else None
    if not code or not user_id:
        return RedirectResponse("/?youtube=error&detail=invalid_state")

    async with httpx.AsyncClient(timeout=15) as client:
        token_res = await client.post("https://oauth2.googleapis.com/token", data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": f"{settings.api_base_url}/api/v1/auth/youtube/callback",
        })
    if token_res.status_code != 200:
        return RedirectResponse("/?youtube=error")
    data = token_res.json()
    update = {"youtube_access_token": data.get("access_token"), "youtube_connected": True}
    if data.get("refresh_token"):
        update["youtube_refresh_token"] = data["refresh_token"]
    UserRepo.update(user_id, update)
    return RedirectResponse("/?youtube=connected")


@router.get("/youtube/status")
async def youtube_status(user_id: str = Depends(require_user)):
    user = UserRepo.get_or_create(user_id)
    return {"connected": bool(user.get("youtube_refresh_token"))}