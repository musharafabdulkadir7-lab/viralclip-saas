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

import hashlib
import hmac
import time
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
from ..security import SESSION_COOKIE, SESSION_TTL_SEC, issue_session_token, require_user, optional_user

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
log = get_logger("auth")
settings = get_settings()

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
LOGIN_SCOPES = ["openid", "email", "profile"]
STATE_COOKIE_LOGIN = "clipai_oauth_login"
STATE_COOKIE_YT = "clipai_oauth_yt"


def _state_secret() -> str:
    return settings.jwt_signing_key or settings.worker_secret or "clipai_state_sig"


def _sign_state(state: str, extra: str = "") -> str:
    sig = hmac.new(_state_secret().encode(), f"{state}:{extra}".encode(), hashlib.sha256).hexdigest()
    return f"{state}.{extra}.{sig}"


def _verify_signed_state(raw: str) -> tuple[bool, str, str]:
    """Returns (valid, state, extra)"""
    if not raw or raw.count(".") < 2:
        return False, "", ""
    parts = raw.split(".", 2)
    state, extra, sig = parts[0], parts[1], parts[2]
    expected = hmac.new(_state_secret().encode(), f"{state}:{extra}".encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(expected, sig):
        return True, state, extra
    return False, "", ""


@router.get("/google/login")
async def start_google_login():
    """Sign-in-with-Google — issues our own session on success."""
    if not settings.google_client_id:
        log.error("Google OAuth client_id is not configured.")
        return RedirectResponse("/?auth=error&detail=not_configured")

    state = str(uuid.uuid4())
    r = get_redis()
    if r:
        try:
            await r.setex(f"oauth_state:login:{state}", 600, "1")
        except Exception as e:
            log.warning("Could not persist login oauth_state to Redis (using cookie fallback): %s", e)

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": f"{settings.api_base_url}/api/v1/auth/google/callback",
        "response_type": "code",
        "scope": " ".join(LOGIN_SCOPES),
        "access_type": "online",
        "prompt": "select_account",
        "state": state,
    }
    resp = RedirectResponse("https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params))
    # Set signed fallback state cookie in case Redis is unreachable/down
    resp.set_cookie(
        STATE_COOKIE_LOGIN,
        _sign_state(state, "1"),
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=settings.env == "production",
    )
    return resp


@router.get("/google/callback")
async def google_login_callback(request: Request, state: str = "", code: str = ""):
    if not code or not state:
        return RedirectResponse("/?auth=error&detail=invalid_state")

    # Verify state via Redis or fallback cookie
    r = get_redis()
    valid = False
    if r:
        try:
            res = await r.get(f"oauth_state:login:{state}")
            if res:
                valid = True
                await r.delete(f"oauth_state:login:{state}")
        except Exception as e:
            log.warning("Redis lookup for login oauth_state failed: %s", e)

    if not valid:
        cookie_val = request.cookies.get(STATE_COOKIE_LOGIN, "")
        cookie_valid, cookie_state, _ = _verify_signed_state(cookie_val)
        if cookie_valid and cookie_state == state:
            valid = True

    if not valid:
        return RedirectResponse("/?auth=error&detail=invalid_state")

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
    resp.delete_cookie(STATE_COOKIE_LOGIN)
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
    if not settings.google_client_id:
        log.error("Google OAuth client_id is not configured for YouTube connect.")
        return RedirectResponse("/?youtube=error&detail=not_configured")

    state = str(uuid.uuid4())
    r = get_redis()
    if r:
        try:
            await r.setex(f"oauth_state:yt:{state}", 600, user_id)
        except Exception as e:
            log.warning("Could not persist YouTube oauth_state to Redis (using cookie fallback): %s", e)

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": f"{settings.api_base_url}/api/v1/auth/youtube/callback",
        "response_type": "code",
        "scope": " ".join(YOUTUBE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    resp = RedirectResponse("https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params))
    resp.set_cookie(
        STATE_COOKIE_YT,
        _sign_state(state, user_id),
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=settings.env == "production",
    )
    return resp


@router.get("/youtube/callback")
async def youtube_connect_callback(request: Request, state: str = "", code: str = ""):
    if not code or not state:
        return RedirectResponse("/?youtube=error&detail=invalid_state")

    r = get_redis()
    user_id = None
    if r:
        try:
            user_id = await r.get(f"oauth_state:yt:{state}")
            if user_id:
                await r.delete(f"oauth_state:yt:{state}")
        except Exception as e:
            log.warning("Redis lookup for YouTube oauth_state failed: %s", e)

    if not user_id:
        cookie_val = request.cookies.get(STATE_COOKIE_YT, "")
        cookie_valid, cookie_state, cookie_user_id = _verify_signed_state(cookie_val)
        if cookie_valid and cookie_state == state:
            user_id = cookie_user_id

    if not user_id:
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
    resp = RedirectResponse("/?youtube=connected")
    resp.delete_cookie(STATE_COOKIE_YT)
    return resp


@router.get("/youtube/status")
async def youtube_status(user_id: str = Depends(require_user)):
    user = UserRepo.get_or_create(user_id)
    return {"connected": bool(user.get("youtube_refresh_token"))}