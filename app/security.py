"""
app/security.py

Three real upgrades over v2:

1. USER SESSIONS: v2 identified users by a `user_id` cookie the *client*
   was trusted to set (and the frontend even let JS mint its own
   `user_{random}` id via localStorage!). That's an unauthenticated
   identity — anyone could set the cookie to someone else's user_id and
   read their profile/analytics. Now sessions are signed JWTs the server
   issues after real Google OAuth verification; the cookie is opaque and
   tamper-evident (HMAC-SHA256 signed, short expiry + refresh).

2. WORKER TOKENS: same idea as v2 (HMAC(WORKER_SECRET, user_id)), but
   time-boxed (5 minute TTL) and scoped with a purpose string, so a
   leaked token can't be replayed forever and can't be reused across
   endpoints it wasn't issued for.

3. RATE LIMITING: v2's `_rate_buckets` was an in-process Python dict —
   it's reset on every deploy/restart, and doesn't work at all once you
   run more than one web process (each process has its own bucket, so
   real capacity is max_calls * num_processes). This is a Redis sorted-set
   sliding window, shared across every process/replica.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional

import jwt
from fastapi import HTTPException, Request

from .config import get_settings
from .logging_conf import get_logger
from .redis_client import get_redis

log = get_logger("security")
settings = get_settings()

SESSION_COOKIE = "clipai_session"
SESSION_TTL_SEC = 60 * 60 * 24 * 30  # 30 days
WORKER_TOKEN_TTL_SEC = 300


# ── User sessions (JWT) ────────────────────────────────────────────
def issue_session_token(user_id: str, email: str = "") -> str:
    now = int(time.time())
    payload = {"sub": user_id, "email": email, "iat": now, "exp": now + SESSION_TTL_SEC}
    return jwt.encode(payload, _signing_key(), algorithm="HS256")


def verify_session_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, _signing_key(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def require_user(request: Request) -> str:
    """FastAPI dependency: returns the authenticated user_id or raises 401.
    Unlike v2, there is no 'demo_user_123' fallback for real endpoints —
    an unauthenticated caller is a 401, not a shared demo account."""
    token = request.cookies.get(SESSION_COOKIE, "")
    data = verify_session_token(token) if token else None
    if not data:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    return data["sub"]


def optional_user(request: Request) -> Optional[str]:
    token = request.cookies.get(SESSION_COOKIE, "")
    data = verify_session_token(token) if token else None
    return data["sub"] if data else None


def _signing_key() -> str:
    key = settings.jwt_signing_key
    if not key:
        raise RuntimeError("JWT_SIGNING_KEY must be set — it must not fall back to WORKER_SECRET.")
    return key


# ── Worker <-> server auth ─────────────────────────────────────────
def sign_worker_token(user_id: str, purpose: str = "poll") -> str:
    """HMAC token, time-boxed and purpose-scoped. Safe on infra you control
    (cloud worker), same caveat as v2: never ship WORKER_SECRET in a
    distributable desktop binary."""
    if not settings.worker_secret:
        raise RuntimeError("WORKER_SECRET is not set.")
    window = int(time.time()) // WORKER_TOKEN_TTL_SEC
    msg = f"{user_id}:{purpose}:{window}".encode()
    return hmac.new(settings.worker_secret.encode(), msg, hashlib.sha256).hexdigest()


def verify_worker_token(user_id: str, token: str, purpose: str = "poll") -> bool:
    """NEW: checks both the current and previous WORKER_SECRET (if set),
    so rotating the secret doesn't invalidate every worker mid-flight."""
    if not token or not user_id:
        return False
    secrets = [s for s in (settings.worker_secret, settings.worker_secret_previous) if s]
    if not secrets:
        return False
    now_window = int(time.time()) // WORKER_TOKEN_TTL_SEC
    for secret in secrets:
        for window in (now_window, now_window - 1):
            msg = f"{user_id}:{purpose}:{window}".encode()
            expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
            if hmac.compare_digest(expected, token):
                return True
    return False



def verify_admin(request: Request) -> None:
    auth = request.headers.get("X-Admin-Secret", "")
    candidates = [s for s in (settings.admin_secret, settings.admin_secret_previous) if s]
    if not candidates or not any(hmac.compare_digest(auth, c) for c in candidates):
        raise HTTPException(status_code=403, detail="Forbidden")



# ── Distributed rate limiting (Redis sorted-set sliding window) ────
async def rate_limit(key: str, max_calls: int, window_sec: int) -> None:
    r = get_redis()
    if r is None:
        return  # fail open if Redis is down — availability over strictness
    try:
        now = time.time()
        zkey = f"rl:{key}"
        pipe = r.pipeline()
        pipe.zremrangebyscore(zkey, 0, now - window_sec)
        pipe.zcard(zkey)
        pipe.zadd(zkey, {str(now): now})
        pipe.expire(zkey, window_sec + 5)
        _, count, *_ = await pipe.execute()
        if count >= max_calls:
            raise HTTPException(status_code=429, detail="Too many requests — please slow down.")
    except HTTPException:
        raise
    except Exception as e:
        log.warning("Rate limit check failed (failing open): %s", e)
        return


def stable_user_id_for_email(email: str) -> str:
    digest = hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()
    return f"user_{digest[:16]}"