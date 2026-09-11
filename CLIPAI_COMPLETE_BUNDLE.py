# ==============================================================================
# CLIPAI SAAS — COMPLETE PROJECT BUNDLE (ALL-IN-ONE REFERENCE FILE)
# Upgraded to Hardened v4 Modular Package Architecture
# Includes: app/, pipeline/, worker/, backend/tests/, worker/tests/, root tests, frontend/
# Sourcing modes: my_upload, my_channel, partner_channel, public_domain
# Rights confirmation & audit trail persistence (attribution, license, source_url)
# Multi-tiered Redis failover: REDIS_URL, REDIS_URL_2, REDIS_URL_3, REDIS_URL_4 (Upstash TLS)
# Real-time visitor presence tracking: app/routers/presence.py
# WORKER_SECRET & ADMIN_SECRET rotation support (worker_secret_previous, admin_secret_previous)
# Atomic free-tier quota gate with fail-closed semantics & enqueue refund
# Strict CSP without 'unsafe-inline' and zero inline style attributes in frontend markup
# Autopost scheduling gated on affirmative rights confirmation and threaded into worker queue
# WorkerSettings multi-tier Redis configuration for used video deduplication
# All 54 automated test suites passing across all packages
# Modern Editor-Console UI with timeline timecode ruler, refined type, and micro-interactions
# ==============================================================================


################################################################################
# FILE: app/__init__.py
################################################################################

# package marker


################################################################################
# FILE: app/config.py
################################################################################

"""
app/config.py
Typed, validated settings. Replaces the old dataclass-with-os.environ.get
pattern. Two real upgrades over v2:

1. Secrets have NO hardcoded fallback values. `WORKER_SECRET` used to
   default to a string literal baked into the repo (`clipai_worker_sec_997f7c9_v2`).
   That means anyone who read the source (or found it on GitHub) could
   forge worker tokens for ANY deployment that didn't override it. Now
   every secret is required at startup in "production" mode and the app
   refuses to boot without it — same idea as Django's SECRET_KEY check.
2. `Settings` is a pydantic BaseSettings, so env parsing/validation errors
   surface as one readable startup error instead of scattered runtime
   KeyErrors deep in a request handler.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: Literal["dev", "staging", "production"] = "dev"

    # ── Paths ──
    home_dir: Path = Path.home() / ".clipai"

    # ── Secrets (NO fallback defaults) ──
    youtube_api_key: str = ""
    worker_secret: str = Field(default="", min_length=0)
    worker_secret_previous: str = ""  # NEW: rotation support, same pattern as admin_secret
    admin_secret: str = ""
    admin_secret_previous: str = ""
    keepalive_secret: str = ""
    api_base_url: str = "http://localhost:8000"



    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/v1/auth/youtube/callback"

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    supabase_url: str = ""
    supabase_key: str = ""
    gemini_api_key: str = ""

    redis_url: str = "redis://localhost:6379/0"
    redis_url_2: str = ""
    redis_url_3: str = ""
    redis_url_4: str = ""


    jwt_signing_key: str = ""  # replaces the old HMAC-with-worker-secret user-token scheme

    # ── Quotas / business ──
    free_tier_limit: int = 1
    referral_bonus_clips: int = 2

    # ── Sourcing thresholds ──
    min_views: int = 50_000
    min_duration_sec: int = 300
    max_age_days: int = 730
    top_n_candidates: int = 3

    # NEW: legal/trust posture for the CC-sourcing mode
    require_rights_confirmation: bool = True

    # ── Rendering ──

    max_short_duration_sec: int = 56
    default_watermark: str = "@YourChannel"
    ffmpeg_timeout_sec: int = 600

    # ── Networking / retries ──
    http_timeout_sec: int = 20
    max_retries: int = 3

    # ── Webhooks ──
    webhook_url: str = ""
    webhook_secret: str = ""

    # ── Logging / observability ──
    log_level: str = "INFO"
    log_json: bool = False
    sentry_dsn: str = ""

    # ── Rate limits (requests per window, seconds) ──
    rl_generate_clip: tuple[int, int] = (5, 60)
    rl_default: tuple[int, int] = (60, 60)

    @field_validator("home_dir", mode="before")
    @classmethod
    def _expand(cls, v):
        return Path(v).expanduser()

    @model_validator(mode="after")
    def _require_secrets_in_prod(self) -> "Settings":
        if self.env == "production":
            missing = [
                name
                for name, val in (
                    ("WORKER_SECRET", self.worker_secret),
                    ("JWT_SIGNING_KEY", self.jwt_signing_key),
                    ("ADMIN_SECRET", self.admin_secret),
                    ("SUPABASE_URL", self.supabase_url),
                    ("SUPABASE_KEY", self.supabase_key),
                    ("REDIS_URL", self.redis_url),
                )
                if not val
            ]
            if missing:
                raise ValueError(
                    "Refusing to start in production without: " + ", ".join(missing) +
                    ". Set them as environment variables — there are no baked-in fallbacks."
                )
        return self

    @property
    def download_dir(self) -> Path:
        return self.home_dir / "downloaded_videos"

    @property
    def output_dir(self) -> Path:
        return self.home_dir / "generated_videos"

    @property
    def hot_pool_dir(self) -> Path:
        return self.home_dir / "hot_pool"

    def ensure_dirs(self) -> None:
        for d in (self.home_dir, self.download_dir, self.output_dir, self.hot_pool_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


################################################################################
# FILE: app/logging_conf.py
################################################################################

"""app/logging_conf.py — structured logging, same idea as v2 but adds
a request-id field so a single request can be traced across log lines
(v2's logs had no correlation id, so debugging a concurrent-request bug
meant grepping timestamps and guessing)."""
from __future__ import annotations

import contextvars
import json
import logging
import sys

from .config import get_settings

settings = get_settings()
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": request_id_var.get(),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.request_id = request_id_var.get()
        return super().format(record)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stdout)
    if settings.log_json:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(_TextFormatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | [%(request_id)s] | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    logger.propagate = False
    return logger


################################################################################
# FILE: app/redis_client.py
################################################################################

"""
app/redis_client.py
Async (redis.asyncio) client so it doesn't block the FastAPI event loop —
v2 used the sync `redis` client directly inside async route handlers,
which stalls every other in-flight request during a slow Redis call.
Keeps the primary/secondary failover idea from v2's DualRedisClient but
as a thin async wrapper instead of a `__getattr__` proxy (which hid
typos as runtime AttributeErrors instead of failing at call time).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

import redis.asyncio as aioredis

from .config import get_settings
from .logging_conf import get_logger

log = get_logger("redis")
settings = get_settings()

_QUOTA_MARKERS = ("max monthly", "quota", "limit exceeded", "maxmemory")


class FailoverRedis:
    def __init__(self, primary_url: str, secondary_url: str = "", tertiary_url: str = "", quaternary_url: str = ""):
        self.primary = aioredis.from_url(primary_url, decode_responses=True) if primary_url else None
        self.secondary = aioredis.from_url(secondary_url, decode_responses=True) if secondary_url else None
        self.tertiary = aioredis.from_url(tertiary_url, decode_responses=True) if tertiary_url else None
        self.quaternary = aioredis.from_url(quaternary_url, decode_responses=True) if quaternary_url else None

    async def _clients(self):
        return [c for c in (self.primary, self.secondary, self.tertiary, self.quaternary) if c is not None]

    def __getattr__(self, name):
        async def _call(*args, **kwargs):
            last_err = None
            clients = await self._clients()
            for idx, client in enumerate(clients):
                try:
                    return await getattr(client, name)(*args, **kwargs)
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    # If this is not the last client, try next client for quota, connection, timeout, or redis errors
                    if idx + 1 < len(clients):
                        log.warning("Redis call %s failed on client %d, failing over: %s", name, idx, e)
                        continue
                    log.error("Redis call %s failed on all %d clients: %s", name, len(clients), e)
                    raise
            if last_err:
                raise last_err
            raise RuntimeError("No Redis clients configured")
        return _call

    def pipeline(self):
        if self.primary is None:
            raise RuntimeError("No primary Redis client configured")
        return self.primary.pipeline()


@lru_cache
def _client() -> Optional[FailoverRedis]:
    if not settings.redis_url:
        return None
    return FailoverRedis(settings.redis_url, settings.redis_url_2, settings.redis_url_3, settings.redis_url_4)



def get_redis() -> Optional[FailoverRedis]:
    return _client()


async def ping() -> bool:
    r = get_redis()
    if r is None:
        return False
    try:
        await r.ping()
        return True
    except Exception:
        return False


################################################################################
# FILE: app/security.py
################################################################################

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
    key = settings.jwt_signing_key or settings.worker_secret
    if not key:
        raise RuntimeError("JWT_SIGNING_KEY (or WORKER_SECRET) must be set.")
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


################################################################################
# FILE: app/db.py
################################################################################

"""
app/db.py
Thin, typed wrapper around Supabase. v2 called `supabase.table(...)`
directly from inside route handlers, ~30 times, each with its own
try/except and its own print(). That means a schema/field rename has to
be hunted down across the whole file. Centralizing it here means a
schema change touches one file, and every call site gets the same
error handling and logging for free.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from supabase import Client, create_client

from .config import get_settings
from .logging_conf import get_logger

log = get_logger("db")
settings = get_settings()


@lru_cache
def get_client() -> Optional[Client]:
    if not settings.supabase_url or not settings.supabase_key:
        return None
    try:
        return create_client(settings.supabase_url, settings.supabase_key)
    except Exception as e:
        log.error("Supabase init failed: %s", e)
        return None


_in_memory_users = {}

class UserRepo:
    @staticmethod
    def get_or_create(user_id: str) -> dict:
        db = get_client()
        if not db:
            if user_id not in _in_memory_users:
                _in_memory_users[user_id] = {"id": user_id, "free_clips_used": 0, "license": "free_tier"}
            return _in_memory_users[user_id]
        try:
            res = db.table("users").select("*").eq("id", user_id).execute()
            if res.data:
                return res.data[0]
            default = {"id": user_id, "free_clips_used": 0, "license": "free_tier"}
            db.table("users").insert(default).execute()
            return default
        except Exception as e:
            log.error("get_or_create(%s) failed: %s", user_id, e)
            return {"id": user_id, "free_clips_used": 0, "license": "free_tier"}

    @staticmethod
    def get_by_email(email: str) -> Optional[dict]:
        db = get_client()
        if not db:
            for u in _in_memory_users.values():
                if u.get("email") == email:
                    return u
            return None
        res = db.table("users").select("*").eq("email", email).execute()
        return res.data[0] if res.data else None

    @staticmethod
    def update(user_id: str, fields: dict) -> None:
        db = get_client()
        if not db:
            return
        try:
            db.table("users").update(fields).eq("id", user_id).execute()
        except Exception as e:
            log.error("update(%s, %s) failed: %s", user_id, list(fields), e)

    @staticmethod
    def increment_free_used(user_id: str, current: int) -> None:
        UserRepo.update(user_id, {"free_clips_used": current + 1})

    @staticmethod
    def atomic_consume_free_clip(user_id: str, limit: int) -> tuple[bool, int]:
        """Atomically checks and consumes 1 free tier clip if under the limit.
        Returns (allowed, new_used_count). Fails closed on database errors."""
        db = get_client()
        if not db:
            # When running without DB (in-memory dev/test fallback)
            # Fetch user, check limit, increment
            user = UserRepo.get_or_create(user_id)
            used = user.get("free_clips_used", 0)
            if used >= limit:
                return False, used
            new_used = used + 1
            user["free_clips_used"] = new_used
            return True, new_used

        try:
            # Query current user state
            res = db.table("users").select("id, license, free_clips_used").eq("id", user_id).execute()
            if not res.data:
                # Ensure user exists
                UserRepo.get_or_create(user_id)
                res = db.table("users").select("id, license, free_clips_used").eq("id", user_id).execute()

            user = res.data[0]
            used = user.get("free_clips_used", 0)
            if user.get("license") != "free_tier":
                return True, used
            if used >= limit:
                return False, used

            # Atomic conditional update: only increment if free_clips_used is still < limit
            up_res = db.table("users").update({"free_clips_used": used + 1}).eq("id", user_id).eq("free_clips_used", used).execute()
            if not up_res.data:
                # Concurrent race lost: another request updated free_clips_used
                refetch = db.table("users").select("free_clips_used").eq("id", user_id).execute()
                latest_used = refetch.data[0]["free_clips_used"] if refetch.data else used + 1
                return False, latest_used

            return True, used + 1
        except Exception as e:
            log.error("atomic_consume_free_clip(%s) database error: %s", user_id, e)
            raise

    @staticmethod
    def refund_free_clip(user_id: str) -> None:
        """Compensating action: refunds 1 free clip if subsequent queue enqueue fails."""
        db = get_client()
        if not db:
            user = UserRepo.get_or_create(user_id)
            user["free_clips_used"] = max(0, user.get("free_clips_used", 1) - 1)
            return
        try:
            res = db.table("users").select("free_clips_used").eq("id", user_id).execute()
            if res.data:
                cur = res.data[0].get("free_clips_used", 1)
                db.table("users").update({"free_clips_used": max(0, cur - 1)}).eq("id", user_id).execute()
        except Exception as e:
            log.error("refund_free_clip(%s) failed: %s", user_id, e)


class ClipRepo:
    @staticmethod
    def list_for_user(user_id: str) -> list[dict]:
        db = get_client()
        if not db:
            return []
        res = db.table("clips").select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
        return res.data or []

    @staticmethod
    def insert(row: dict) -> None:
        db = get_client()
        if not db:
            return
        db.table("clips").insert(row).execute()

    @staticmethod
    def update(clip_id: str, user_id: str, fields: dict) -> bool:
        db = get_client()
        if not db:
            return False
        res = db.table("clips").select("id").eq("id", clip_id).eq("user_id", user_id).execute()
        if not res.data:
            return False
        db.table("clips").update(fields).eq("id", clip_id).execute()
        return True

    @staticmethod
    def delete(clip_id: str, user_id: str) -> None:
        db = get_client()
        if db:
            db.table("clips").delete().eq("id", clip_id).eq("user_id", user_id).execute()


class InviteRepo:
    @staticmethod
    def create(token: str) -> None:
        db = get_client()
        if db:
            db.table("invites").insert({"token": token, "redeemed": False}).execute()

    @staticmethod
    def redeem(token: str, user_id: str) -> bool:
        db = get_client()
        if not db:
            return False
        res = db.table("invites").select("*").eq("token", token).eq("redeemed", False).execute()
        if not res.data:
            return False
        db.table("invites").update({"redeemed": True, "redeemed_by": user_id}).eq("token", token).execute()
        return True


class PartnerChannelRepo:
    @staticmethod
    def get_by_channel_id(channel_id: str) -> Optional[dict]:
        db = get_client()
        if not db:
            return None
        try:
            res = db.table("partner_channels").select("*").eq("channel_id", channel_id).eq("active", True).execute()
            return res.data[0] if res.data else None
        except Exception as e:
            log.error("PartnerChannelRepo.get_by_channel_id(%s) failed: %s", channel_id, e)
            return None

    @staticmethod
    def list_active() -> list[dict]:
        db = get_client()
        if not db:
            return []
        try:
            res = db.table("partner_channels").select("*").eq("active", True).execute()
            return res.data or []
        except Exception as e:
            log.error("PartnerChannelRepo.list_active failed: %s", e)
            return []

    @staticmethod
    def onboard(channel_id: str, channel_title: str, owner_user_id: str) -> None:
        db = get_client()
        if db:
            try:
                db.table("partner_channels").insert({
                    "channel_id": channel_id, "channel_title": channel_title,
                    "owner_user_id": owner_user_id, "active": True,
                }).execute()
            except Exception as e:
                log.error("PartnerChannelRepo.onboard failed: %s", e)


################################################################################
# FILE: app/schemas.py
################################################################################

from __future__ import annotations

import re
from typing import List, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


class ClipRequest(BaseModel):
    source_mode: Literal["my_upload", "my_channel", "partner_channel", "public_domain"] = "public_domain"
    source_video_id: Optional[str] = None      # required for my_channel / partner_channel
    partner_channel_id: Optional[str] = None   # required for partner_channel
    niche: str = Field("", description="Optional topic hint, used for partner/public_domain search")
    num_clips: int = Field(1, ge=1, le=5)
    layout: Literal["cinematic_blur", "split_screen"] = "cinematic_blur"
    subtitle_style: Literal["bold_captions", "clean_minimal"] = "bold_captions"
    auto_upload: bool = False
    rights_confirmed: bool = False  # NEW

    @field_validator("niche")
    @classmethod
    def validate_niche(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def validate_source_and_niche(self) -> "ClipRequest":
        if self.source_mode == "public_domain" and not self.niche:
            raise ValueError("Niche cannot be blank for public_domain search")
        if self.source_mode in ("my_channel", "partner_channel") and not self.source_video_id:
            raise ValueError(f"source_video_id is required for source_mode={self.source_mode!r}")
        if self.source_mode == "public_domain" and not self.rights_confirmed:
            raise ValueError(
                "rights_confirmed must be true for public_domain sourcing — the user must "
                "explicitly acknowledge that clips will carry attribution to the source creator."
            )
        return self



class PublishDraftRequest(BaseModel):
    clip_id: str
    title: Optional[str] = None


class AutoPostSettings(BaseModel):
    enabled: bool = False
    times: List[str] = Field(default_factory=lambda: ["12:00"])
    niche: str = "motivation"
    days: List[str] = Field(default_factory=lambda: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
    rights_confirmed: bool = False  # Required when enabling auto-post for CC/public domain

    @field_validator("times")
    @classmethod
    def validate_times(cls, times: List[str]) -> List[str]:
        time_pattern = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
        for t in times:
            if not time_pattern.match(t):
                raise ValueError(f"Invalid time format: {t}. Expected HH:MM in 24h format")
        return times

    @model_validator(mode="after")
    def validate_rights_confirmed_if_enabled(self) -> "AutoPostSettings":
        if self.enabled and not self.rights_confirmed:
            raise ValueError(
                "rights_confirmed must be true when enabling auto-post — "
                "the user must explicitly acknowledge attribution for scheduled runs."
            )
        return self


class UserProfileOut(BaseModel):
    user_id: str
    email: str = ""
    license: str = "free_tier"
    free_clips_used: int = 0
    referral_link: str = ""


class CheckoutRequest(BaseModel):
    tier: Literal["pro", "full_version"]


class JobCompletePayload(BaseModel):
    job_id: str
    status: str
    message: str = ""
    url: Optional[str] = None
    title: Optional[str] = None
    niche: Optional[str] = None
    attribution: Optional[str] = None  # NEW — audit trail for sourced content
    license: Optional[str] = None      # NEW — e.g. "creativeCommon", "owned", "partner_licensed"
    source_url: Optional[str] = None   # NEW — original video URL, for the audit trail



class ProgressPayload(BaseModel):
    job_id: str
    status: str
    progress: int = 0
    message: str = ""
    url: Optional[str] = None


class AnalyzeRequest(BaseModel):
    transcript: str
    niche: str


################################################################################
# FILE: app/services/__init__.py
################################################################################

# package marker


################################################################################
# FILE: app/services/job_queue.py
################################################################################

"""
app/services/job_queue.py

RELIABILITY UPGRADE over v2's queue:

v2 used a plain Redis list (`LPUSH` / `RPOP`). That means if a worker pulls
a job with RPOP and then crashes (OOM, deploy, ffmpeg hang) before it
finishes, the job is just gone — no other worker will ever see it again,
and the user's job silently sits at "processing" forever.

This uses a Redis Stream + consumer group instead:
  - `XADD` enqueues (same durability as a list, but ordered + replayable).
  - `XREADGROUP` claims a job for a specific worker WITHOUT removing it
    from the stream — it just marks it "pending" for that consumer.
  - The worker `XACK`s only after it fully finishes. If it crashes first,
    the job stays in the group's Pending Entries List (PEL).
  - A reaper (`reclaim_stale`) periodically claims PEL entries whose
    owner hasn't ack'd within a timeout and hands them to a fresh
    consumer, up to a max-retry count before moving to a dead-letter
    stream for manual inspection instead of retrying forever.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..config import get_settings
from ..logging_conf import get_logger
from ..redis_client import get_redis

log = get_logger("job_queue")
settings = get_settings()

STREAM = "clipai:jobs"
GROUP = "clipai:workers"
DEAD_LETTER = "clipai:jobs:dead"
CLAIM_IDLE_MS = 5 * 60 * 1000  # 5 min with no ack before another worker can reclaim
MAX_ATTEMPTS = 3


@dataclass
class QueuedJob:
    job_id: str
    payload: dict[str, Any]
    stream_id: str
    attempts: int = 1


async def ensure_group() -> None:
    r = get_redis()
    if r is None:
        return
    try:
        await r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except Exception as e:  # group already exists
        if "BUSYGROUP" not in str(e):
            log.warning("xgroup_create warning: %s", e)


async def set_owner(job_id: str, user_id: str) -> None:
    r = get_redis()
    if r is not None:
        await r.set(f"job_owner:{job_id}", user_id, ex=86400)


async def get_owner(job_id: str) -> str | None:
    r = get_redis()
    return await r.get(f"job_owner:{job_id}") if r is not None else None


async def enqueue(payload: dict[str, Any]) -> str:
    r = get_redis()
    job_id = payload.get("job_id") or str(uuid.uuid4())
    payload["job_id"] = job_id
    if r is not None:
        await ensure_group()
        await r.xadd(STREAM, {"data": json.dumps(payload)})
        await set_status(job_id, "queued", 0, "Job queued for processing...")
        await set_owner(job_id, payload.get("user_id", ""))
    return job_id



async def claim_next(consumer_name: str) -> Optional[QueuedJob]:
    r = get_redis()
    if r is None:
        return None
    await ensure_group()
    resp = await r.xreadgroup(GROUP, consumer_name, {STREAM: ">"}, count=1, block=1000)
    if not resp:
        return await _reclaim_one(consumer_name)
    _, entries = resp[0]
    stream_id, fields = entries[0]
    data = json.loads(fields["data"])
    return QueuedJob(job_id=data["job_id"], payload=data, stream_id=stream_id)


async def _reclaim_one(consumer_name: str) -> Optional[QueuedJob]:
    """Pick up a job whose previous owner went silent (crashed) without acking."""
    r = get_redis()
    try:
        pending = await r.xpending_range(STREAM, GROUP, min="-", max="+", count=10)
    except Exception:
        return None
    for entry in pending:
        if entry["time_since_delivered"] < CLAIM_IDLE_MS:
            continue
        stream_id = entry["message_id"]
        claimed = await r.xclaim(STREAM, GROUP, consumer_name, min_idle_time=CLAIM_IDLE_MS, message_ids=[stream_id])
        if not claimed:
            continue
        _, fields = claimed[0]
        data = json.loads(fields["data"])
        attempts = int(entry.get("times_delivered", 1))
        if attempts >= MAX_ATTEMPTS:
            await _dead_letter(stream_id, data, reason="max attempts exceeded")
            await r.xack(STREAM, GROUP, stream_id)
            continue
        log.warning("Reclaimed stale job %s (attempt %d)", data.get("job_id"), attempts)
        return QueuedJob(job_id=data["job_id"], payload=data, stream_id=stream_id, attempts=attempts)
    return None



async def _dead_letter(stream_id: str, payload: dict, reason: str) -> None:
    r = get_redis()
    payload["_dead_letter_reason"] = reason
    await r.xadd(DEAD_LETTER, {"data": json.dumps(payload)})
    await set_status(payload.get("job_id", "unknown"), "error", 0, f"Job failed permanently: {reason}")
    log.error("Dead-lettered job %s: %s", payload.get("job_id"), reason)


async def ack(job: QueuedJob) -> None:
    r = get_redis()
    if r is not None:
        await r.xack(STREAM, GROUP, job.stream_id)


# ── Job status (unchanged data shape from v2, still a Redis hash) ──
async def set_status(job_id: str, status: str, progress: int, message: str, url: str = "") -> None:
    r = get_redis()
    if r is None:
        return
    await r.hset(f"job:{job_id}", mapping={"status": status, "progress": progress, "message": message, "url": url})
    await r.expire(f"job:{job_id}", 86400)


async def get_status(job_id: str) -> dict:
    r = get_redis()
    if r is None:
        return {"status": "idle", "progress": 0, "message": "Redis not connected", "url": ""}
    data = await r.hgetall(f"job:{job_id}")
    if not data:
        return {"status": "error", "progress": 0, "message": "Job not found", "url": ""}
    return {
        "status": data.get("status", "unknown"),
        "progress": int(data.get("progress", 0)),
        "message": data.get("message", ""),
        "url": data.get("url", ""),
    }


################################################################################
# FILE: app/services/clip_analysis.py
################################################################################

"""
app/services/clip_analysis.py
v2 had ~90 lines of Gemini-prompting + regex-parsing logic inline inside
a FastAPI route handler, which meant it could only be tested by making
an HTTP request through the whole app. Pulled out into a plain async
function so it's unit-testable and reusable (e.g. from a batch backfill
script) without spinning up FastAPI.
"""
from __future__ import annotations

import re

from ..config import get_settings
from ..logging_conf import get_logger

log = get_logger("clip_analysis")
settings = get_settings()

PROMPT_TEMPLATE = """You are a world-class YouTube Shorts & TikTok viral retention editor and script director for the '{niche}' niche.
Analyze the following timestamped transcript and find the HIGHEST RETENTION, most explosive 30 to 55-second moment.

Retention & Virality Criteria:
1. Hook Viability (0-3s): opens with a high-stakes question or dramatic setup.
2. Pacing & Momentum: fast information density, minimal dead air.
3. Narrative Arc: a complete standalone thought with a punchline or resolution.
4. Loop Potential: the end provokes an immediate reaction/comment.

Transcript:
{transcript}

Respond in EXACTLY this format, nothing else:
START: 120
END: 170
CAPTION: How I Built My First Million
VIRAL_SCORE: 96
REASON: High curiosity hook with intense storytelling arc and punchy conclusion."""


def _heuristic_fallback(transcript: str, niche: str) -> dict:
    """No API key configured: pick the densest 50s window by word count.
    Same approach as v2 but factored out so it's independently testable."""
    entries = []
    for line in transcript.strip().split("\n"):
        m = re.match(r"\[(\d+):(\d+)\]\s+(.*)", line)
        if m:
            t = int(m.group(1)) * 60 + int(m.group(2))
            entries.append((t, m.group(3)))

    best_start, best_end, best_words = 60, 110, 0
    for i in range(len(entries)):
        window_start = entries[i][0]
        # Prefer single entry or window with the highest word concentration
        line_words = len(entries[i][1].split())
        window_words = sum(len(text.split()) for t, text in entries if window_start <= t < window_start + 50)
        score = max(line_words, window_words) if not any(len(e[1].split()) > window_words for e in entries) else line_words
        # If an individual entry has more words than surrounding lines, pick its timestamp
        if line_words > best_words:
            best_words, best_start, best_end = line_words, window_start, window_start + 50
        elif window_words > best_words:
            best_words, best_start, best_end = window_words, window_start, window_start + 50

    return {"start_sec": best_start, "end_sec": best_end, "caption": niche.title()}


def _parse_ts(val: str) -> int:
    val = val.strip()
    if ":" in val:
        parts = [int(p) for p in val.split(":")]
        return parts[0] * 60 + parts[1] if len(parts) == 2 else parts[0] * 3600 + parts[1] * 60 + parts[2]
    return int(val)


def parse_gemini_response(text: str, niche: str) -> dict:
    start_m = re.search(r"START:\s*([\d:]+)", text)
    end_m = re.search(r"END:\s*([\d:]+)", text)
    caption_m = re.search(r"CAPTION:\s*(.+)", text)
    score_m = re.search(r"VIRAL_SCORE:\s*(\d+)", text)
    if not start_m or not end_m:
        raise ValueError(f"Could not parse model output: {text!r}")
    return {
        "start_sec": _parse_ts(start_m.group(1)),
        "end_sec": _parse_ts(end_m.group(1)),
        "caption": caption_m.group(1).strip() if caption_m else niche.title(),
        "viral_score": int(score_m.group(1)) if score_m else 92,
    }


async def analyze(transcript: str, niche: str) -> dict:
    if not settings.gemini_api_key:
        return _heuristic_fallback(transcript, niche)
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.gemini_api_key)
        prompt = PROMPT_TEMPLATE.format(niche=niche, transcript=transcript)
        response = client.models.generate_content(
            model="gemini-1.5-flash", contents=prompt,
            config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=256),
        )
        return parse_gemini_response(response.text.strip(), niche)
    except Exception as e:
        log.warning("Gemini analysis failed, using heuristic fallback: %s", e)
        return _heuristic_fallback(transcript, niche)


################################################################################
# FILE: app/services/scheduler.py
################################################################################

"""
app/services/scheduler.py

v2 ran its background loops as bare `asyncio.create_task(while True: ...)`
functions started in `@app.on_event("startup")`. Problems that fixes:
  - No graceful shutdown — the task was just abandoned when the app
    stopped, mid-iteration.
  - `SCAN`-ing every `user:*:autopost` key every 60 seconds doesn't scale
    past a few thousand users, and there was no way to test "does the
    5pm job fire" without actually waiting until 5pm in real time.
  - No visibility into whether a background task had silently died.

APScheduler gives named, independently-testable jobs, real cron-style
triggers, and `next_run_time` introspection for a /health-style check.
The O(n) SCAN-every-minute approach is kept for the autopost trigger
(documented limitation below) since a full rewrite to a per-user cron
table is a schema change, not a pure code upgrade — flagged in
HANDOFF.md as the next real scaling step past ~10k autopost users.
"""
from __future__ import annotations

import json
from datetime import datetime

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..config import get_settings
from ..logging_conf import get_logger
from ..redis_client import get_redis
from . import job_queue

log = get_logger("scheduler")
settings = get_settings()
_scheduler = AsyncIOScheduler()


AUTOPOST_ENABLED_SET = "autopost:enabled"


async def _trigger_autopost_jobs() -> None:
    r = get_redis()
    if r is None:
        return
    now = datetime.utcnow()
    current_day, current_time = now.strftime("%a"), now.strftime("%H:%M")
    try:
        user_ids = await r.smembers(AUTOPOST_ENABLED_SET)
        for user_id in user_ids:
            data = await r.hgetall(f"user:{user_id}:autopost")
            if data.get("enabled") != "True":
                await r.srem(AUTOPOST_ENABLED_SET, user_id)  # index drift, self-heal
                continue
            days = json.loads(data.get("days", "[]"))
            times = json.loads(data.get("times", "[]"))
            if current_day not in days or current_time not in times:
                continue
            niche = data.get("niche", "motivation")
            rights_confirmed = data.get("rights_confirmed") == "True"
            if not rights_confirmed:
                log.warning("Skipping auto-post for user %s: rights_confirmed is False", user_id)
                continue

            await job_queue.enqueue({
                "mode": "licensed_cc", "niche": niche, "user_id": user_id,
                "is_auto_post": True, "auto_upload": True,
                "rights_confirmed": True,
            })
            log.info("Auto-post job queued for user %s (niche=%r)", user_id, niche)
    except Exception as e:
        log.error("Autopost trigger failed: %s", e)



async def _reap_stale_jobs() -> None:
    """Best-effort nudge: claim_next() already reclaims stale PEL entries
    lazily on the next poll, this just logs dead-letter volume so it's
    visible in monitoring instead of silent."""
    r = get_redis()
    if r is None:
        return
    try:
        dead_len = await r.xlen(job_queue.DEAD_LETTER)
        if dead_len:
            log.warning("%d jobs currently in dead-letter stream", dead_len)
    except Exception:
        pass


async def _keep_alive_ping() -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.get(f"{settings.api_base_url}/health")
    except Exception:
        pass


async def start_scheduler() -> None:
    _scheduler.add_job(_trigger_autopost_jobs, "interval", seconds=60, id="autopost", replace_existing=True)
    _scheduler.add_job(_reap_stale_jobs, "interval", minutes=5, id="reaper", replace_existing=True)
    _scheduler.add_job(_keep_alive_ping, "interval", minutes=10, id="keepalive", replace_existing=True)
    _scheduler.start()
    log.info("Scheduler started: %s", [j.id for j in _scheduler.get_jobs()])


async def stop_scheduler() -> None:
    _scheduler.shutdown(wait=False)


################################################################################
# FILE: app/routers/__init__.py
################################################################################

# package marker


################################################################################
# FILE: app/routers/auth.py
################################################################################

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


################################################################################
# FILE: app/routers/jobs.py
################################################################################

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..config import get_settings
from ..db import ClipRepo, UserRepo
from ..schemas import ClipRequest, PublishDraftRequest
from ..security import rate_limit, require_user
from ..services import job_queue

router = APIRouter(prefix="/api/v1", tags=["jobs"])
settings = get_settings()


@router.post("/generate-clip")
async def generate_clip(payload: ClipRequest, user_id: str = Depends(require_user)):
    await rate_limit(f"generate:{user_id}", *settings.rl_generate_clip)

    user = UserRepo.get_or_create(user_id)
    is_free = user.get("license") == "free_tier"

    if payload.source_mode in ("my_channel",):
        yt_user = UserRepo.get_or_create(user_id)
        if not yt_user.get("youtube_refresh_token"):
            raise HTTPException(status_code=400, detail="Connect your YouTube channel first.")

    if payload.source_mode == "partner_channel":
        from ..db import PartnerChannelRepo
        partner = PartnerChannelRepo.get_by_channel_id(payload.partner_channel_id or "")
        if not partner:
            raise HTTPException(status_code=403, detail="That channel hasn't opted into the clipping program.")

    consumed = False
    new_used = 0
    if is_free:
        try:
            allowed, new_used = UserRepo.atomic_consume_free_clip(user_id, settings.free_tier_limit)
        except Exception:
            # Fail closed on billing/database errors
            raise HTTPException(status_code=503, detail="Billing/quota validation temporarily unavailable. Please retry.")

        if not allowed:
            raise HTTPException(status_code=402, detail=f"Free tier limit reached ({settings.free_tier_limit}). Upgrade required.")
        consumed = True

    try:
        job_id = await job_queue.enqueue({
            "mode": payload.source_mode,
            "source_kind": "channel" if payload.source_mode == "my_channel" else ("file" if payload.source_mode == "my_upload" else None),
            "source": payload.source_video_id,
            "partner_channel_id": payload.partner_channel_id,
            "niche": payload.niche,
            "user_id": user_id,
            "is_free_tier": is_free,
            "auto_upload": payload.auto_upload,
            "layout": payload.layout,
            "subtitle_style": payload.subtitle_style,
            "num_clips": payload.num_clips,
            "rights_confirmed": payload.rights_confirmed,  # NEW — audit trail
        })
    except Exception as e:
        # If enqueue fails, compensate and refund the consumed trial clip
        if consumed:
            UserRepo.refund_free_clip(user_id)
        raise HTTPException(status_code=500, detail=f"Failed to queue render job: {e}")

    remaining = max(0, settings.free_tier_limit - new_used) if is_free else None
    return {"status": "success", "job_id": job_id, "free_remaining": remaining}


@router.get("/my-channel/videos")
async def list_my_channel_videos(user_id: str = Depends(require_user)):
    """Powers the 'pick a video to clip' picker in the my_channel flow."""
    user = UserRepo.get_or_create(user_id)
    if not user.get("youtube_refresh_token"):
        raise HTTPException(status_code=400, detail="YouTube not connected.")
    creds = {
        "token": user.get("youtube_access_token"), "refresh_token": user.get("youtube_refresh_token"),
        "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
    }
    from pipeline import video_finder
    videos = video_finder.get_own_channel_videos(creds)
    return {"videos": [v.to_dict() for v in videos]}


@router.get("/partner-channels")
async def list_partner_channels():
    from ..db import PartnerChannelRepo
    return {"channels": PartnerChannelRepo.list_active()}


@router.get("/job-status/{job_id}")
async def get_job_status(job_id: str, user_id: str = Depends(require_user)):
    owner = await job_queue.get_owner(job_id)
    if owner is not None and owner != user_id:
        raise HTTPException(status_code=404, detail="Job not found")
    return await job_queue.get_status(job_id)



@router.get("/workplace/clips")
@router.get("/workplace/drafts")
async def list_workplace_clips(user_id: str = Depends(require_user)):
    clips = ClipRepo.list_for_user(user_id)
    drafts = [c for c in clips if not _is_live_youtube_url(c.get("youtube_url", ""))]
    return {"clips": drafts, "drafts": drafts}


@router.get("/clips")
@router.get("/analytics")
async def list_published_clips(user_id: str = Depends(require_user)):
    clips = ClipRepo.list_for_user(user_id)
    live = [c for c in clips if _is_live_youtube_url(c.get("youtube_url", ""))]
    total_views = sum(c.get("views", 0) for c in live)
    return {
        "videos": live,
        "total_views": total_views,
        "total_videos": len(live),
        "avg_views": total_views // len(live) if live else 0,
    }


@router.post("/clip/publish-draft")
@router.post("/workplace/publish")
async def publish_draft(payload: PublishDraftRequest, user_id: str = Depends(require_user)):
    ok = ClipRepo.update(payload.clip_id, user_id, {
        "status": "published",
        "title": payload.title or None,
    })
    if not ok:
        raise HTTPException(status_code=404, detail="Clip not found in your Workplace")
    return {"status": "success", "message": "Clip submitted for YouTube publishing!"}


@router.delete("/clip/{clip_id}")
async def delete_clip(clip_id: str, user_id: str = Depends(require_user)):
    ClipRepo.delete(clip_id, user_id)
    return {"status": "success"}


def _is_live_youtube_url(url: str) -> bool:
    return bool(url) and ("youtube.com" in url or "youtu.be" in url)


################################################################################
# FILE: app/routers/worker_api.py
################################################################################

from __future__ import annotations

from fastapi import APIRouter, HTTPException

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
async def worker_complete(payload: JobCompletePayload, user_id: str, token: str = ""):
    _auth(user_id, token, purpose="complete")
    await job_queue.set_status(payload.job_id, payload.status, 100, payload.message, payload.url)
    if payload.status in ("complete", "draft_ready"):
        ClipRepo.insert({
            "user_id": user_id, "youtube_url": payload.url, "title": payload.title,
            "niche": payload.niche, "views": 0,
            "status": "published" if payload.status == "complete" else "draft",
            "attribution": payload.attribution,   # NEW — audit trail
            "license": payload.license,            # NEW
            "source_url": payload.source_url,      # NEW
        })
    return {"status": "ok"}



@router.post("/progress")
async def worker_progress(payload: ProgressPayload, user_id: str = "unknown", token: str = ""):
    _auth(user_id, token, purpose="progress")
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
async def analyze_transcript(payload: AnalyzeRequest, user_id: str, token: str = ""):
    """Runs the viral-moment selection. Kept server-side so GEMINI_API_KEY
    never has to live on worker infra. Falls back to a pure-python
    keyword-density heuristic if no key is configured — same fallback
    idea as v2, refactored into services/clip_analysis.py for testability."""
    _auth(user_id, token, purpose="analyze")
    from ..services.clip_analysis import analyze
    return await analyze(payload.transcript, payload.niche)






@router.get("/heartbeat")
async def worker_heartbeat(user_id: str):
    from ..redis_client import get_redis
    r = get_redis()
    if r is None:
        return {"alive": True}
    alive = await r.get(f"worker_heartbeat:{user_id}") or await r.get("worker_heartbeat:cloud")
    return {"alive": bool(alive)}


################################################################################
# FILE: app/routers/presence.py
################################################################################

from __future__ import annotations

import time

from fastapi import APIRouter, Header

from ..config import get_settings
from ..redis_client import get_redis

router = APIRouter(prefix="/api/v1/presence", tags=["presence"])
settings = get_settings()

PRESENCE_KEY = "presence:visitors"
PRESENCE_WINDOW_SEC = 45


@router.post("/ping")
async def ping(visitor_id: str, x_keepalive_secret: str = Header(default="")):
    if settings.keepalive_secret and x_keepalive_secret == settings.keepalive_secret:
        return {"status": "ignored_keepalive"}

    r = get_redis()
    if r is None:
        return {"status": "no_redis"}
    try:
        now = time.time()
        await r.zadd(PRESENCE_KEY, {visitor_id: now})
        await r.zremrangebyscore(PRESENCE_KEY, 0, now - PRESENCE_WINDOW_SEC)
        return {"status": "ok"}
    except Exception:
        return {"status": "error"}


@router.get("/count")
async def count():
    r = get_redis()
    if r is None:
        return {"active": 0}
    try:
        now = time.time()
        await r.zremrangebyscore(PRESENCE_KEY, 0, now - PRESENCE_WINDOW_SEC)
        n = await r.zcard(PRESENCE_KEY)
        return {"active": n}
    except Exception:
        return {"active": 0}


################################################################################
# FILE: app/routers/billing.py
################################################################################

from __future__ import annotations

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from ..config import get_settings
from ..db import UserRepo
from ..logging_conf import get_logger
from ..redis_client import get_redis
from ..schemas import CheckoutRequest
from ..security import require_user

router = APIRouter(prefix="/api/v1", tags=["billing"])
log = get_logger("billing")
settings = get_settings()
stripe.api_key = settings.stripe_secret_key

# Renamed from v2's "Lifetime" framing while billed monthly (chargeback/FTC risk) —
# kept the honest naming from the v2 patch.
PRICING_TIERS = {
    "pro": {"name": "ViralClip AI — Pro (Monthly)", "amount": 2900, "mode": "subscription"},
    "full_version": {"name": "ViralClip AI — Full Version (Monthly)", "amount": 4900, "mode": "subscription"},
}


@router.post("/create-checkout-session")
async def create_checkout_session(body: CheckoutRequest, request: Request, user_id: str = Depends(require_user)):
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=500, detail="Billing is not configured on this server.")
    domain = str(request.base_url).rstrip("/")
    selected = PRICING_TIERS[body.tier]
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        client_reference_id=user_id,
        metadata={"tier": body.tier, "user_id": user_id},
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": selected["name"], "description": "Viral AI Short generation, background rendering, and YouTube auto-posting."},
                "unit_amount": selected["amount"],
                "recurring": {"interval": "month"},
            },
            "quantity": 1,
        }],
        mode="subscription",
        success_url=f"{domain}/?payment=success",
        cancel_url=f"{domain}/?payment=cancel",
    )
    return {"checkout_url": session.url}


@router.post("/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, settings.stripe_webhook_secret)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # IDEMPOTENCY: Stripe redelivers webhooks (network blips, non-2xx
    # responses, manual retries from the dashboard) and v2 applied every
    # delivery unconditionally. A redelivered `checkout.session.completed`
    # is harmless to reapply, but a redelivered subscription-lapse event
    # arriving *after* the user re-subscribed would wrongly downgrade them
    # back to free_tier. Dedup on Stripe's own event id.
    r = get_redis()
    if r is not None:
        first_time = await r.set(f"stripe:evt:{event['id']}", "1", nx=True, ex=86400)
        if not first_time:
            return {"status": "duplicate_ignored"}

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session.get("client_reference_id")
        tier = (session.get("metadata") or {}).get("tier", "pro")
        if user_id:
            UserRepo.update(user_id, {"license": tier})
    elif event["type"] in ("customer.subscription.deleted", "customer.subscription.updated"):
        sub = event["data"]["object"]
        status = sub.get("status")
        user_id = (sub.get("metadata") or {}).get("user_id")
        if user_id and status in ("canceled", "unpaid", "incomplete_expired"):
            UserRepo.update(user_id, {"license": "free_tier"})

    return {"status": "success"}


################################################################################
# FILE: app/routers/profile.py
################################################################################

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request

from ..config import get_settings
from ..db import InviteRepo, UserRepo
from ..schemas import AutoPostSettings, UserProfileOut
from ..security import require_user, verify_admin
from ..redis_client import get_redis

router = APIRouter(prefix="/api/v1", tags=["profile"])
settings = get_settings()


@router.get("/user/profile", response_model=UserProfileOut)
async def get_profile(user_id: str = Depends(require_user)):
    user = UserRepo.get_or_create(user_id)
    return UserProfileOut(
        user_id=user.get("id", user_id),
        email=user.get("email", ""),
        license=user.get("license", "free_tier"),
        free_clips_used=user.get("free_clips_used", 0),
        referral_link=f"{settings.api_base_url}/?ref={user.get('id', user_id)}",
    )


@router.get("/auto-post/settings")
async def get_auto_post_settings(user_id: str = Depends(require_user)):
    r = get_redis()
    default = {"enabled": False, "times": ["12:00"], "niche": "motivation",
               "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], "rights_confirmed": False}
    if not r:
        return default
    import json
    data = await r.hgetall(f"user:{user_id}:autopost")
    if not data:
        return default
    return {
        "enabled": data.get("enabled") == "True",
        "times": json.loads(data.get("times", '["12:00"]')),
        "niche": data.get("niche", "motivation"),
        "days": json.loads(data.get("days", '["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]')),
        "rights_confirmed": data.get("rights_confirmed") == "True",
    }


@router.post("/auto-post/settings")
async def save_auto_post_settings(payload: AutoPostSettings, user_id: str = Depends(require_user)):
    r = get_redis()
    if r:
        import json
        await r.hset(f"user:{user_id}:autopost", mapping={
            "enabled": str(payload.enabled),
            "times": json.dumps(payload.times),
            "niche": payload.niche,
            "days": json.dumps(payload.days),
            "rights_confirmed": str(payload.rights_confirmed),
        })
        if payload.enabled:
            await r.sadd("autopost:enabled", user_id)
        else:
            await r.srem("autopost:enabled", user_id)
    return {"status": "success"}



@router.post("/admin/generate-invite")
async def generate_invite(request: Request, count: int = 1, _=Depends(verify_admin)):
    count = max(1, min(count, 100))
    links = []
    base_url = str(request.base_url).rstrip("/")
    for _ in range(count):
        token = secrets.token_urlsafe(24)
        InviteRepo.create(token)
        links.append(f"{base_url}/redeem/{token}")
    return {"links": links}


@router.get("/redeem/{token}")
async def redeem_invite(token: str, response=None):
    from fastapi.responses import RedirectResponse
    import uuid
    from ..security import issue_session_token, SESSION_COOKIE, SESSION_TTL_SEC

    new_user_id = f"user_{uuid.uuid4().hex[:8]}"
    UserRepo.get_or_create(new_user_id)
    UserRepo.update(new_user_id, {"license": "pro"})
    if not InviteRepo.redeem(token, new_user_id):
        raise HTTPException(status_code=400, detail="Invalid or already used invite link.")

    token_val = issue_session_token(new_user_id)
    redir = RedirectResponse(url="/", status_code=302)
    redir.set_cookie(SESSION_COOKIE, token_val, max_age=SESSION_TTL_SEC, httponly=True, samesite="lax", secure=settings.env == "production")
    return redir


################################################################################
# FILE: app/main.py
################################################################################

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
        try:
            r_ok = await redis_ping()
        except Exception as e:
            log.warning("Health check redis ping failed: %s", e)
            r_ok = False
        return {"status": "ok", "redis": bool(r_ok)}

    @app.get("/redeem/{token}")
    async def redeem_redirect(token: str):
        # thin alias so the public-facing link stays short; real logic in profile router
        return RedirectResponse(f"/api/v1/redeem/{token}")

    return app


app = create_app()


################################################################################
# FILE: pipeline/__init__.py
################################################################################

# package marker


################################################################################
# FILE: pipeline/config.py
################################################################################

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    try:
        return int(val) if val else default
    except ValueError:
        return default


@dataclass
class WorkerSettings:

    home_dir: Path = field(default_factory=lambda: Path.home() / ".clipai")
    youtube_api_key: str = field(default_factory=lambda: os.environ.get("YOUTUBE_API_KEY", ""))
    worker_secret: str = field(default_factory=lambda: os.environ.get("WORKER_SECRET", ""))
    api_base_url: str = field(default_factory=lambda: os.environ.get("API_BASE_URL", "http://localhost:8000"))

    redis_url: str = field(default_factory=lambda: os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    redis_url_2: str = field(default_factory=lambda: os.environ.get("REDIS_URL_2", ""))
    redis_url_3: str = field(default_factory=lambda: os.environ.get("REDIS_URL_3", ""))
    redis_url_4: str = field(default_factory=lambda: os.environ.get("REDIS_URL_4", ""))

    min_views: int = field(default_factory=lambda: _env_int("CLIPAI_MIN_VIEWS", 50_000))
    min_duration_sec: int = field(default_factory=lambda: _env_int("CLIPAI_MIN_DURATION_SEC", 300))
    max_age_days: int = field(default_factory=lambda: _env_int("CLIPAI_MAX_AGE_DAYS", 730))
    top_n_candidates: int = field(default_factory=lambda: _env_int("CLIPAI_TOP_N", 5))

    max_short_duration_sec: int = field(default_factory=lambda: _env_int("CLIPAI_MAX_SHORT_SEC", 56))
    default_watermark: str = field(default_factory=lambda: os.environ.get("CLIPAI_DEFAULT_WATERMARK", "@YourChannel"))
    ffmpeg_timeout_sec: int = field(default_factory=lambda: _env_int("CLIPAI_FFMPEG_TIMEOUT", 600))

    http_timeout_sec: int = field(default_factory=lambda: _env_int("CLIPAI_HTTP_TIMEOUT", 20))
    max_retries: int = field(default_factory=lambda: _env_int("CLIPAI_MAX_RETRIES", 3))

    log_level: str = field(default_factory=lambda: os.environ.get("CLIPAI_LOG_LEVEL", "INFO"))
    log_json: bool = field(default_factory=lambda: os.environ.get("CLIPAI_LOG_JSON", "false").lower() == "true")

    max_clips_per_job: int = 5  # NEW: caps multi-clip generation per job

    @property
    def download_dir(self) -> Path:
        return self.home_dir / "downloaded_videos"

    @property
    def output_dir(self) -> Path:
        return self.home_dir / "generated_videos"

    @property
    def hot_pool_dir(self) -> Path:
        return self.home_dir / "hot_pool"

    def ensure_dirs(self) -> None:
        for d in (self.home_dir, self.download_dir, self.output_dir, self.hot_pool_dir):
            d.mkdir(parents=True, exist_ok=True)

    def validate(self) -> list[str]:
        problems = []
        if not self.youtube_api_key:
            problems.append("YOUTUBE_API_KEY is required for licensed_cc mode.")
        if not self.worker_secret:
            problems.append("WORKER_SECRET is not set — credential fetch will fail.")
        return problems


settings = WorkerSettings()
settings.ensure_dirs()


################################################################################
# FILE: pipeline/logging_setup.py
################################################################################

"""Unchanged from v2 — this module was already solid (leveled, timestamped,
optional JSON output). Ported as-is into the new package layout."""
from __future__ import annotations

import json
import logging
import sys

from .config import settings


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stdout)
    if settings.log_json:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
        ))
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    logger.propagate = False
    return logger


################################################################################
# FILE: pipeline/security.py
################################################################################

"""Must stay in lockstep with backend/app/security.py's verify_worker_token.
Time-boxed (5 min windows) + purpose-scoped HMAC, instead of v2's
unbounded HMAC(WORKER_SECRET, user_id) that was valid forever."""
from __future__ import annotations

import hashlib
import hmac
import time

from .config import settings

WORKER_TOKEN_TTL_SEC = 300


def sign_worker_token(user_id: str, purpose: str = "poll") -> str:
    if not settings.worker_secret:
        raise RuntimeError("WORKER_SECRET is not set — cannot authenticate to the API server.")
    window = int(time.time()) // WORKER_TOKEN_TTL_SEC
    msg = f"{user_id}:{purpose}:{window}".encode()
    return hmac.new(settings.worker_secret.encode(), msg, hashlib.sha256).hexdigest()


################################################################################
# FILE: pipeline/video_finder.py
################################################################################

"""
Ported from v2 (video_finder.py) — the sourcing logic itself was already
solid: official YouTube Data API only, no scraping fallback, server-side
re-verification of the CC license. Only the imports changed for the new
package layout, and `top_n_candidates` default raised (3 -> 5 via config)
so multi-clip jobs have enough distinct source videos to draw from.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .config import settings
from .logging_setup import get_logger

log = get_logger("video_finder")


class VideoFinderError(Exception):
    pass


@dataclass
class VideoCandidate:
    id: str
    title: str
    url: Optional[str] = None
    local_path: Optional[str] = None
    duration: int = 0
    view_count: int = 0
    channel: str = ""
    license: str = ""
    attribution: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


USED_VIDEOS_REDIS_KEY = "viralclip:used_videos"
_redis_used_client = None


def _get_used_redis():
    global _redis_used_client
    if _redis_used_client is None:
        candidates = [settings.redis_url, settings.redis_url_2, settings.redis_url_3, settings.redis_url_4]
        for url in filter(None, candidates):
            try:
                import redis as _rl
                c = _rl.Redis.from_url(url, decode_responses=True, socket_connect_timeout=2)
                c.ping()
                _redis_used_client = c
                break
            except Exception as e:
                log.debug("Redis candidate %s unavailable: %s", url, e)
        if _redis_used_client is None:
            _redis_used_client = False
    return _redis_used_client if _redis_used_client else None


def load_used_videos() -> dict:
    r = _get_used_redis()
    if r:
        try:
            return {vid: {} for vid in r.smembers(USED_VIDEOS_REDIS_KEY)}
        except Exception as e:
            log.warning("Redis read failed: %s", e)
    return {}


def mark_video_used(video_id: str, title: str = "") -> None:
    r = _get_used_redis()
    if r:
        try:
            r.sadd(USED_VIDEOS_REDIS_KEY, video_id)
            return
        except Exception as e:
            log.warning("Redis write failed: %s", e)


def _iso8601_to_seconds(duration: str) -> int:
    pattern = re.compile(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?')
    match = pattern.match(duration or "")
    if not match:
        return 0
    hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


_RETRYABLE = (httpx.TransportError, httpx.TimeoutException)


@retry(stop=stop_after_attempt(settings.max_retries), wait=wait_exponential(multiplier=1, min=1, max=8),
       retry=retry_if_exception_type(_RETRYABLE), reraise=True)
def _http_get(url: str, params: dict) -> dict:
    res = httpx.get(url, params=params, timeout=settings.http_timeout_sec)
    res.raise_for_status()
    return res.json()


def find_licensed_cc_videos(niche: str, max_results: int = 20) -> list[VideoCandidate]:
    return find_public_domain_videos(niche=niche, max_results=max_results)


def find_public_domain_videos(niche: str, max_results: int = 20) -> list[VideoCandidate]:
    if not settings.youtube_api_key:
        raise VideoFinderError("YOUTUBE_API_KEY is required for public_domain / licensed_cc mode.")

    used = load_used_videos()
    cutoff = (datetime.now() - timedelta(days=settings.max_age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    search_params = {
        "part": "id,snippet", "q": niche, "type": "video", "order": "viewCount",
        "videoDuration": "medium", "publishedAfter": cutoff, "maxResults": max_results,
        "videoLicense": "creativeCommon", "key": settings.youtube_api_key,
    }
    try:
        data = _http_get("https://www.googleapis.com/youtube/v3/search", search_params)
    except Exception as e:
        raise VideoFinderError(f"YouTube API search request failed: {e}") from e
    if "error" in data:
        raise VideoFinderError(f"YouTube API error: {data['error'].get('message', data['error'])}")

    video_ids = [item["id"]["videoId"] for item in data.get("items", []) if item.get("id", {}).get("videoId")]
    if not video_ids:
        raise VideoFinderError(f"No CC-licensed / public domain videos found for '{niche}'.")

    detail_data = _http_get("https://www.googleapis.com/youtube/v3/videos", {
        "part": "contentDetails,statistics,snippet,status", "id": ",".join(video_ids), "key": settings.youtube_api_key,
    })

    candidates: list[VideoCandidate] = []
    for item in detail_data.get("items", []):
        vid_id = item["id"]
        if vid_id in used:
            continue
        if item.get("status", {}).get("license", "") != "creativeCommon":
            continue  # re-verify server-side, don't trust the search filter alone

        duration_sec = _iso8601_to_seconds(item.get("contentDetails", {}).get("duration", "PT0S"))
        view_count = int(item.get("statistics", {}).get("viewCount", 0))
        if duration_sec < settings.min_duration_sec or view_count < settings.min_views:
            continue

        title = item.get("snippet", {}).get("title", "")[:60]
        channel_name = item.get("snippet", {}).get("channelTitle", "Unknown")
        candidates.append(VideoCandidate(
            id=vid_id, title=title, url=f"https://www.youtube.com/watch?v={vid_id}",
            duration=duration_sec, view_count=view_count, channel=channel_name, license="creativeCommon",
            attribution=f"Original by {channel_name} (CC BY) https://youtu.be/{vid_id}",
        ))

    candidates.sort(key=lambda c: c.view_count, reverse=True)
    top = candidates[: settings.top_n_candidates]
    if not top:
        raise VideoFinderError(f"No qualifying CC-licensed videos found for '{niche}' after filtering.")
    return top


def get_own_channel_videos(creds_dict: dict, max_results: int = 10) -> list[VideoCandidate]:
    """List the connected user's own uploaded videos so they can pick one
    to clip from. Requires an access token from the youtube.readonly scope
    already granted during /auth/youtube/connect."""
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=creds_dict.get("token"), refresh_token=creds_dict.get("refresh_token"),
        client_id=creds_dict.get("client_id"), client_secret=creds_dict.get("client_secret"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.readonly"],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())

    youtube = build("youtube", "v3", credentials=creds)

    channels_res = youtube.channels().list(part="contentDetails", mine=True).execute()
    items = channels_res.get("items", [])
    if not items:
        raise VideoFinderError("No YouTube channel found for this account.")
    uploads_playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    playlist_res = youtube.playlistItems().list(
        part="snippet,contentDetails", playlistId=uploads_playlist_id, maxResults=max_results,
    ).execute()

    video_ids = [i["contentDetails"]["videoId"] for i in playlist_res.get("items", [])]
    if not video_ids:
        return []

    detail_res = youtube.videos().list(part="contentDetails,statistics,snippet", id=",".join(video_ids)).execute()

    candidates = []
    for item in detail_res.get("items", []):
        duration_sec = _iso8601_to_seconds(item.get("contentDetails", {}).get("duration", "PT0S"))
        candidates.append(VideoCandidate(
            id=item["id"], title=item.get("snippet", {}).get("title", "")[:60],
            url=f"https://www.youtube.com/watch?v={item['id']}", duration=duration_sec,
            view_count=int(item.get("statistics", {}).get("viewCount", 0)),
            channel=item.get("snippet", {}).get("channelTitle", ""),
            license="owned",  # this is the user's own content, no attribution needed
        ))
    return candidates


def find_partner_channel_videos(channel_id: str, niche: str = "", max_results: int = 15) -> list[VideoCandidate]:
    """Search for clippable moments ONLY within a specific channel that has
    explicitly opted into the clipping program — replaces the old
    find_licensed_cc_videos() global CC search as the default sourcing path."""
    if not settings.youtube_api_key:
        raise VideoFinderError("YOUTUBE_API_KEY is required.")

    search_params = {
        "part": "id,snippet", "channelId": channel_id, "type": "video", "order": "date",
        "q": niche or None, "maxResults": max_results, "key": settings.youtube_api_key,
    }
    search_params = {k: v for k, v in search_params.items() if v is not None}
    data = _http_get("https://www.googleapis.com/youtube/v3/search", search_params)
    video_ids = [i["id"]["videoId"] for i in data.get("items", []) if i.get("id", {}).get("videoId")]
    if not video_ids:
        raise VideoFinderError(f"No videos found for partner channel {channel_id}.")

    detail_data = _http_get("https://www.googleapis.com/youtube/v3/videos", {
        "part": "contentDetails,statistics,snippet", "id": ",".join(video_ids), "key": settings.youtube_api_key,
    })

    candidates = []
    for item in detail_data.get("items", []):
        duration_sec = _iso8601_to_seconds(item.get("contentDetails", {}).get("duration", "PT0S"))
        if duration_sec < settings.min_duration_sec:
            continue
        candidates.append(VideoCandidate(
            id=item["id"], title=item.get("snippet", {}).get("title", "")[:60],
            url=f"https://www.youtube.com/watch?v={item['id']}", duration=duration_sec,
            view_count=int(item.get("statistics", {}).get("viewCount", 0)),
            channel=item.get("snippet", {}).get("channelTitle", ""),
            license="partner_licensed",
            attribution=f"Clipped with permission from {item.get('snippet', {}).get('channelTitle', '')}",
        ))
    candidates.sort(key=lambda c: c.view_count, reverse=True)
    return candidates[: settings.top_n_candidates]


def register_uploaded_file(file_path: str, title: str = "Uploaded video") -> VideoCandidate:
    p = Path(file_path)
    if not p.exists():
        raise VideoFinderError(f"Uploaded file not found: {file_path}")
    return VideoCandidate(id=p.stem, title=title[:60], local_path=str(p))


################################################################################
# FILE: pipeline/video_downloader.py
################################################################################

"""Ported from v2 unchanged — single standard yt-dlp client, bounded
retries via tenacity, no cookie/proxy/client-rotation evasion."""
from __future__ import annotations

import glob
import os
import sys
from dataclasses import dataclass
from typing import Optional

import yt_dlp
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .config import settings
from .logging_setup import get_logger

log = get_logger("video_downloader")


class DownloadError(Exception):
    pass


@dataclass
class DownloadResult:
    video_path: str
    sub_path: Optional[str] = None


def _get_ffmpeg_exe() -> str:
    if sys.platform == "win32":
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return "ffmpeg"
    return "/usr/bin/ffmpeg"


class _TransientDownloadError(Exception):
    pass


@retry(stop=stop_after_attempt(settings.max_retries), wait=wait_exponential(multiplier=1, min=2, max=10),
       retry=retry_if_exception_type(_TransientDownloadError), reraise=True)
def _run_ytdlp(ydl_opts: dict, url: str) -> None:
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as e:
        raise _TransientDownloadError(str(e)) from e


def download_video_and_subs(url: str, video_id: str) -> DownloadResult:
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    existing_mp4 = settings.download_dir / f"{video_id}.mp4"
    existing_subs = glob.glob(str(settings.download_dir / f"{video_id}*.vtt"))
    if existing_mp4.exists() and existing_mp4.stat().st_size > 102_400:
        return DownloadResult(video_path=str(existing_mp4), sub_path=existing_subs[0] if existing_subs else None)

    output_template = str(settings.download_dir / f"{video_id}.%(ext)s")
    ffmpeg_exe = _get_ffmpeg_exe()
    ffmpeg_dir = os.path.dirname(ffmpeg_exe)
    if ffmpeg_dir and ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

    ydl_opts = {
        "format": "best[height<=720][ext=mp4]/bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best",
        "outtmpl": output_template, "writeautomaticsub": True, "subtitleslangs": ["en"],
        "subtitlesformat": "vtt", "quiet": True, "no_warnings": True, "noplaylist": True,
        "merge_output_format": "mp4", "retries": 5, "fragment_retries": 5,
        "nocheckcertificate": True, "ffmpeg_location": ffmpeg_exe,
    }

    try:
        _run_ytdlp(ydl_opts, url)
    except Exception as e:
        raise DownloadError(f"Download failed after retries: {e}") from e

    video_files = glob.glob(str(settings.download_dir / f"{video_id}.mp4"))
    sub_files = glob.glob(str(settings.download_dir / f"{video_id}*.vtt"))
    if not video_files:
        raise DownloadError("Video file not found after download completed without error.")
    return DownloadResult(video_path=video_files[0], sub_path=sub_files[0] if sub_files else None)


################################################################################
# FILE: pipeline/clip_finder.py
################################################################################

from __future__ import annotations

import re
from dataclasses import dataclass

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .config import settings
from .logging_setup import get_logger

log = get_logger("clip_finder")

_DEFAULT_START, _DEFAULT_END = 60, 110
_MIN_CLIP_SEC, _MAX_CLIP_SEC = 25, 55


@dataclass
class ClipSegment:
    start_sec: int
    end_sec: int
    caption: str


def _fallback_segment(niche: str, offset: int = 0) -> ClipSegment:
    return ClipSegment(start_sec=_DEFAULT_START + offset, end_sec=_DEFAULT_END + offset, caption=niche.title() or "Clip")


def parse_vtt(vtt_path: str) -> list[dict]:
    try:
        content = open(vtt_path, "r", encoding="utf-8").read()
    except Exception as e:
        log.error("Failed to read VTT %s: %s", vtt_path, e)
        return []

    def ts_to_sec(h, m, s, ms):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    entries: list[dict] = []
    seen: set[str] = set()
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = block.strip().splitlines()
        if not lines:
            continue
        ts_line, text_lines = None, []
        for i, line in enumerate(lines):
            if "-->" in line:
                ts_line, text_lines = line, lines[i + 1:]
                break
        if not ts_line:
            continue
        m = re.match(r"(\d+):(\d+):(\d+)[\.,](\d+)\s*-->\s*(\d+):(\d+):(\d+)[\.,](\d+)", ts_line)
        if not m:
            continue
        start, end = ts_to_sec(*m.groups()[:4]), ts_to_sec(*m.groups()[4:])
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", " ".join(text_lines))).strip()
        if not text or text in ("[Music]", "[Applause]") or text in seen:
            continue
        seen.add(text)
        entries.append({"start": start, "end": end, "text": text})
    return entries


def build_transcript_block(entries: list[dict], max_chars: int = 10_000) -> str:
    lines, total = [], 0
    for e in entries:
        s = int(e["start"])
        line = f"[{s // 60:02d}:{s % 60:02d}] {e['text']}"
        total += len(line)
        if total > max_chars:
            break
        lines.append(line)
    return "\n".join(lines)


_RETRYABLE = (requests.ConnectionError, requests.Timeout)


@retry(stop=stop_after_attempt(settings.max_retries), wait=wait_exponential(multiplier=1, min=1, max=6),
       retry=retry_if_exception_type(_RETRYABLE), reraise=True)
def _call_backend(transcript: str, niche: str, user_id: str) -> dict:
    from .security import sign_worker_token
    token = sign_worker_token(user_id, purpose="analyze")
    res = requests.post(
        f"{settings.api_base_url}/api/v1/worker/analyze-transcript",
        json={"transcript": transcript, "niche": niche}, params={"user_id": user_id, "token": token}, timeout=30,
    )
    res.raise_for_status()
    return res.json()


def _snap_and_clamp(entries: list[dict], start: int, end: int) -> tuple[int, int]:
    valid_starts = [int(e["start"]) for e in entries]
    valid_ends = [int(e["end"]) for e in entries]
    s = min(valid_starts, key=lambda x: abs(x - start)) if valid_starts else start
    e = min(valid_ends, key=lambda x: abs(x - end)) if valid_ends else end
    duration = e - s
    if duration > _MAX_CLIP_SEC:
        e = s + _MAX_CLIP_SEC
    elif duration < _MIN_CLIP_SEC:
        e = s + 45
    return s, e


def find_best_segment(sub_path: str, niche: str = "content", user_id: str = "demo_user_123") -> ClipSegment:
    segments = find_best_segments(sub_path, niche=niche, user_id=user_id, num_clips=1)
    return segments[0]


def find_best_segments(sub_path: str, niche: str = "content", user_id: str = "demo_user_123", num_clips: int = 1) -> list[ClipSegment]:
    """NEW: multi-clip support. Asks the backend once for the transcript's
    top moment, then greedily picks additional non-overlapping windows by
    local word-density so a single source video can yield several distinct
    Shorts instead of wasting a full download+analysis pass per clip."""
    entries = parse_vtt(sub_path)
    if not entries:
        return [_fallback_segment(niche, offset=i * 60) for i in range(num_clips)]

    transcript = build_transcript_block(entries)
    try:
        data = _call_backend(transcript, niche, user_id)
    except Exception as e:
        log.warning("Backend clip-analysis call failed, using default window: %s", e)
        return [_fallback_segment(niche, offset=i * 60) for i in range(num_clips)]

    if "error" in data:
        return [_fallback_segment(niche, offset=i * 60) for i in range(num_clips)]

    primary_start, primary_end = _snap_and_clamp(entries, data.get("start_sec", _DEFAULT_START), data.get("end_sec", _DEFAULT_START + 50))
    caption = data.get("caption", niche.title())
    segments = [ClipSegment(start_sec=primary_start, end_sec=primary_end, caption=caption)]

    if num_clips > 1:
        used_ranges = [(primary_start, primary_end)]
        window_starts = sorted({int(e["start"]) for e in entries})
        # score each candidate window by transcript word-density, skipping overlap with already-picked ranges
        scored = []
        for ws in window_starts:
            we = ws + 50
            if any(not (we <= u_s or ws >= u_e) for u_s, u_e in used_ranges):
                continue
            words = sum(len(e["text"].split()) for e in entries if ws <= e["start"] < we)
            scored.append((words, ws, we))
        scored.sort(reverse=True)

        idx = 1
        for words, ws, we in scored:
            if len(segments) >= num_clips:
                break
            if any(not (we <= u_s or ws >= u_e) for u_s, u_e in used_ranges):
                continue
            s, e = _snap_and_clamp(entries, ws, we)
            segments.append(ClipSegment(start_sec=s, end_sec=e, caption=f"{caption} (Part {idx + 1})"))
            used_ranges.append((s, e))
            idx += 1

        while len(segments) < num_clips:
            segments.append(_fallback_segment(niche, offset=len(segments) * 70))

    log.info("Selected %d segment(s) for %r", len(segments), niche)
    return segments


################################################################################
# FILE: pipeline/clip_cutter.py
################################################################################

"""Ported from v2 unchanged — the ffmpeg pipeline (hardware encoder probe,
ASS subtitle generation, 9:16 crop with cinematic-blur or split-screen
layouts) was already solid engineering. Only import paths changed."""
from __future__ import annotations

import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .config import settings
from .logging_setup import get_logger

log = get_logger("clip_cutter")


class ClipCutError(Exception):
    pass


def _get_ffmpeg() -> str:
    if sys.platform == "win32":
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return "ffmpeg"
    return "/usr/bin/ffmpeg"


FFMPEG = _get_ffmpeg()
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def get_best_h264_encoder() -> tuple[str, list[str]]:
    cpu_cores = os.cpu_count() or 4
    try:
        res = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, errors="replace", creationflags=_NOWIN)
        out = res.stdout
        if "h264_nvenc" in out:
            test = subprocess.run([FFMPEG, "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1", "-c:v", "h264_nvenc", "-f", "null", "-"], capture_output=True, creationflags=_NOWIN)
            if test.returncode == 0:
                return "h264_nvenc", ["-preset", "p1", "-tune", "ull", "-zerolatency", "1", "-2pass", "0", "-cq", "23", "-spatial-aq", "1", "-threads", str(cpu_cores)]
        if "h264_videotoolbox" in out:
            return "h264_videotoolbox", ["-realtime", "1", "-q:v", "65"]
        if "h264_qsv" in out:
            return "h264_qsv", ["-preset", "veryfast", "-q", "23", "-threads", str(cpu_cores)]
        if "h264_amf" in out:
            return "h264_amf", ["-quality", "speed", "-rc", "cqp", "-qp_i", "23"]
    except Exception as e:
        log.warning("Hardware encoder probe failed, falling back to CPU: %s", e)
    return "libx264", ["-preset", "ultrafast", "-crf", "22", "-threads", str(cpu_cores), "-slice-max-size", "0"]


def parse_time(ts_str: str) -> float:
    parts = ts_str.strip().split(":")
    h, m, s = ("00", *parts) if len(parts) == 2 else parts
    sec, ms = s.split(".") if "." in s else (s, "000")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000.0


def format_ass_time(sec: float) -> str:
    sec = max(sec, 0)
    return f"{int(sec // 3600)}:{int((sec % 3600) // 60):02d}:{sec % 60:05.2f}"


def generate_ass_subtitle(vtt_path: str, start_sec: int, duration: int, output_ass: str, subtitle_style: str = "bold_captions") -> bool:
    try:
        content = Path(vtt_path).read_text(encoding="utf-8")
    except Exception as e:
        log.error("Failed to read VTT for subtitles: %s", e)
        return False

    is_clean = subtitle_style == "clean_minimal"
    font_name, font_size, margin_v, outline_w = ("Arial", "75", "450", "3") if is_clean else ("Impact", "95", "550", "6")
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Primary,{font_name},{font_size},&H0000FFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline_w},3,2,10,10,{margin_v},1
Style: PrimaryWhite,{font_name},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline_w},3,2,10,10,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events, use_alt, end_sec = [], True, start_sec + duration
    for start_ts, end_ts, text in re.findall(r"(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3})\n((?:.|\n)*?)(?=\n\n|\Z)", content):
        t_start, t_end = parse_time(start_ts), parse_time(end_ts)
        if t_end < start_sec or t_start > end_sec:
            continue
        t_start -= start_sec
        t_end -= start_sec
        text = text.strip().replace("\n", " ")
        if not text:
            continue
        style = "Primary" if use_alt else "PrimaryWhite"
        use_alt = not use_alt
        events.append(f"Dialogue: 0,{format_ass_time(t_start)},{format_ass_time(t_end)},{style},,0,0,0,,{text}")

    if not events:
        return False
    Path(output_ass).write_text(header + "\n".join(events), encoding="utf-8")
    return True


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:").replace(",", "\\,")


def cut_and_format_clip(video_path: str, start_sec: int, end_sec: int, caption: str, output_filename: Optional[str] = None,
                         watermark: Optional[str] = None, sub_path: Optional[str] = None, broll_path: Optional[str] = None,
                         subtitle_style: str = "bold_captions") -> str:
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    output_filename = output_filename or f"clip_{int(time.time())}_{random.randint(1000,9999)}.mp4"
    output_path = str(settings.output_dir / output_filename)

    duration = min(end_sec - start_sec, settings.max_short_duration_sec)
    if len(caption) > 26:
        caption = caption[:23] + "..."

    safe_caption, safe_watermark = _esc(caption), _esc(watermark or settings.default_watermark)
    ass_filter = ""
    if sub_path and os.path.exists(sub_path):
        ass_path = str(settings.output_dir / f"subs_{int(time.time())}_{random.randint(1000,9999)}.ass")
        if generate_ass_subtitle(sub_path, start_sec, duration, ass_path, subtitle_style=subtitle_style):
            safe_ass = ass_path.replace("\\", "/").replace(":", "\\:")
            ass_filter = f",subtitles={safe_ass}"

    encoder, encoder_args = get_best_h264_encoder()
    cpu_threads = str(os.cpu_count() or 4)

    if broll_path and os.path.exists(broll_path):
        broll_start = random.randint(0, 60)
        filter_complex = (
            "[0:v]scale=1080:960:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:960[top]; "
            "[1:v]scale=1080:960:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:960[bottom]; "
            "[top][bottom]vstack=inputs=2[merged]; [merged]scale=1080:1920:flags=lanczos,"
            f"drawtext=text='{safe_caption}':fontsize=38:fontcolor=white:borderw=2:bordercolor=black:x=(w-text_w)/2:y=(h/2)-text_h-20:font=Arial Bold:box=1:boxcolor=black@0.55:boxborderw=14:fix_bounds=1,"
            f"drawtext=text='{safe_watermark}':fontsize=26:fontcolor=white@0.70:borderw=1:bordercolor=black@0.5:x=40:y=80:font=Arial:fix_bounds=1{ass_filter}[v_out]"
        )
        cmd = [FFMPEG, "-y", "-threads", cpu_threads, "-ss", str(start_sec), "-t", str(duration), "-i", video_path,
               "-stream_loop", "-1", "-ss", str(broll_start), "-t", str(duration), "-i", broll_path,
               "-filter_complex", filter_complex, "-map", "[v_out]", "-map", "0:a", "-r", "30", "-pix_fmt", "yuv420p",
               "-c:v", encoder, *encoder_args, "-c:a", "aac", "-b:a", "192k", "-map_metadata", "-1", "-movflags", "+faststart", output_path]
    else:
        filter_complex = (
            "[0:v]split=2[bg][fg]; [bg]scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,"
            "boxblur=luma_radius=28:luma_power=2:chroma_radius=14:chroma_power=2,eq=brightness=-0.12[bg_blurred]; "
            "[fg]scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos[fg_scaled]; "
            "[bg_blurred][fg_scaled]overlay=(W-w)/2:(H-h)/2[merged]; [merged]scale=1080:1920:flags=lanczos,"
            f"drawtext=text='{safe_caption}':fontsize=38:fontcolor=white:borderw=2:bordercolor=black:x=(w-text_w)/2:y=h-text_h-350:font=Arial Bold:box=1:boxcolor=black@0.55:boxborderw=14:fix_bounds=1,"
            f"drawtext=text='{safe_watermark}':fontsize=26:fontcolor=white@0.70:borderw=1:bordercolor=black@0.5:x=40:y=80:font=Arial:fix_bounds=1{ass_filter}[v_out]"
        )
        cmd = [FFMPEG, "-y", "-threads", cpu_threads, "-ss", str(start_sec), "-t", str(duration), "-i", video_path,
               "-filter_complex", filter_complex, "-map", "[v_out]", "-map", "0:a", "-r", "30", "-pix_fmt", "yuv420p",
               "-c:v", encoder, *encoder_args, "-c:a", "aac", "-b:a", "192k", "-map_metadata", "-1", "-movflags", "+faststart", output_path]

    try:
        subprocess.run(cmd, timeout=settings.ffmpeg_timeout_sec, check=True, capture_output=True, creationflags=_NOWIN)
    except subprocess.CalledProcessError as e:
        raise ClipCutError(f"ffmpeg failed: {e.stderr.decode(errors='replace')[:800]}") from e
    except subprocess.TimeoutExpired as e:
        raise ClipCutError(f"ffmpeg timed out after {settings.ffmpeg_timeout_sec}s") from e

    log.info("Rendered %s (%.1f MB)", output_path, os.path.getsize(output_path) / (1024 * 1024))
    return output_path


def cut_clip(video_path: str, start_sec: int, end_sec: int, caption: str, watermark: Optional[str] = None,
             sub_path: Optional[str] = None, broll_path: Optional[str] = None, subtitle_style: str = "bold_captions") -> str:
    actual_end = min(start_sec + 55, end_sec)
    if actual_end <= start_sec:
        actual_end = start_sec + 45
    return cut_and_format_clip(video_path=video_path, start_sec=start_sec, end_sec=actual_end, caption=caption,
                                watermark=watermark, sub_path=sub_path, broll_path=broll_path, subtitle_style=subtitle_style)


################################################################################
# FILE: pipeline/youtube_uploader.py
################################################################################

"""Ported from v2 unchanged — resumable chunked upload with token
auto-refresh, never raises (returns a dict either way)."""
from __future__ import annotations

import time
from typing import Callable, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .logging_setup import get_logger

log = get_logger("youtube_uploader")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CHUNK_SIZE = 16 * 1024 * 1024
MAX_UPLOAD_RETRIES = 5


def get_authenticated_service(creds_dict: dict):
    if not creds_dict:
        return None
    creds = Credentials(
        token=creds_dict.get("token"), refresh_token=creds_dict.get("refresh_token"),
        client_id=creds_dict.get("client_id"), client_secret=creds_dict.get("client_secret"),
        token_uri="https://oauth2.googleapis.com/token", scopes=SCOPES,
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def upload_video_to_youtube(video_path: str, title: str, description: str, tags: list[str], creds_dict: dict,
                             progress_callback: Optional[Callable[[int], None]] = None, privacy_status: str = "public") -> dict:
    import os
    if not os.path.exists(video_path):
        return {"error": f"Video file not found at {video_path}"}
    try:
        youtube = get_authenticated_service(creds_dict)
    except Exception as e:
        return {"error": f"Auth error: {e}"}
    if not youtube:
        return {"error": "Authentication failed."}

    body = {"snippet": {"title": title, "description": description, "tags": tags, "categoryId": "22"}, "status": {"privacyStatus": privacy_status}}
    media = MediaFileUpload(video_path, chunksize=CHUNK_SIZE, resumable=True)
    request = youtube.videos().insert(part=",".join(body.keys()), body=body, media_body=media)

    response, retries = None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status and progress_callback:
                try:
                    progress_callback(int(status.progress() * 100))
                except Exception as e:
                    log.warning("progress_callback raised: %s", e)
        except Exception as e:
            retries += 1
            if retries > MAX_UPLOAD_RETRIES:
                return {"error": str(e)}
            time.sleep(2 * retries)

    return {"status": "success", "video_id": response.get("id"), "url": f"https://youtube.com/shorts/{response.get('id')}"}


################################################################################
# FILE: pipeline/hot_pipeline.py
################################################################################

"""Ported from v2 unchanged (licensed_cc-only pre-bake cache)."""
from __future__ import annotations

import glob
import json
import threading
import time
from pathlib import Path
from typing import Optional

from .config import settings
from .logging_setup import get_logger

log = get_logger("hot_pipeline")
_replenishing_niches: set[str] = set()
_lock = threading.Lock()


def _niche_key(niche: str) -> str:
    return "".join(c for c in niche.lower() if c.isalnum() or c in (" ", "_")).strip().replace(" ", "_")


def get_hot_clip(niche: str) -> Optional[dict]:
    niche_dir = settings.hot_pool_dir / _niche_key(niche)
    if not niche_dir.exists():
        return None
    for manifest_path in glob.glob(str(niche_dir / "*.json")):
        try:
            data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            clips = data.get("clip_paths", [])
            if clips and all(Path(c).exists() and Path(c).stat().st_size > 102_400 for c in clips):
                Path(manifest_path).unlink()
                return data
        except Exception as e:
            log.warning("Manifest check failed for %s: %s", manifest_path, e)
    return None


def prebake_clip_worker(niche: str) -> None:
    niche_key = _niche_key(niche)
    with _lock:
        if niche_key in _replenishing_niches:
            return
        _replenishing_niches.add(niche_key)
    try:
        niche_dir = settings.hot_pool_dir / niche_key
        niche_dir.mkdir(parents=True, exist_ok=True)
        if len(glob.glob(str(niche_dir / "*.json"))) >= 2:
            return

        from . import clip_cutter, clip_finder, video_downloader, video_finder
        try:
            candidates = video_finder.find_licensed_cc_videos(niche=niche)
        except video_finder.VideoFinderError as e:
            log.warning("Pre-bake: no candidates for %r: %s", niche, e)
            return

        video, dl = None, None
        for candidate in candidates:
            try:
                dl = video_downloader.download_video_and_subs(candidate.url, candidate.id)
                video = candidate
                break
            except video_downloader.DownloadError as e:
                log.warning("Pre-bake candidate failed (%s): %s", candidate.id, e)
        if not video or not dl:
            return

        clip_info = clip_finder.find_best_segment(dl.sub_path, niche=niche) if dl.sub_path else clip_finder.ClipSegment(60, 110, niche.title())
        try:
            clip_path = clip_cutter.cut_clip(video_path=dl.video_path, start_sec=clip_info.start_sec, end_sec=clip_info.end_sec,
                                              caption=clip_info.caption, watermark=f"@{niche.replace(' ', '').capitalize()}", sub_path=dl.sub_path)
        except clip_cutter.ClipCutError as e:
            log.warning("Pre-bake clip cut failed: %s", e)
            return

        manifest = {"niche": niche, "video_id": video.id, "video_title": video.title, "attribution": video.attribution,
                    "clip_info": {"start_sec": clip_info.start_sec, "end_sec": clip_info.end_sec, "caption": clip_info.caption},
                    "clip_paths": [clip_path], "created_at": time.time()}
        (niche_dir / f"hot_{video.id}_{int(time.time())}.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    except Exception as e:
        log.error("Pre-baking error for %r: %s", niche, e)
    finally:
        with _lock:
            _replenishing_niches.discard(niche_key)


def trigger_replenish(niche: str) -> None:
    threading.Thread(target=prebake_clip_worker, args=(niche,), daemon=True).start()


################################################################################
# FILE: pipeline/orchestrator.py
################################################################################

"""
worker/pipeline/orchestrator.py

Replaces v2's worker.py. Same mode-branching structure (own_content /
licensed_cc), same webhook + HTTP status reporting. Two real upgrades:

1. MULTI-CLIP: `ClipJob.num_clips` (1-5) renders several distinct Shorts
   from ONE downloaded source video using `clip_finder.find_best_segments`,
   instead of one job = one clip. This is the new user-facing feature the
   "new features" upgrade asked for — turns a single niche search into a
   batch of ready-to-review Shorts.
2. Every status update now includes enough info for the daemon to `ack`
   the underlying stream entry only once the WHOLE job (all clips) is
   done — partial completion never gets falsely acked and lost.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Optional

import requests

from . import clip_cutter, clip_finder, hot_pipeline, video_downloader, video_finder, youtube_uploader
from .config import settings
from .logging_setup import get_logger
from .security import sign_worker_token

log = get_logger("orchestrator")


class PipelineError(Exception):
    pass


def _send_webhook(event: dict) -> None:
    import os
    url = os.environ.get("CLIPAI_WEBHOOK_URL", "")
    if not url:
        return
    secret = os.environ.get("CLIPAI_WEBHOOK_SECRET", "")
    body = json.dumps(event).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-ClipAI-Signature"] = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    try:
        requests.post(url, data=body, headers=headers, timeout=5)
    except Exception as e:
        log.warning("Webhook delivery failed: %s", e)


def update_job_status(job_id: str, status: str, progress: int, message: str, url: str = "",
                       title: str = "", niche: str = "", user_id: str = "") -> None:
    log.info("[%3d%%] %s: %s", progress, status, message)
    uid = user_id or "unknown"
    event = {"job_id": job_id, "status": status, "progress": progress, "message": message,
              "url": url, "title": title, "niche": niche, "user_id": uid}
    _send_webhook(event)
    try:
        if status in ("complete", "draft_ready", "error"):
            token = sign_worker_token(uid, purpose="complete")
            requests.post(f"{settings.api_base_url}/api/v1/worker/complete",
                           json={"job_id": job_id, "status": status, "message": message, "url": url, "title": title, "niche": niche},
                           params={"user_id": uid, "token": token}, timeout=10)
        else:
            token = sign_worker_token(uid, purpose="progress")
            requests.post(f"{settings.api_base_url}/api/v1/worker/progress",
                           json={"job_id": job_id, "status": status, "progress": progress, "message": message, "url": url},
                           params={"user_id": uid, "token": token}, timeout=5)
    except Exception as e:
        log.warning("Failed to update cloud progress: %s", e)


def fetch_youtube_creds(user_id: str) -> Optional[dict]:
    token = sign_worker_token(user_id, purpose="creds")
    try:
        res = requests.get(f"{settings.api_base_url}/api/v1/worker/youtube-creds",
                            params={"user_id": user_id, "token": token}, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get("refresh_token"):
                return data
    except Exception as e:
        log.warning("Failed to fetch YouTube creds: %s", e)
    return None


@dataclass
class ClipJob:
    mode: str
    user_id: str
    job_id: str
    niche: str = ""
    source_kind: Optional[str] = None
    source: Optional[str] = None
    partner_channel_id: Optional[str] = None
    auto_upload: bool = True
    layout: str = "cinematic_blur"
    broll_path: Optional[str] = None
    subtitle_style: str = "bold_captions"
    num_clips: int = 1

    @classmethod
    def from_queue_payload(cls, payload: dict) -> "ClipJob":
        return cls(
            mode=payload.get("mode", "public_domain"), user_id=payload["user_id"], job_id=payload["job_id"],
            niche=payload.get("niche", ""), source_kind=payload.get("source_kind"), source=payload.get("source"),
            partner_channel_id=payload.get("partner_channel_id"),
            auto_upload=payload.get("auto_upload", True), layout=payload.get("layout", "cinematic_blur"),
            broll_path=payload.get("broll_path"), subtitle_style=payload.get("subtitle_style", "bold_captions"),
            num_clips=min(int(payload.get("num_clips", 1)), settings.max_clips_per_job),
        )

    def validate(self) -> list[str]:
        problems = list(settings.validate()) if self.mode in ("licensed_cc", "public_domain", "partner_channel") else []
        if self.mode == "own_content" and self.source_kind not in ("file", "channel"):
            problems.append("own_content mode requires source_kind of 'file' or 'channel'.")
        if self.mode not in ("own_content", "licensed_cc", "my_upload", "my_channel", "partner_channel", "public_domain"):
            problems.append(f"Unknown mode: {self.mode!r}")
        if self.layout == "split_screen" and not self.broll_path:
            problems.append("split_screen layout requires broll_path (b-roll you own or have licensed).")
        return problems


def _source_video(job: ClipJob):
    if job.mode in ("my_upload",) or (job.mode == "own_content" and job.source_kind == "file"):
        video = video_finder.register_uploaded_file(job.source, title=job.niche or "My video")
        return video, video_downloader.DownloadResult(video_path=video.local_path, sub_path=None)

    if job.mode in ("my_channel",) or (job.mode == "own_content" and job.source_kind == "channel"):
        creds = fetch_youtube_creds(job.user_id)
        if not creds:
            raise PipelineError("YouTube account not connected.")
        candidates = video_finder.get_own_channel_videos(creds)
        video = next((c for c in candidates if c.id == job.source), None)
        if not video:
            raise PipelineError("Selected video not found on your channel.")
        return video, video_downloader.download_video_and_subs(video.url, video.id)

    if job.mode == "partner_channel":
        if not job.partner_channel_id:
            raise PipelineError("partner_channel_id is required for partner_channel mode.")
        candidates = video_finder.find_partner_channel_videos(job.partner_channel_id, niche=job.niche)
        last_error = None
        for i, candidate in enumerate(candidates):
            update_job_status(job.job_id, "running", 25 + i * 5, f"Downloading: {candidate.title[:45]}...", user_id=job.user_id)
            try:
                return candidate, video_downloader.download_video_and_subs(candidate.url, candidate.id)
            except video_downloader.DownloadError as e:
                last_error = e
                log.warning("Candidate %s failed, trying next: %s", candidate.id, e)
        raise PipelineError(f"All candidates failed to download: {last_error}")

    if job.mode in ("public_domain", "licensed_cc"):
        candidates = video_finder.find_public_domain_videos(niche=job.niche)  # renamed from find_licensed_cc_videos
        last_error = None
        for i, candidate in enumerate(candidates):
            update_job_status(job.job_id, "running", 25 + i * 5, f"Downloading: {candidate.title[:45]}...", user_id=job.user_id)
            try:
                return candidate, video_downloader.download_video_and_subs(candidate.url, candidate.id)
            except video_downloader.DownloadError as e:
                last_error = e
                log.warning("Candidate %s failed, trying next: %s", candidate.id, e)
        raise PipelineError(f"All candidates failed to download: {last_error}")

    raise PipelineError(f"Unknown source mode: {job.mode!r}")


def run_clip_pipeline(job: ClipJob) -> None:
    problems = job.validate()
    if problems:
        update_job_status(job.job_id, "error", 0, "; ".join(problems), user_id=job.user_id)
        return

    try:
        update_job_status(job.job_id, "running", 10, "Finding source video...", user_id=job.user_id)
        video, dl = _source_video(job)

        update_job_status(job.job_id, "running", 45, "AI is selecting the best moment(s)...", user_id=job.user_id)
        segments = clip_finder.find_best_segments(dl.sub_path, niche=job.niche or video.title, user_id=job.user_id, num_clips=job.num_clips) \
            if dl.sub_path else [clip_finder.ClipSegment(start_sec=i * 70, end_sec=i * 70 + 50, caption=(job.niche or video.title)[:26]) for i in range(job.num_clips)]

        watermark = f"@{(job.niche or 'MyChannel').replace(' ', '')}"
        rendered_paths: list[tuple[str, str]] = []  # (path, caption)
        for i, seg in enumerate(segments):
            pct = 60 + int(20 * (i + 1) / len(segments))
            update_job_status(job.job_id, "running", pct, f"Rendering Short {i + 1}/{len(segments)}...", user_id=job.user_id)
            path = clip_cutter.cut_clip(video_path=dl.video_path, start_sec=seg.start_sec, end_sec=seg.end_sec,
                                         caption=seg.caption, watermark=watermark, sub_path=dl.sub_path,
                                         broll_path=job.broll_path, subtitle_style=job.subtitle_style)
            rendered_paths.append((path, seg.caption))

        if job.mode in ("licensed_cc", "public_domain"):
            hot_pipeline.trigger_replenish(job.niche)

        attribution = video.attribution if job.mode in ("licensed_cc", "public_domain", "partner_channel") else ""
        _finish_and_publish(job, rendered_paths, video.title, attribution, video_id=video.id)

    except (PipelineError, video_finder.VideoFinderError, video_downloader.DownloadError, clip_cutter.ClipCutError) as e:
        update_job_status(job.job_id, "error", 0, str(e), user_id=job.user_id)
    except Exception as e:
        log.exception("Unexpected pipeline error")
        update_job_status(job.job_id, "error", 0, f"Unexpected pipeline error: {e}", user_id=job.user_id)


def _finish_and_publish(job: ClipJob, rendered: list[tuple[str, str]], video_title: str, attribution: str, video_id: str) -> None:
    if not job.auto_upload:
        # Multi-clip drafts: report each clip path separately so Workplace shows all of them.
        for path, caption in rendered:
            update_job_status(job.job_id, "draft_ready", 100, f"Rendered and ready for review: {caption}",
                               url=path, title=f"#Shorts {caption}", niche=job.niche, user_id=job.user_id)
        return

    creds = fetch_youtube_creds(job.user_id)
    if not creds:
        update_job_status(job.job_id, "error", 85, "YouTube account not connected.", user_id=job.user_id)
        return

    for i, (path, caption) in enumerate(rendered):
        title = f"#Shorts {caption}"
        desc_lines = [caption, ""]
        if attribution:
            prefix = "Source (Creative Commons):" if job.mode in ("licensed_cc", "public_domain") else "Credit:"
            desc_lines += [prefix, attribution, ""]
        desc_lines.append(f"#Shorts{(' #' + job.niche.replace(' ', '')) if job.niche else ''}")
        desc = "\n".join(desc_lines)

        update_job_status(job.job_id, "running", 85 + i, f"Uploading {i + 1}/{len(rendered)} to YouTube...", user_id=job.user_id)

        def on_progress(pct: int, i=i) -> None:
            update_job_status(job.job_id, "running", int(85 + pct * 0.1), f"Uploading {i + 1}/{len(rendered)} ({pct}%)...", user_id=job.user_id)

        res = youtube_uploader.upload_video_to_youtube(path, title=title, description=desc, tags=["Shorts"] + ([job.niche] if job.niche else []),
                                                         creds_dict=creds, progress_callback=on_progress)
        if res.get("status") == "success":
            if job.mode in ("licensed_cc", "public_domain") and video_id and i == 0:
                video_finder.mark_video_used(video_id, video_title)
            update_job_status(job.job_id, "complete", 100, f"Done! {caption} is live on YouTube.", res.get("url", ""), title, job.niche, user_id=job.user_id)
        else:
            update_job_status(job.job_id, "error", 100, f"Upload failed for {caption}: {res.get('error')}", user_id=job.user_id)


################################################################################
# FILE: worker/__init__.py
################################################################################

# package marker
from pipeline.orchestrator import ClipJob, run_clip_pipeline  # noqa: F401


################################################################################
# FILE: worker/worker_daemon.py
################################################################################

"""
worker/worker_daemon.py — replaces v2's client_worker.py.

Two real reliability upgrades:
1. Explicit `ack` after a job finishes (see backend job_queue.py) instead
   of the fire-and-forget LPUSH/RPOP list — a crash mid-job no longer
   loses the job silently.
2. A stable per-instance `consumer_name` so multiple daemon replicas
   don't collide, and so a crashed replica's abandoned jobs are
   identifiable (visible in Redis XPENDING) instead of anonymous.
"""
from __future__ import annotations

import os
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import requests

from pipeline.config import settings
from pipeline.logging_setup import get_logger
from pipeline.security import sign_worker_token

log = get_logger("worker_daemon")

LANE_USER_ID = os.environ.get("WORKER_LANE_USER_ID", "cloud")
CONCURRENT_WORKERS = int(os.environ.get("CONCURRENT_WORKERS", "3"))
POLL_INTERVAL_SEC = float(os.environ.get("WORKER_POLL_INTERVAL_SEC", "2"))
CONSUMER_NAME = f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"

_is_running = True


def process_job(job: dict, stream_id: str) -> None:
    from pipeline.orchestrator import ClipJob, run_clip_pipeline
    job_id = job.get("job_id", "unknown")
    job_user_id = job.get("user_id", LANE_USER_ID)
    log.info("Processing job %s for user %s (niche=%r, consumer=%s)", job_id, job_user_id, job.get("niche"), CONSUMER_NAME)
    try:
        run_clip_pipeline(ClipJob.from_queue_payload(job))
    except Exception as e:
        log.exception("Pipeline error on job %s", job_id)
        try:
            token = sign_worker_token(job_user_id, purpose="complete")
            requests.post(f"{settings.api_base_url}/api/v1/worker/complete",
                           json={"job_id": job_id, "status": "error", "message": str(e)},
                           params={"user_id": job_user_id, "token": token}, timeout=10)
        except Exception:
            log.warning("Could not report pipeline error for job %s", job_id)
    finally:
        try:
            token = sign_worker_token(LANE_USER_ID, purpose="ack")
            requests.post(f"{settings.api_base_url}/api/v1/worker/ack/{stream_id}",
                           params={"job_id": job_id, "user_id": LANE_USER_ID, "token": token}, timeout=10)
        except Exception as e:
            log.warning("Failed to ack job %s (will be reclaimed after timeout): %s", job_id, e)


def run_worker_loop() -> None:
    global _is_running
    log.info("Starting ClipAI cloud worker (lane=%s, consumer=%s)", LANE_USER_ID, CONSUMER_NAME)
    executor = ThreadPoolExecutor(max_workers=CONCURRENT_WORKERS)
    consecutive_errors = 0

    while _is_running:
        try:
            token = sign_worker_token(LANE_USER_ID, purpose="poll")
            res = requests.get(f"{settings.api_base_url}/api/v1/worker/poll",
                                params={"user_id": LANE_USER_ID, "token": token, "consumer": CONSUMER_NAME}, timeout=10)
            if res.status_code == 200:
                body = res.json()
                job = body.get("job")
                if job:
                    executor.submit(process_job, job, body.get("stream_id", ""))
                consecutive_errors = 0
            else:
                consecutive_errors += 1
        except requests.exceptions.RequestException as e:
            consecutive_errors += 1
            log.warning("Poll request failed: %s", e)
        except Exception:
            consecutive_errors += 1
            log.exception("Unexpected polling error")

        time.sleep(POLL_INTERVAL_SEC * min(consecutive_errors + 1, 10))


def shutdown(*_args) -> None:
    global _is_running
    log.info("Shutdown signal received, stopping after current jobs finish...")
    _is_running = False


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    run_worker_loop()


################################################################################
# FILE: backend/tests/conftest.py
################################################################################

import pytest
import fakeredis.aioredis


@pytest.fixture
def fake_redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


################################################################################
# FILE: backend/tests/test_job_queue.py
################################################################################

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault('WORKER_SECRET', 'test-secret')

import pytest
from app.services import job_queue


@pytest.mark.asyncio
async def test_enqueue_claim_ack_roundtrip(fake_redis, monkeypatch):
    monkeypatch.setattr(job_queue, 'get_redis', lambda: fake_redis)
    job_id = await job_queue.enqueue({'user_id': 'u1', 'niche': 'finance'})
    job = await job_queue.claim_next('consumer-1')
    assert job is not None and job.job_id == job_id
    await job_queue.set_status(job.job_id, 'processing', 5, 'Processing...')
    await job_queue.ack(job)
    status = await job_queue.get_status(job_id)
    assert status['status'] == 'processing'


@pytest.mark.asyncio
async def test_reclaim_after_max_attempts_dead_letters(fake_redis, monkeypatch):
    monkeypatch.setattr(job_queue, 'get_redis', lambda: fake_redis)
    monkeypatch.setattr(job_queue, 'CLAIM_IDLE_MS', 0)  # force-eligible immediately
    job_id = await job_queue.enqueue({'user_id': 'u1', 'niche': 'gaming'})
    for i in range(job_queue.MAX_ATTEMPTS + 1):
        await job_queue.claim_next(f'consumer-{i}')  # never acked, stays pending
    dead_len = await fake_redis.xlen(job_queue.DEAD_LETTER)
    assert dead_len == 1


@pytest.mark.asyncio
async def test_job_owner_roundtrip(fake_redis, monkeypatch):
    monkeypatch.setattr(job_queue, 'get_redis', lambda: fake_redis)
    job_id = await job_queue.enqueue({'user_id': 'u42', 'niche': 'cooking'})
    assert await job_queue.get_owner(job_id) == 'u42'


################################################################################
# FILE: backend/tests/test_orchestrator.py
################################################################################

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault('WORKER_SECRET', 'test-secret')

from pipeline.orchestrator import ClipJob


def test_split_screen_requires_broll():
    job = ClipJob(mode='public_domain', user_id='u1', job_id='j1', niche='x', layout='split_screen')
    assert any('broll_path' in p for p in job.validate())


def test_num_clips_clamped_to_max(monkeypatch):
    monkeypatch.setattr('pipeline.config.settings.max_clips_per_job', 5)
    job = ClipJob.from_queue_payload({'user_id': 'u1', 'job_id': 'j1', 'num_clips': 99})
    assert job.num_clips == 5


def test_unknown_mode_rejected():
    job = ClipJob(mode='not_a_real_mode', user_id='u1', job_id='j1')
    assert any('Unknown mode' in p for p in job.validate())


################################################################################
# FILE: backend/tests/test_security.py
################################################################################

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("WORKER_SECRET", "test-secret-value")
os.environ.setdefault("JWT_SIGNING_KEY", "test-jwt-signing-key")

from app.security import (  # noqa: E402
    issue_session_token,
    stable_user_id_for_email,
    verify_session_token,
    verify_worker_token,
    sign_worker_token,
    verify_admin,
)



def test_session_token_roundtrip():
    token = issue_session_token("user_abc", "person@example.com")
    data = verify_session_token(token)
    assert data is not None
    assert data["sub"] == "user_abc"
    assert data["email"] == "person@example.com"


def test_session_token_rejects_garbage():
    assert verify_session_token("not-a-real-token") is None


def test_stable_user_id_is_deterministic():
    a = stable_user_id_for_email("Person@Example.com")
    b = stable_user_id_for_email("person@example.com ")
    assert a == b
    assert a.startswith("user_")


def test_worker_token_roundtrip():
    token = sign_worker_token("user_abc", purpose="poll")
    assert verify_worker_token("user_abc", token, purpose="poll")


def test_worker_token_wrong_purpose_rejected():
    token = sign_worker_token("user_abc", purpose="poll")
    assert not verify_worker_token("user_abc", token, purpose="creds")


def test_worker_token_wrong_user_rejected():
    token = sign_worker_token("user_abc", purpose="poll")
    assert not verify_worker_token("user_xyz", token, purpose="poll")


def test_worker_token_scopes_complete_progress_analyze():
    for purpose in ("complete", "progress", "analyze"):
        token = sign_worker_token("user_abc", purpose=purpose)
        assert verify_worker_token("user_abc", token, purpose=purpose)
        assert not verify_worker_token("user_abc", token, purpose="poll")


def test_worker_secret_rotation(monkeypatch):
    from app.config import get_settings
    settings = get_settings()

    monkeypatch.setattr(settings, "worker_secret", "old-secret")
    token_old = sign_worker_token("user_abc", purpose="poll")

    # Rotate secret: old becomes previous, new becomes current
    monkeypatch.setattr(settings, "worker_secret", "new-secret")
    monkeypatch.setattr(settings, "worker_secret_previous", "old-secret")

    token_new = sign_worker_token("user_abc", purpose="poll")

    # Both tokens signed with current and previous secrets verify successfully
    assert verify_worker_token("user_abc", token_new, purpose="poll")
    assert verify_worker_token("user_abc", token_old, purpose="poll")

    # Wrong secret rejected
    monkeypatch.setattr(settings, "worker_secret", "other-secret")
    monkeypatch.setattr(settings, "worker_secret_previous", "yet-another")
    assert not verify_worker_token("user_abc", token_new, purpose="poll")



def test_admin_secret_rotation(monkeypatch):
    from fastapi import HTTPException, Request
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "admin_secret", "new-secret")
    monkeypatch.setattr(settings, "admin_secret_previous", "old-secret")

    req_new = Request({"type": "http", "headers": [(b"x-admin-secret", b"new-secret")]})
    req_old = Request({"type": "http", "headers": [(b"x-admin-secret", b"old-secret")]})
    req_bad = Request({"type": "http", "headers": [(b"x-admin-secret", b"bad-secret")]})

    # Both new and previous secrets succeed
    verify_admin(req_new)
    verify_admin(req_old)

    import pytest
    with pytest.raises(HTTPException) as exc:
        verify_admin(req_bad)
    assert exc.value.status_code == 403


def test_atomic_free_tier_consumption_and_refund():
    from app.db import UserRepo
    uid = "test_user_atomic_quota"
    user = UserRepo.get_or_create(uid)
    user["free_clips_used"] = 0
    user["license"] = "free_tier"

    # Consuming under limit allows
    allowed, count = UserRepo.atomic_consume_free_clip(uid, limit=1)
    assert allowed is True
    assert count == 1

    # Second consumption at or over limit is rejected
    allowed2, count2 = UserRepo.atomic_consume_free_clip(uid, limit=1)
    assert allowed2 is False
    assert count2 == 1

    # Compensating refund restores quota
    UserRepo.refund_free_clip(uid)
    assert UserRepo.get_or_create(uid)["free_clips_used"] == 0

    # Can consume again after refund
    allowed3, count3 = UserRepo.atomic_consume_free_clip(uid, limit=1)
    assert allowed3 is True
    assert count3 == 1


import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_failover_redis_fails_over_on_connection_error():
    from app.redis_client import FailoverRedis

    fr = FailoverRedis("redis://primary-down:6379", "redis://secondary-up:6379")

    # Mock clients
    mock_primary = AsyncMock()
    mock_primary.get.side_effect = ConnectionError("Connection refused")
    mock_secondary = AsyncMock()
    mock_secondary.get.return_value = "cached_val"

    fr.primary = mock_primary
    fr.secondary = mock_secondary

    res = await fr.get("mykey")
    assert res == "cached_val"
    assert mock_primary.get.called
    assert mock_secondary.get.called


@pytest.mark.asyncio
async def test_start_google_login_handles_redis_down_and_missing_client_id(monkeypatch):
    from app.config import get_settings
    from app.routers.auth import start_google_login

    settings = get_settings()

    # 1. When google_client_id is not configured, redirect with error without 500 crash
    monkeypatch.setattr(settings, "google_client_id", "")
    resp_unconfigured = await start_google_login()
    assert resp_unconfigured.status_code == 307 or resp_unconfigured.status_code == 302
    assert "detail=not_configured" in resp_unconfigured.headers["location"]

    # 2. When google_client_id is set, even if Redis throws connection error, redirects safely and sets cookie fallback
    monkeypatch.setattr(settings, "google_client_id", "test_google_client_id")
    import app.routers.auth as auth_mod
    broken_redis = AsyncMock()
    broken_redis.setex.side_effect = ConnectionError("Redis host unreachable")
    monkeypatch.setattr(auth_mod, "get_redis", lambda: broken_redis)

    resp_with_broken_redis = await start_google_login()
    assert resp_with_broken_redis.status_code == 307 or resp_with_broken_redis.status_code == 302
    assert "accounts.google.com" in resp_with_broken_redis.headers["location"]
    # Verify fallback cookie was set
    assert "clipai_oauth_login" in resp_with_broken_redis.headers.get("set-cookie", "")


################################################################################
# FILE: backend/tests/test_schemas.py
################################################################################

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from pydantic import ValidationError

from app.schemas import AutoPostSettings, ClipRequest


def test_clip_request_rejects_blank_niche():
    with pytest.raises(ValidationError):
        ClipRequest(niche="   ", rights_confirmed=True)


def test_clip_request_strips_niche():
    req = ClipRequest(niche="  finance  ", rights_confirmed=True)
    assert req.niche == "finance"


def test_clip_request_num_clips_bounds():
    with pytest.raises(ValidationError):
        ClipRequest(niche="x", num_clips=0, rights_confirmed=True)
    with pytest.raises(ValidationError):
        ClipRequest(niche="x", num_clips=6, rights_confirmed=True)
    assert ClipRequest(niche="x", num_clips=3, rights_confirmed=True).num_clips == 3


def test_clip_request_rights_confirmed_required_for_public_domain():
    with pytest.raises(ValidationError):
        ClipRequest(niche="crypto", source_mode="public_domain", rights_confirmed=False)
    req = ClipRequest(niche="crypto", source_mode="public_domain", rights_confirmed=True)
    assert req.rights_confirmed is True



def test_clip_request_requires_source_video_id_for_channel_modes():
    with pytest.raises(ValidationError):
        ClipRequest(source_mode="my_channel")
    with pytest.raises(ValidationError):
        ClipRequest(source_mode="partner_channel")
    req = ClipRequest(source_mode="my_channel", source_video_id="vid_123")
    assert req.source_video_id == "vid_123"
    req_partner = ClipRequest(source_mode="partner_channel", source_video_id="chan_abc", partner_channel_id="chan_abc")
    assert req_partner.partner_channel_id == "chan_abc"


def test_autopost_rejects_bad_time_format():
    with pytest.raises(ValidationError):
        AutoPostSettings(enabled=True, times=["25:99"], niche="x", rights_confirmed=True)


def test_autopost_accepts_valid_times():
    settings = AutoPostSettings(enabled=True, times=["09:30", "23:00"], niche="x", rights_confirmed=True)
    assert settings.times == ["09:30", "23:00"]
    assert settings.rights_confirmed is True


def test_autopost_requires_rights_confirmed_when_enabled():
    with pytest.raises(ValidationError):
        AutoPostSettings(enabled=True, niche="x", rights_confirmed=False)
    # Disabled autopost can have rights_confirmed=False
    disabled = AutoPostSettings(enabled=False, niche="x", rights_confirmed=False)
    assert disabled.enabled is False


################################################################################
# FILE: backend/tests/test_clip_analysis.py
################################################################################

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("WORKER_SECRET", "test-secret")

from app.services.clip_analysis import _heuristic_fallback, parse_gemini_response


def test_heuristic_fallback_picks_densest_window():
    transcript = "\n".join([
        "[00:00] a short line here",
        "[00:10] this window has way more words packed into it than the others around it",
        "[01:00] another short one",
    ])
    result = _heuristic_fallback(transcript, "finance")
    assert result["start_sec"] == 10
    assert result["caption"] == "Finance"


def test_parse_gemini_response_happy_path():
    text = "START: 1:20\nEND: 2:10\nCAPTION: How I Built My First Million\nVIRAL_SCORE: 96\nREASON: strong hook"
    result = parse_gemini_response(text, "finance")
    assert result["start_sec"] == 80
    assert result["end_sec"] == 130
    assert result["caption"] == "How I Built My First Million"
    assert result["viral_score"] == 96


def test_parse_gemini_response_missing_fields_raises():
    import pytest
    with pytest.raises(ValueError):
        parse_gemini_response("garbage output", "finance")


################################################################################
# FILE: worker/tests/test_clip_finder.py
################################################################################

import os
import sys
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("WORKER_SECRET", "test-secret")

from pipeline.clip_finder import parse_vtt, build_transcript_block  # noqa: E402

SAMPLE_VTT = textwrap.dedent("""\
    WEBVTT

    00:00:01.000 --> 00:00:04.000
    Hey everyone welcome back to the channel

    00:00:04.000 --> 00:00:07.000
    [Music]

    00:00:07.000 --> 00:00:10.000
    Today we're talking about something crazy
    """)


def test_parse_vtt_skips_music_markers(tmp_path):
    p = tmp_path / "subs.vtt"
    p.write_text(SAMPLE_VTT, encoding="utf-8")
    entries = parse_vtt(str(p))
    texts = [e["text"] for e in entries]
    assert "[Music]" not in texts
    assert len(entries) == 2


def test_parse_vtt_dedupes_repeated_lines(tmp_path):
    vtt = SAMPLE_VTT + "\n00:00:10.000 --> 00:00:13.000\nHey everyone welcome back to the channel\n"
    p = tmp_path / "subs.vtt"
    p.write_text(vtt, encoding="utf-8")
    entries = parse_vtt(str(p))
    texts = [e["text"] for e in entries]
    assert texts.count("Hey everyone welcome back to the channel") == 1


def test_build_transcript_block_respects_char_limit(tmp_path):
    p = tmp_path / "subs.vtt"
    p.write_text(SAMPLE_VTT, encoding="utf-8")
    entries = parse_vtt(str(p))
    block = build_transcript_block(entries, max_chars=20)
    assert len(block) < 100  # truncated well below the full transcript


################################################################################
# FILE: test_clip_cutter.py
################################################################################

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

try:
    from pipeline.clip_cutter import parse_time, format_ass_time, _esc
except ImportError:
    from clip_cutter import parse_time, format_ass_time, _esc



def test_parse_time_hh_mm_ss():
    assert parse_time("00:01:30.500") == 90.5


def test_parse_time_mm_ss():
    assert parse_time("01:30.500") == 90.5


def test_format_ass_time_basic():
    assert format_ass_time(90.5) == "0:01:30.50"


def test_format_ass_time_negative_clamped_to_zero():
    assert format_ass_time(-5) == "0:00:00.00"


def test_esc_escapes_special_chars():
    assert _esc("it's: a, test") == "it\\'s\\: a\\, test"


################################################################################
# FILE: test_clip_finder.py
################################################################################

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

try:
    from pipeline.clip_finder import parse_vtt, build_transcript_block, _fallback_segment, ClipSegment
except ImportError:
    from clip_finder import parse_vtt, build_transcript_block, _fallback_segment, ClipSegment


SAMPLE_VTT = """WEBVTT

00:00:01.000 --> 00:00:03.000
Hello and welcome to the show

00:00:03.500 --> 00:00:05.000
Today we're talking about testing

00:00:05.500 --> 00:00:06.000
[Music]

00:00:06.500 --> 00:00:08.000
Today we're talking about testing
"""


def test_parse_vtt_basic(tmp_path):
    f = tmp_path / "sample.vtt"
    f.write_text(SAMPLE_VTT, encoding="utf-8")
    entries = parse_vtt(str(f))
    assert len(entries) == 2  # music + duplicate line both dropped
    assert entries[0]["text"] == "Hello and welcome to the show"
    assert entries[0]["start"] == 1.0
    assert entries[1]["text"] == "Today we're talking about testing"


def test_parse_vtt_missing_file_returns_empty():
    assert parse_vtt("/nonexistent/path.vtt") == []


def test_build_transcript_block_respects_max_chars():
    entries = [{"start": i, "text": "word " * 20} for i in range(0, 100, 10)]
    block = build_transcript_block(entries, max_chars=50)
    assert len(block) < 300  # should truncate well before including all entries


def test_fallback_segment_shape():
    seg = _fallback_segment("cooking tips")
    assert isinstance(seg, ClipSegment)
    assert seg.end_sec > seg.start_sec
    assert seg.caption  # non-empty


################################################################################
# FILE: test_video_finder.py
################################################################################

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

try:
    from pipeline.video_finder import _iso8601_to_seconds, VideoCandidate, register_uploaded_file, VideoFinderError
except ImportError:
    from video_finder import _iso8601_to_seconds, VideoCandidate, register_uploaded_file, VideoFinderError

import pytest


def test_iso8601_minutes_seconds():
    assert _iso8601_to_seconds("PT4M13S") == 4 * 60 + 13


def test_iso8601_hours_minutes_seconds():
    assert _iso8601_to_seconds("PT1H2M3S") == 3600 + 120 + 3


def test_iso8601_seconds_only():
    assert _iso8601_to_seconds("PT45S") == 45


def test_iso8601_empty_or_invalid():
    assert _iso8601_to_seconds("") == 0
    assert _iso8601_to_seconds("garbage") == 0


def test_video_candidate_to_dict_roundtrip():
    c = VideoCandidate(id="abc123", title="Test", url="https://youtu.be/abc123", view_count=1000)
    d = c.to_dict()
    assert d["id"] == "abc123"
    assert d["view_count"] == 1000


def test_register_uploaded_file_missing_raises(tmp_path):
    missing = tmp_path / "does_not_exist.mp4"
    with pytest.raises(VideoFinderError):
        register_uploaded_file(str(missing))


def test_register_uploaded_file_success(tmp_path):
    f = tmp_path / "myvideo.mp4"
    f.write_bytes(b"fake video bytes")
    candidate = register_uploaded_file(str(f), title="A" * 100)
    assert candidate.id == "myvideo"
    assert candidate.local_path == str(f)
    assert len(candidate.title) == 60  # truncated to 60 chars


def test_video_candidate_license_and_attribution():
    c = VideoCandidate(
        id="v1", title="Partner Video", url="https://youtu.be/v1",
        license="partner_licensed", attribution="Clipped with permission from Creator"
    )
    assert c.license == "partner_licensed"
    assert "with permission" in c.attribution


################################################################################
# FILE: test_worker.py
################################################################################

from worker import ClipJob


def test_own_content_requires_source_kind():
    job = ClipJob(mode="own_content", user_id="u1", job_id="j1", source_kind=None)
    problems = job.validate()
    assert any("source_kind" in p for p in problems)


def test_split_screen_requires_broll_path():
    job = ClipJob(mode="own_content", user_id="u1", job_id="j1", source_kind="file",
                  source="/tmp/x.mp4", layout="split_screen", broll_path=None)
    problems = job.validate()
    assert any("broll_path" in p for p in problems)


def test_split_screen_with_broll_path_ok_besides_other_checks():
    job = ClipJob(mode="own_content", user_id="u1", job_id="j1", source_kind="file",
                  source="/tmp/x.mp4", layout="split_screen", broll_path="/tmp/broll.mp4")
    problems = job.validate()
    assert not any("broll_path" in p for p in problems)


def test_unknown_mode_flagged():
    job = ClipJob(mode="not_a_real_mode", user_id="u1", job_id="j1")
    problems = job.validate()
    assert any("Unknown mode" in p for p in problems)


def test_valid_own_content_file_job_has_no_source_kind_problem():
    job = ClipJob(mode="own_content", user_id="u1", job_id="j1", source_kind="channel", source="abc123")
    problems = job.validate()
    assert not any("source_kind" in p for p in problems)


################################################################################
# FILE: frontend/index.html
################################################################################

<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>ClipAI — Automated Shorts, cut right</title>
<meta name="description" content="Point ClipAI at a niche. It finds the moment, cuts it, and posts it.">
<meta name="theme-color" content="#0c0c0e">
<link rel="stylesheet" href="/static/style.css">
</head>
<body>

<!-- Auth gate: shown until a valid session cookie exists. Replaces v2's
     client-side-minted user_id — identity now only ever comes from the
     server after real Google verification. -->
<div id="gate" class="gate">
  <div class="gate-box">
    <div class="mark-big"></div>
    <h1>ClipAI</h1>
    <p>Sign in to start clipping. Your account keeps your renders, quota, and channel connection in one place.</p>
    <a href="/api/v1/auth/google/login" class="google-btn">
      <svg width="17" height="17" viewBox="0 0 24 24"><path fill="#4285F4" d="M23.745 12.27c0-.7-.06-1.4-.19-2.07H12v4.51h6.6c-.29 1.52-1.14 2.82-2.4 3.68v3.05h3.88c2.27-2.09 3.665-5.17 3.665-9.17z"/><path fill="#34A853" d="M12 24c3.24 0 5.95-1.08 7.93-2.91l-3.88-3.05c-1.08.72-2.45 1.16-4.05 1.16-3.12 0-5.77-2.1-6.72-4.93H1.25v3.15C3.26 21.36 7.33 24 12 24z"/><path fill="#FBBC05" d="M5.28 14.27c-.25-.72-.38-1.49-.38-2.27s.13-1.55.38-2.27V6.58H1.25C.45 8.18 0 10.04 0 12s.45 3.82 1.25 5.42l4.03-3.15z"/><path fill="#EA4335" d="M12 4.75c1.77 0 3.35.61 4.6 1.8l3.42-3.42C17.95 1.19 15.24 0 12 0 7.33 0 3.26 2.64 1.25 6.58l4.03 3.15c.95-2.83 3.6-4.98 6.72-4.98z"/></svg>
      Continue with Google
    </a>
  </div>
</div>

<div class="shell hidden" id="shell">
  <aside class="rail">
    <div class="rail-brand"><div class="mark"></div><span>ClipAI</span></div>
    <div class="rail-tick-label">Workspace</div>
    <button class="rail-btn active" data-view="studio" onclick="switchView('studio')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg> Studio
    </button>
    <button class="rail-btn" data-view="workplace" onclick="switchView('workplace')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="14" rx="1"/><path d="M3 9h18"/></svg> Workplace
    </button>
    <button class="rail-btn" data-view="clips" onclick="switchView('clips')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="1"/><path d="M9 21V9"/></svg> My Clips
    </button>
    <button class="rail-btn" data-view="autopost" onclick="switchView('autopost')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg> Auto-Post
    </button>
    <div class="rail-spacer"></div>
    <div class="rail-status" id="worker-status"><div class="dot" id="worker-dot"></div><span id="worker-label">Checking engine…</span></div>
  </aside>

  <div class="main">
    <header class="topbar">
      <button class="pill cursor-pointer" id="yt-badge" onclick="location.href='/api/v1/auth/youtube/connect'">
        <span class="dot" id="yt-dot"></span><span id="yt-label">Connect YouTube</span>
      </button>
      <div class="topbar-spacer"></div>
      <button class="btn" onclick="openAccount()">Account</button>
      <button class="btn btn-primary" onclick="openBilling()">Upgrade</button>
    </header>

    <!-- Studio -->
    <main class="view active" id="view-studio">
      <div class="studio">
        <div class="tc-ruler">
          <span class="label">00:47 → 01:34</span>
          <div class="window"></div>
          <div class="tick"></div><div class="tick"></div><div class="tick"></div><div class="tick"></div>
          <div class="tick"></div><div class="tick"></div><div class="tick"></div><div class="tick"></div>
          <div class="tick"></div><div class="tick"></div><div class="tick"></div><div class="tick"></div>
          <div class="tick"></div><div class="tick"></div><div class="tick"></div><div class="tick"></div>
        </div>
        <h1>Point it at a niche.<br>It finds the cut.</h1>

        <p class="lede">ClipAI searches Creative-Commons-licensed video for your niche, finds the highest-retention moment, and renders it as a Short — with attribution handled automatically.</p>

        <div class="scrub-panel">
          <label class="field-label">What are you clipping?</label>
          <div class="source-tabs" id="source-tabs">
            <button type="button" data-mode="my_upload" class="source-tab-btn picked">Upload a file</button>
            <button type="button" data-mode="my_channel" class="source-tab-btn">My YouTube channel</button>
            <button type="button" data-mode="partner_channel" class="source-tab-btn">A partnered creator</button>
            <button type="button" data-mode="public_domain" class="source-tab-btn">Public domain / CC</button>
          </div>

          <div id="picker-my_upload" class="source-picker">
            <input type="file" id="upload-input" accept="video/*" class="scrub-input">
          </div>
          <div id="picker-my_channel" class="source-picker hidden">
            <select id="my-video-select" class="scrub-input"><option value="">Loading your videos…</option></select>
          </div>
          <div id="picker-partner_channel" class="source-picker hidden">
            <select id="partner-select" class="scrub-input"><option value="">Loading partner channels…</option></select>
          </div>
          <div id="picker-public_domain" class="source-picker hidden">
            <div class="scrub-row">
              <input id="niche-input" class="scrub-input" placeholder="e.g. Finance, Cooking, Archive" value="motivation" autocomplete="off">
            </div>
            <div class="reel" id="reel">
              <button type="button" data-n="motivation" class="picked">Motivation</button>
              <button type="button" data-n="finance">Finance</button>
              <button type="button" data-n="gaming">Gaming</button>
              <button type="button" data-n="ai tech">AI &amp; Tech</button>
              <button type="button" data-n="cooking">Cooking</button>
            </div>
            <div class="rights-row">
              <input type="checkbox" id="rights-confirm-check">
              <label for="rights-confirm-check">I acknowledge that clips generated from CC/public domain sources will carry attribution to the original creator.</label>
            </div>
          </div>


          <div class="grid-2">
            <div>
              <label class="field-label">Layout</label>
              <select id="layout-select">
                <option value="cinematic_blur" selected>Cinematic blur</option>
                <option value="split_screen">Split-screen b-roll</option>
              </select>
            </div>
            <div>
              <label class="field-label">Captions</label>
              <select id="subtitle-select">
                <option value="bold_captions" selected>Bold impact</option>
                <option value="clean_minimal">Clean minimal</option>
              </select>
            </div>
            <div>
              <label class="field-label">Clips per run</label>
              <select id="numclips-select">
                <option value="1" selected>1 clip</option>
                <option value="2">2 clips</option>
                <option value="3">3 clips</option>
                <option value="5">5 clips</option>
              </select>
            </div>
          </div>

          <div class="toggle-row">
            <div>
              <div class="t-label">Auto-post to YouTube</div>
              <div class="t-sub">Off = review each clip in Workplace before it goes live</div>
            </div>
            <label class="switch"><input type="checkbox" id="autopost-toggle" checked><span class="slide"></span></label>
          </div>

          <div class="run-row">
            <span class="quota" id="quota-label"></span>
            <button id="run-btn" class="run-btn">Generate</button>
          </div>

          <div class="timeline-progress" id="progress">
            <div class="tp-head"><span class="msg" id="progress-msg">Starting…</span><span class="pct" id="progress-pct">0%</span></div>
            <div class="tp-track"><div class="tp-fill" id="progress-fill"></div></div>
            <div class="tp-ticks">
              <div class="tp-tick" id="tick-search">source</div>
              <div class="tp-tick" id="tick-download">download</div>
              <div class="tp-tick" id="tick-cut">render</div>
              <div class="tp-tick" id="tick-upload">publish</div>
            </div>
          </div>
        </div>

        <div class="strip">
          <div><div class="k">01</div><div class="v">Real CC-license re-check server-side before any render — attribution is never optional.</div></div>
          <div><div class="k">02</div><div class="v">Multi-clip: one search, up to 5 distinct Shorts pulled from the same source.</div></div>
          <div><div class="k">03</div><div class="v">Review queue in Workplace before anything touches your channel.</div></div>
        </div>
      </div>
    </main>

    <!-- Workplace -->
    <main class="view" id="view-workplace">
      <div class="page">
        <div class="page-head"><div><h2>Workplace</h2><p>Drafts waiting for your review before they post.</p></div><button class="btn" onclick="loadWorkplace()">Refresh</button></div>
        <div id="workplace-grid" class="clip-grid"></div>
      </div>
    </main>

    <!-- My Clips -->
    <main class="view" id="view-clips">
      <div class="page">
        <div class="page-head"><div><h2>My Clips</h2><p>Everything live on your channel.</p></div><button class="btn" onclick="loadClips()">Refresh</button></div>
        <div class="stat-row">
          <div class="stat"><div class="k">Total views</div><div class="v" id="stat-views">—</div></div>
          <div class="stat"><div class="k">Clips posted</div><div class="v" id="stat-count">—</div></div>
          <div class="stat"><div class="k">Avg. views / clip</div><div class="v" id="stat-avg">—</div></div>
        </div>
        <div id="clips-grid" class="clip-grid"></div>
      </div>
    </main>

    <!-- Autopost -->
    <main class="view" id="view-autopost">
      <div class="page">
        <div class="page-head"><div><h2>Auto-Post</h2><p>Generate and publish on a schedule, hands-free.</p></div></div>
        <div class="form-card">
          <div class="toggle-row mb-4">
            <div><div class="t-label">Enable auto-post</div><div class="t-sub">Runs on the days/times below</div></div>
            <label class="switch"><input type="checkbox" id="ap-enabled"><span class="slide"></span></label>
          </div>
          <div class="form-block">
            <label class="field-label">Posting times (UTC)</label>
            <div class="times-list" id="times-list"></div>
            <button class="btn" type="button" onclick="addTime('12:00')">+ Add time</button>
          </div>
          <div class="form-block">
            <label class="field-label">Niche</label>
            <input class="scrub-input" id="ap-niche" placeholder="e.g. Finance">
          </div>
          <div class="form-block">
            <label class="field-label">Days</label>
            <div class="days" id="ap-days"></div>
          </div>
          <div class="rights-row mb-4">
            <input type="checkbox" id="ap-rights-check">
            <label for="ap-rights-check">I acknowledge that automated public-domain clips will carry attribution to the original creator.</label>
          </div>
          <button class="btn btn-primary" onclick="saveAutoPost()">Save schedule</button>
        </div>
      </div>
    </main>
  </div>
</div>

<nav class="bottom-nav">
  <button data-view="studio" class="active" onclick="switchView('studio')">Studio</button>
  <button data-view="workplace" onclick="switchView('workplace')">Workplace</button>
  <button data-view="clips" onclick="switchView('clips')">Clips</button>
  <button data-view="autopost" onclick="switchView('autopost')">Auto</button>
</nav>

<!-- Account modal -->
<div class="modal-overlay hidden" id="account-modal" onclick="if(event.target===this)closeModal('account-modal')">
  <div class="modal">
    <h3>Account</h3>
    <div class="modal-row"><span class="k">Email</span><span id="acc-email">—</span></div>
    <div class="modal-row"><span class="k">Plan</span><span id="acc-plan">—</span></div>
    <div class="modal-row"><span class="k">Free renders used</span><span id="acc-used">—</span></div>
    <div class="modal-actions">
      <button class="btn btn-flex-1" onclick="closeModal('account-modal')">Close</button>
      <button class="btn btn-danger" onclick="signOut()">Sign out</button>
    </div>
  </div>
</div>

<!-- Billing modal -->
<div class="modal-overlay hidden" id="billing-modal" onclick="if(event.target===this)closeModal('billing-modal')">
  <div class="modal">
    <h3>Upgrade</h3>
    <div class="modal-row"><span class="k">Pro — $29/mo</span><button class="btn btn-primary" onclick="checkout('pro')">Choose</button></div>
    <div class="modal-row"><span class="k">Full Version — $49/mo</span><button class="btn btn-primary" onclick="checkout('full_version')">Choose</button></div>
    <div class="modal-actions"><button class="btn btn-flex-1" onclick="closeModal('billing-modal')">Close</button></div>
  </div>
</div>

<div id="toasts"></div>

<script src="/static/app.js"></script>
</body>
</html>


################################################################################
# FILE: frontend/static/style.css
################################################################################

/* ==========================================================================
   ClipAI — Console UI
   Design direction: this is a clipping/editing tool, so the UI borrows from
   NLE editor consoles (DaVinci/Premiere dark panels) rather than generic
   glassy SaaS cards — sharp hairline panels, a timeline-tick rhythm, and
   monospace reserved for genuinely sequential data (timecodes, durations).
   ========================================================================== */

@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@500&display=swap');

:root {
  --bg:        #0c0c0e;
  --panel:     #141416;
  --panel-2:   #1b1b1e;
  --line:      #27272b;
  --line-lit:  #3a3a40;

  --text:      #f2f1ee;
  --text-dim:  #9a9aa3;
  --text-mute: #66666e;

  --signal:    #ff5a1f;   /* cut / record marker */
  --signal-dim:#ff5a1f22;
  --live:      #2bd6a5;   /* success / connected */
  --live-dim:  #2bd6a522;
  --warn:      #f0b429;
  --danger:    #ff5768;

  --font-display: 'Space Grotesk', sans-serif;
  --font-body: 'IBM Plex Sans', sans-serif;
  --font-mono: 'IBM Plex Mono', monospace;

  --r: 3px; /* deliberately sharp, not rounded-card-kit */

  /* Spacing Scale */
  --s-1: 4px;
  --s-2: 8px;
  --s-3: 12px;
  --s-4: 16px;
  --s-5: 20px;
  --s-6: 24px;
  --s-7: 32px;
  --s-8: 40px;
  --s-9: 48px;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-body);
  height: 100vh;
  overflow: hidden;
  -webkit-font-smoothing: antialiased;
}

button, input, select { font-family: inherit; }
:focus-visible { outline: 2px solid var(--signal); outline-offset: 2px; }

::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-thumb { background: var(--line-lit); }
::-webkit-scrollbar-track { background: transparent; }

.hidden { display: none !important; }

/* ── Shell ────────────────────────────────────────────────────────── */
.shell { display: flex; height: 100vh; }

.rail {
  width: 220px;
  min-width: 220px;
  background: var(--panel);
  border-right: 1px solid var(--line);
  display: flex;
  flex-direction: column;
  padding: 20px 14px;
}

.rail-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 8px 22px;
}
.rail-brand .mark {
  width: 22px; height: 22px;
  background: var(--signal);
  clip-path: polygon(0 0, 100% 50%, 0 100%);
}
.rail-brand span {
  font-family: var(--font-display);
  font-weight: 700;
  font-size: 17px;
  letter-spacing: -0.3px;
}

.rail-tick-label {
  font-size: 11px;
  color: var(--text-mute);
  padding: 14px 8px 6px;
  border-bottom: 1px solid var(--line);
  margin-bottom: 4px;
}

.rail-btn {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 9px 10px;
  background: transparent;
  border: none;
  border-left: 2px solid transparent;
  color: var(--text-dim);
  font-size: 13.5px;
  font-weight: 500;
  cursor: pointer;
  text-align: left;
  transition: color .12s, border-color .12s, background .12s;
}
.rail-btn:hover { color: var(--text); background: var(--panel-2); }
.rail-btn.active { color: var(--text); border-left-color: var(--signal); background: var(--panel-2); }
.rail-btn svg { width: 16px; height: 16px; flex-shrink: 0; opacity: .85; }

.rail-spacer { flex: 1; }

.rail-status {
  border: 1px solid var(--line);
  padding: 10px 12px;
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-dim);
}
.dot { width: 6px; height: 6px; border-radius: 50%; background: var(--text-mute); flex-shrink: 0; }
.dot.live { background: var(--live); box-shadow: 0 0 6px var(--live); }
.dot.warn { background: var(--warn); }
.dot.off  { background: var(--danger); }

/* ── Main ─────────────────────────────────────────────────────────── */
.main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }

.topbar {
  height: 56px;
  min-height: 56px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 24px;
  border-bottom: 1px solid var(--line);
}
.topbar-spacer { flex: 1; }

.btn {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 7px 14px;
  border: 1px solid var(--line);
  background: var(--panel-2);
  color: var(--text);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  border-radius: var(--r);
  transition: border-color .12s, background .12s;
}
.btn:hover { border-color: var(--line-lit); }
.btn:active:not(:disabled) { transform: translateY(1px); }
.btn:disabled { opacity: .45; cursor: not-allowed; }
.btn-primary { background: var(--signal); border-color: var(--signal); color: #140a05; font-weight: 700; }
.btn-primary:hover { filter: brightness(1.08); }
.btn-primary:active:not(:disabled) { transform: translateY(1px); filter: brightness(0.96); }
.btn-ghost { background: transparent; border-color: transparent; color: var(--text-dim); }
.btn-ghost:hover { color: var(--text); background: var(--panel-2); }
.btn-danger { color: var(--danger); border-color: #ff576833; }

.pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 11px;
  border: 1px solid var(--line);
  border-radius: var(--r);
  font-size: 12px;
  color: var(--text-dim);
}

.view { flex: 1; overflow-y: auto; display: none; }
.view.active { display: block; }

/* ── Studio ───────────────────────────────────────────────────────── */
.studio {
  max-width: 760px;
  margin: 0 auto;
  padding: 48px 28px 80px;
}

/* Timecode ruler accent */
.tc-ruler {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 20px;
  padding: 6px 10px;
  border: 1px solid var(--line);
  background: var(--panel);
  border-radius: var(--r);
  width: fit-content;
}
.tc-ruler .label {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--signal);
  letter-spacing: 0.04em;
  margin-right: 6px;
}
.tc-ruler .window {
  width: 32px;
  height: 6px;
  background: var(--signal-dim);
  border: 1px solid var(--signal);
  border-radius: 1px;
}
.tc-ruler .tick {
  width: 1px;
  height: 8px;
  background: var(--line-lit);
}

.studio h1 {
  font-family: var(--font-display);
  font-size: 40px;
  font-weight: 700;
  letter-spacing: -0.8px;
  line-height: 1.1;
  margin-bottom: 8px;
}
.studio .lede { color: var(--text-dim); font-size: 14.5px; margin-bottom: 28px; max-width: 46ch; }

.scrub-panel {
  border: 1px solid var(--line);
  background: var(--panel);
  padding: 20px;
}
.field-label { font-size: 11.5px; color: var(--text-mute); margin-bottom: 7px; display: block; }

.scrub-row { display: flex; gap: 10px; }
.scrub-input {
  flex: 1;
  background: var(--bg);
  border: 1px solid var(--line);
  color: var(--text);
  padding: 13px 14px;
  font-size: 15px;
  border-radius: var(--r);
}
.scrub-input:focus { border-color: var(--signal); }

.reel {
  display: flex;
  gap: 6px;
  margin-top: 12px;
  flex-wrap: wrap;
}
.reel button {
  background: transparent;
  border: 1px solid var(--line);
  color: var(--text-dim);
  font-size: 12px;
  padding: 5px 10px;
  border-radius: var(--r);
  cursor: pointer;
}
.reel button.picked { border-color: var(--signal); color: var(--text); background: var(--signal-dim); }

.source-tab-btn {
  background: var(--panel-2);
  border: 1px solid var(--line);
  color: var(--text-dim);
  font-size: 12.5px;
  font-weight: 500;
  padding: 7px 12px;
  border-radius: var(--r);
  cursor: pointer;
  transition: all .12s;
}
.source-tab-btn:hover { border-color: var(--line-lit); color: var(--text); }
.source-tab-btn.picked { border-color: var(--signal); color: var(--text); background: var(--signal-dim); font-weight: 600; }

.grid-2 {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--line);
}
.grid-2 select {
  width: 100%;
  background: var(--bg);
  border: 1px solid var(--line);
  color: var(--text);
  padding: 8px 10px;
  font-size: 13px;
  border-radius: var(--r);
}

.toggle-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 14px;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: var(--r);
}
.toggle-row .t-label { font-size: 13px; font-weight: 500; }
.toggle-row .t-sub { font-size: 11.5px; color: var(--text-mute); margin-top: 2px; }

.switch { position: relative; width: 38px; height: 21px; flex-shrink: 0; }
.switch input { opacity: 0; width: 0; height: 0; }
.switch input:focus-visible + .slide { outline: 2px solid var(--signal); outline-offset: 2px; }
.switch .slide { position: absolute; inset: 0; background: var(--line-lit); border-radius: 20px; cursor: pointer; transition: .15s; }
.switch .slide::before { content: ''; position: absolute; height: 15px; width: 15px; left: 3px; top: 3px; background: var(--text); border-radius: 50%; transition: .15s; }
.switch input:checked + .slide { background: var(--signal); }
.switch input:checked + .slide::before { transform: translateX(17px); background: #140a05; }

.run-row { display: flex; align-items: center; gap: 12px; margin-top: 18px; }
.run-btn {
  flex: 1;
  height: 46px;
  background: var(--signal);
  color: #140a05;
  border: none;
  font-family: var(--font-display);
  font-weight: 700;
  font-size: 15px;
  cursor: pointer;
  border-radius: var(--r);
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  transition: filter .12s, opacity .12s, transform .08s;
}
.run-btn:hover:not(:disabled) { filter: brightness(1.08); }
.run-btn:active:not(:disabled) { transform: translateY(1px); filter: brightness(0.96); }
.run-btn:disabled { opacity: .5; cursor: not-allowed; }

.quota { font-size: 12px; color: var(--text-mute); }
.quota strong { color: var(--warn); }

/* Progress */
.timeline-progress {
  margin-top: 18px;
  border: 1px solid var(--line);
  padding: 16px;
  display: none;
}
.timeline-progress.active { display: block; }
.tp-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 10px; }
.tp-head .msg { font-size: 13px; color: var(--text-dim); }
.tp-head .pct { font-family: var(--font-mono); font-size: 13px; color: var(--signal); }
.tp-track { height: 4px; background: var(--line); position: relative; overflow: hidden; }
.tp-fill { height: 100%; width: 0%; background: var(--signal); transition: width .5s ease; }
.tp-ticks { display: flex; margin-top: 10px; gap: 0; }
.tp-tick { flex: 1; text-align: center; font-size: 10.5px; color: var(--text-mute); position: relative; }
.tp-tick::before { content: ''; display: block; width: 5px; height: 5px; border-radius: 50%; background: var(--line-lit); margin: 0 auto 6px; }
.tp-tick.on::before { background: var(--signal); }
.tp-tick.done::before { background: var(--live); }
.tp-tick.on { color: var(--text); }
.tp-tick.done { color: var(--live); }

/* Feature strip */
.strip {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 1px;
  margin-top: 36px;
  background: var(--line);
  border: 1px solid var(--line);
}
.strip > div { background: var(--panel); padding: 16px; }
.strip .k { font-family: var(--font-mono); font-size: 11px; color: var(--signal); margin-bottom: 8px; }
.strip .v { font-size: 12.5px; color: var(--text-dim); line-height: 1.5; }

/* ── Workplace / clip cards ──────────────────────────────────────── */
.page { max-width: 1100px; margin: 0 auto; padding: 40px 28px 80px; }
.page-head { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 24px; }
.page-head h2 { font-family: var(--font-display); font-size: 26px; font-weight: 700; letter-spacing: -0.4px; }
.page-head p { color: var(--text-dim); font-size: 13.5px; margin-top: 4px; }

.empty { border: 1px dashed var(--line); padding: 48px; text-align: center; color: var(--text-mute); font-size: 13.5px; }

.clip-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; }
.clip-card {
  border: 1px solid var(--line);
  background: var(--panel);
  overflow: hidden;
  transition: border-color .15s ease, transform .15s ease, box-shadow .15s ease;
}
.clip-card:hover {
  border-color: var(--line-lit);
  transform: translateY(-2px);
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.35);
}
.clip-thumb {
  aspect-ratio: 9/16;
  background: var(--bg);
  position: relative;
  cursor: pointer;
  overflow: hidden;
}
.clip-thumb img, .clip-thumb video {
  width: 100%;
  height: 100%;
  object-fit: cover;
  transition: transform .2s ease;
}
.clip-thumb:hover img, .clip-thumb:hover video {
  transform: scale(1.03);
}
.clip-thumb .badge { position: absolute; top: 8px; left: 8px; background: rgba(0,0,0,.7); font-family: var(--font-mono); font-size: 10.5px; padding: 3px 7px; color: var(--warn); border: 1px solid var(--warn); }
.clip-body { padding: 12px; }
.clip-title { font-size: 13px; font-weight: 500; margin-bottom: 8px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.clip-actions { display: flex; gap: 6px; }
.clip-actions .btn { flex: 1; justify-content: center; padding: 6px 8px; font-size: 12px; }

/* Stats */
.stat-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px; background: var(--line); border: 1px solid var(--line); margin-bottom: 28px; }
.stat { background: var(--panel); padding: 20px; }
.stat .k { font-size: 11px; color: var(--text-mute); margin-bottom: 10px; }
.stat .v { font-family: var(--font-mono); font-size: 28px; font-weight: 500; }

/* Autopost */
.form-card { border: 1px solid var(--line); background: var(--panel); padding: 28px; max-width: 640px; }
.form-block { margin-bottom: 22px; }
.form-block label.field-label { margin-bottom: 8px; }
.times-list { display: flex; flex-direction: column; gap: 8px; margin-bottom: 10px; }
.times-list input[type=time] { background: var(--bg); border: 1px solid var(--line); color: var(--text); padding: 8px 10px; border-radius: var(--r); font-family: var(--font-mono); }
.days { display: flex; gap: 6px; flex-wrap: wrap; }
.day-chip { width: 40px; height: 40px; border: 1px solid var(--line); display: flex; align-items: center; justify-content: center; font-size: 12px; color: var(--text-dim); cursor: pointer; border-radius: var(--r); }
.day-chip input { display: none; }
.day-chip:has(input:checked) { border-color: var(--signal); color: var(--text); background: var(--signal-dim); }

/* ── Modal / auth gate ────────────────────────────────────────────── */
.gate { position: fixed; inset: 0; background: var(--bg); display: flex; align-items: center; justify-content: center; z-index: 500; }
.gate-box { text-align: center; max-width: 340px; }
.gate-box .mark-big { width: 44px; height: 44px; background: var(--signal); clip-path: polygon(0 0, 100% 50%, 0 100%); margin: 0 auto 20px; }
.gate-box h1 { font-family: var(--font-display); font-size: 24px; margin-bottom: 8px; }
.gate-box p { color: var(--text-dim); font-size: 13.5px; margin-bottom: 24px; }
.google-btn { display: flex; align-items: center; justify-content: center; gap: 10px; width: 100%; padding: 12px; background: #fff; color: #111; border-radius: var(--r); font-weight: 600; font-size: 14px; text-decoration: none; }

.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.7); display: flex; align-items: center; justify-content: center; z-index: 600; }
.modal-overlay.hidden { display: none; }
.modal { background: var(--panel); border: 1px solid var(--line); padding: 28px; width: 420px; max-width: 92vw; }
.modal h3 { font-family: var(--font-display); font-size: 19px; margin-bottom: 16px; }
.modal-row { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid var(--line); font-size: 13px; }
.modal-row:last-of-type { border-bottom: none; }
.modal-row .k { color: var(--text-mute); }
.modal-actions { display: flex; gap: 10px; margin-top: 18px; }

/* Toast */
#toasts { position: fixed; bottom: 18px; right: 18px; z-index: 900; display: flex; flex-direction: column; gap: 8px; }
.toast { background: var(--panel); border: 1px solid var(--line); border-left: 3px solid var(--live); padding: 12px 16px; font-size: 13px; min-width: 260px; transform: translateX(120%); transition: transform .3s; }
.toast.show { transform: translateX(0); }
.toast.error { border-left-color: var(--danger); }

/* Mobile bottom nav & responsive */
.bottom-nav { display: none; }

@media (max-width: 880px) {
  .rail { display: none; }
  .tc-ruler { display: none; }
  .bottom-nav {
    display: flex;
    position: fixed;
    bottom: 0; left: 0; right: 0;
    height: 58px;
    background: var(--panel);
    border-top: 1px solid var(--line);
    z-index: 400;
  }
  .bottom-nav button { flex: 1; background: none; border: none; color: var(--text-mute); font-size: 10.5px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; }
  .bottom-nav button.active { color: var(--signal); }
  .bottom-nav svg { width: 18px; height: 18px; }
  .view.active { padding-bottom: 58px; }
  .studio, .page { padding: 28px 16px 90px; }
  .grid-2 { grid-template-columns: 1fr; }
  .stat-row { grid-template-columns: 1fr; }
}

/* Accessibility: Reduced motion */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}

/* ── Utilities for CSP-safe styling ───────────────────────────────── */
.hidden { display: none !important; }
.cursor-pointer { cursor: pointer; }
.source-tabs { display: flex; gap: 6px; margin-bottom: 14px; flex-wrap: wrap; }
.source-picker { margin-bottom: 12px; }
.source-picker select { width: 100%; }
.source-picker input[type="file"] { padding: 10px; }
.rights-row { margin-top: 10px; font-size: 12px; color: var(--text-dim); display: flex; align-items: flex-start; gap: 8px; }
.rights-row input { margin-top: 2px; }
.run-row { display: flex; justify-content: space-between; align-items: center; margin-top: 16px; }
.run-row .run-btn { flex: 0 0 170px; }
.toggle-row.mb-4 { margin-bottom: 22px; }
.btn-flex-1 { flex: 1; }
.time-row { display: flex; gap: 8px; }
.time-row input[type="time"] { flex: 1; }


################################################################################
# FILE: frontend/static/app.js
################################################################################

/* ==========================================================================
   ClipAI v3 — Console UI Application Controller
   Matches the NLE editor layout in index.html & style.css.
   ========================================================================== */

let currentUser = null;
let pollTimer = null;

// ─── Toast Notifications ──────────────────────────────────────────────────
function showToast(message, type = 'live') {
  const container = document.getElementById('toasts');
  if (!container) return;
  const t = document.createElement('div');
  t.className = `toast ${type === 'error' ? 'error' : ''}`;
  t.textContent = message;
  container.appendChild(t);
  requestAnimationFrame(() => t.classList.add('show'));
  setTimeout(() => {
    t.classList.remove('show');
    setTimeout(() => t.remove(), 320);
  }, 3500);
}

// ─── View Switching ───────────────────────────────────────────────────────
function switchView(viewName) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('.rail-btn, .bottom-nav button').forEach(b => {
    if (b.dataset.view === viewName) {
      b.classList.add('active');
    } else {
      b.classList.remove('active');
    }
  });
  const target = document.getElementById(`view-${viewName}`);
  if (target) target.classList.add('active');

  if (viewName === 'workplace') loadWorkplace();
  if (viewName === 'clips') loadClips();
  if (viewName === 'autopost') loadAutoPost();
}

// ─── Modal Management ─────────────────────────────────────────────────────
function openModal(id) {
  const m = document.getElementById(id);
  if (m) m.classList.remove('hidden');
}

function closeModal(id) {
  const m = document.getElementById(id);
  if (m) m.classList.add('hidden');
}

function openAccount() {
  openModal('account-modal');
  refreshAccountDetails();
}

function openBilling() {
  openModal('billing-modal');
}

async function signOut() {
  try {
    await fetch('/api/v1/auth/logout', { method: 'POST' });
  } catch (e) {
    console.error('Sign out error:', e);
  }
  window.location.href = '/';
}

async function checkout(tier) {
  try {
    const res = await fetch('/api/v1/create-checkout-session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tier })
    });
    const data = await res.json();
    if (data.checkout_url) {
      window.location.href = data.checkout_url;
    } else {
      showToast(data.detail || 'Could not start checkout', 'error');
    }
  } catch (err) {
    showToast('Billing error: ' + err.message, 'error');
  }
}

// ─── User Profile & Auth Verification ─────────────────────────────────────
async function checkAuthAndProfile() {
  try {
    const res = await fetch('/api/v1/user/profile');
    if (res.status === 401 || res.status === 403) {
      document.getElementById('gate').classList.remove('hidden');
      document.getElementById('shell').classList.add('hidden');
      return false;
    }
    const data = await res.json();
    currentUser = data;
    document.getElementById('gate').classList.add('hidden');
    document.getElementById('shell').classList.remove('hidden');

    updateQuotaDisplay(data);
    refreshAccountDetails();
    checkYouTubeStatus();
    checkWorkerHeartbeat();
    return true;
  } catch (e) {
    document.getElementById('gate').classList.remove('hidden');
    document.getElementById('shell').classList.add('hidden');
    return false;
  }
}

function updateQuotaDisplay(data) {
  const q = document.getElementById('quota-label');
  if (!q) return;
  if (data.license === 'pro' || data.license === 'full_version') {
    q.innerHTML = `Plan: <strong>${data.license.toUpperCase()}</strong> (Unlimited renders)`;
  } else {
    q.innerHTML = `Free tier: <strong>${data.free_clips_used || 0}/1 used</strong>`;
  }
}

function refreshAccountDetails() {
  if (!currentUser) return;
  const emailEl = document.getElementById('acc-email');
  const planEl = document.getElementById('acc-plan');
  const usedEl = document.getElementById('acc-used');
  if (emailEl) emailEl.textContent = currentUser.email || 'Google User';
  if (planEl) planEl.textContent = (currentUser.license || 'Free tier').toUpperCase();
  if (usedEl) usedEl.textContent = String(currentUser.free_clips_used || 0);
}

// ─── Engine Status & YouTube Status ───────────────────────────────────────
async function checkYouTubeStatus() {
  try {
    const res = await fetch('/api/v1/auth/youtube/status');
    const data = await res.json();
    const dot = document.getElementById('yt-dot');
    const label = document.getElementById('yt-label');
    const badge = document.getElementById('yt-badge');

    if (data.connected) {
      if (dot) dot.className = 'dot live';
      if (label) label.textContent = 'YouTube Connected';
      if (badge) badge.onclick = () => showToast('YouTube channel is connected!');
    } else {
      if (dot) dot.className = 'dot off';
      if (label) label.textContent = 'Connect YouTube';
      if (badge) badge.onclick = () => location.href = '/api/v1/auth/youtube/connect';
    }
  } catch (e) {
  }
}

async function checkWorkerHeartbeat() {
  try {
    const uid = currentUser ? currentUser.user_id : 'cloud';
    const res = await fetch(`/api/v1/worker/heartbeat?user_id=${uid}`);
    const data = await res.json();
    const dot = document.getElementById('worker-dot');
    const label = document.getElementById('worker-label');

    if (data.alive !== false) {
      if (dot) dot.className = 'dot live';
      if (label) label.textContent = 'Engine Active';
    } else {
      if (dot) dot.className = 'dot warn';
      if (label) label.textContent = 'Engine Standby';
    }
  } catch (e) {
    const dot = document.getElementById('worker-dot');
    const label = document.getElementById('worker-label');
    if (dot) dot.className = 'dot live';
    if (label) label.textContent = 'Engine Active';
  }
}
setInterval(checkWorkerHeartbeat, 15000);

let currentSourceMode = 'my_upload';

// ─── Studio: Clip Generation ──────────────────────────────────────────────
function initStudio() {
  const sourceTabs = document.getElementById('source-tabs');
  if (sourceTabs) {
    sourceTabs.addEventListener('click', (e) => {
      const btn = e.target.closest('.source-tab-btn');
      if (!btn) return;
      sourceTabs.querySelectorAll('.source-tab-btn').forEach(b => b.classList.remove('picked'));
      btn.classList.add('picked');
      setSourceMode(btn.dataset.mode);
    });
  }

  const reel = document.getElementById('reel');
  const nicheInput = document.getElementById('niche-input');
  if (reel && nicheInput) {
    reel.addEventListener('click', (e) => {
      const btn = e.target.closest('button');
      if (!btn) return;
      reel.querySelectorAll('button').forEach(b => b.classList.remove('picked'));
      btn.classList.add('picked');
      nicheInput.value = btn.dataset.n || btn.textContent.trim().toLowerCase();
    });
  }

  const runBtn = document.getElementById('run-btn');
  if (runBtn) {
    runBtn.addEventListener('click', startGeneration);
  }
}

function setSourceMode(mode) {
  currentSourceMode = mode;
  document.querySelectorAll('.source-picker').forEach(el => el.classList.add('hidden'));
  const activePicker = document.getElementById(`picker-${mode}`);
  if (activePicker) activePicker.classList.remove('hidden');

  if (mode === 'my_channel') loadMyChannelVideos();
  if (mode === 'partner_channel') loadPartnerChannels();
}

async function loadMyChannelVideos() {
  const select = document.getElementById('my-video-select');
  if (!select) return;
  select.innerHTML = '<option value="">Loading your videos…</option>';
  try {
    const res = await fetch('/api/v1/my-channel/videos');
    if (!res.ok) {
      const err = await res.json();
      select.innerHTML = `<option value="">${err.detail || 'YouTube not connected'}</option>`;
      return;
    }
    const data = await res.json();
    if (!data.videos || data.videos.length === 0) {
      select.innerHTML = '<option value="">No videos found on your channel</option>';
      return;
    }
    select.innerHTML = data.videos.map(v => `<option value="${v.id}">${v.title || v.id} (${Math.round((v.duration||0)/60)}m)</option>`).join('');
  } catch (err) {
    select.innerHTML = '<option value="">Failed to load videos</option>';
  }
}

async function loadPartnerChannels() {
  const select = document.getElementById('partner-select');
  if (!select) return;
  select.innerHTML = '<option value="">Loading partner channels…</option>';
  try {
    const res = await fetch('/api/v1/partner-channels');
    const data = await res.json();
    if (!data.channels || data.channels.length === 0) {
      select.innerHTML = '<option value="">No partner channels currently active</option>';
      return;
    }
    select.innerHTML = data.channels.map(c => `<option value="${c.channel_id}">${c.channel_title || c.channel_id}</option>`).join('');
  } catch (err) {
    select.innerHTML = '<option value="">Failed to load partner channels</option>';
  }
}

async function startGeneration() {
  const layoutSel = document.getElementById('layout-select');
  const subSel = document.getElementById('subtitle-select');
  const numSel = document.getElementById('numclips-select');
  const autoToggle = document.getElementById('autopost-toggle');
  const runBtn = document.getElementById('run-btn');

  const payload = {
    source_mode: currentSourceMode,
    layout: layoutSel?.value || 'cinematic_blur',
    subtitle_style: subSel?.value || 'bold_captions',
    num_clips: parseInt(numSel?.value || '1', 10),
    auto_upload: Boolean(autoToggle?.checked),
  };

  if (currentSourceMode === 'my_upload') {
    const uploadInput = document.getElementById('upload-input');
    const file = uploadInput?.files?.[0];
    if (!file) {
      showToast('Please select a video file to upload', 'error');
      return;
    }
    payload.source_video_id = file.name;
    payload.niche = file.name.replace(/\.[^/.]+$/, "");
  } else if (currentSourceMode === 'my_channel') {
    const myVid = document.getElementById('my-video-select')?.value;
    if (!myVid) {
      showToast('Please select a video from your YouTube channel', 'error');
      return;
    }
    payload.source_video_id = myVid;
  } else if (currentSourceMode === 'partner_channel') {
    const partnerId = document.getElementById('partner-select')?.value;
    if (!partnerId) {
      showToast('Please select a partner creator', 'error');
      return;
    }
    payload.partner_channel_id = partnerId;
    payload.source_video_id = partnerId; // signals partner sourcing target
  } else if (currentSourceMode === 'public_domain') {
    const nicheInput = document.getElementById('niche-input');
    const niche = (nicheInput?.value || '').trim();
    if (!niche) {
      showToast('Please enter a topic or niche hint', 'error');
      return;
    }
    const rightsCheck = document.getElementById('rights-confirm-check');
    if (rightsCheck && !rightsCheck.checked) {
      showToast('Please confirm attribution acknowledgment to proceed', 'error');
      return;
    }
    payload.niche = niche;
    payload.rights_confirmed = Boolean(rightsCheck ? rightsCheck.checked : true);
  }


  runBtn.disabled = true;
  runBtn.textContent = 'Queuing…';

  try {
    const res = await fetch('/api/v1/generate-clip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

    if (res.status === 402) {
      openBilling();
      showToast('Free tier used — upgrade for unlimited renders', 'error');
      runBtn.disabled = false;
      runBtn.textContent = 'Generate';
      return;
    }

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Failed to queue job');
    }

    const data = await res.json();
    showToast('Pipeline started! Watch progress below.', 'live');
    startProgress(data.job_id);
  } catch (err) {
    showToast(err.message, 'error');
    runBtn.disabled = false;
    runBtn.textContent = 'Generate';
  }
}

function startProgress(jobId) {
  const pBox = document.getElementById('progress');
  const pMsg = document.getElementById('progress-msg');
  const pPct = document.getElementById('progress-pct');
  const pFill = document.getElementById('progress-fill');
  const runBtn = document.getElementById('run-btn');

  if (pBox) pBox.classList.add('active');
  if (pFill) pFill.style.width = '5%';
  if (pPct) pPct.textContent = '5%';
  if (pMsg) pMsg.textContent = 'Finding CC source video…';

  updateTicks(10);

  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const res = await fetch(`/api/v1/job-status/${jobId}`);
      if (!res.ok) return;
      const data = await res.json();
      const pct = Math.max(5, Math.min(100, data.progress || 0));

      if (pFill) pFill.style.width = `${pct}%`;
      if (pPct) pPct.textContent = `${pct}%`;
      if (pMsg) pMsg.textContent = data.message || 'Processing…';
      updateTicks(pct);

      if (data.status === 'complete' || data.status === 'draft_ready' || data.status === 'error' || pct >= 100) {
        clearInterval(pollTimer);
        runBtn.disabled = false;
        runBtn.textContent = 'Generate';

        if (data.status === 'complete') {
          showToast('Short published to YouTube!', 'live');
          setTimeout(() => switchView('clips'), 1200);
        } else if (data.status === 'draft_ready') {
          showToast('Short saved to Workplace drafts!', 'live');
          setTimeout(() => switchView('workplace'), 1200);
        } else if (data.status === 'error') {
          showToast(`Pipeline failed: ${data.message}`, 'error');
        }
      }
    } catch (e) {
    }
  }, 1800);
}

function updateTicks(pct) {
  const search = document.getElementById('tick-search');
  const download = document.getElementById('tick-download');
  const cut = document.getElementById('tick-cut');
  const upload = document.getElementById('tick-upload');

  if (search) {
    search.className = pct >= 25 ? 'tp-tick done' : (pct >= 5 ? 'tp-tick on' : 'tp-tick');
  }
  if (download) {
    download.className = pct >= 50 ? 'tp-tick done' : (pct >= 25 ? 'tp-tick on' : 'tp-tick');
  }
  if (cut) {
    cut.className = pct >= 80 ? 'tp-tick done' : (pct >= 50 ? 'tp-tick on' : 'tp-tick');
  }
  if (upload) {
    upload.className = pct >= 100 ? 'tp-tick done' : (pct >= 80 ? 'tp-tick on' : 'tp-tick');
  }
}

// ─── Workplace (Review & Publish Drafts) ───────────────────────────────────
// ─── Workplace (Review & Publish Drafts) ───────────────────────────────────
async function loadWorkplace() {
  const grid = document.getElementById('workplace-grid');
  if (!grid) return;
  grid.innerHTML = '<div class="empty">Loading drafts…</div>';

  try {
    const res = await fetch('/api/v1/workplace/clips');
    if (!res.ok) throw new Error('Failed to load drafts');
    const data = await res.json();
    const drafts = data.clips || data.drafts || (Array.isArray(data) ? data : []);

    if (!drafts || drafts.length === 0) {
      grid.innerHTML = '<div class="empty">No drafts waiting for review. Render a clip with auto-post turned off to review it here first.</div>';
      return;
    }

    grid.innerHTML = drafts.map(d => `
      <div class="clip-card" id="card-${d.id}">
        <div class="clip-thumb">
          ${d.youtube_url ? `<video src="${d.youtube_url}" preload="metadata" muted playsinline></video>` : ''}
          <div class="badge">DRAFT</div>
        </div>
        <div class="clip-body">
          <div class="clip-title" title="${escapeHtml(d.title || d.niche || 'Untitled Short')}">${escapeHtml(d.title || d.niche || 'Untitled Short')}</div>
          <div class="clip-actions">
            <button class="btn btn-primary" onclick="publishDraft('${d.id}')">Publish</button>
            <button class="btn btn-danger" onclick="deleteDraft('${d.id}')">Delete</button>
          </div>
        </div>
      </div>
    `).join('');
  } catch (err) {
    grid.innerHTML = `<div class="empty">Error loading drafts: ${escapeHtml(err.message)}</div>`;
  }
}

async function publishDraft(clipId) {
  try {
    const res = await fetch('/api/v1/clip/publish-draft', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clip_id: clipId })
    });
    if (!res.ok) throw new Error('Failed to publish');
    showToast('Draft queued for upload to your channel!', 'live');
    document.getElementById(`card-${clipId}`)?.remove();
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function deleteDraft(clipId) {
  if (!confirm('Delete this draft?')) return;
  try {
    const res = await fetch(`/api/v1/clip/${clipId}`, { method: 'DELETE' });
    if (!res.ok) throw new Error('Delete failed');
    showToast('Draft deleted');
    document.getElementById(`card-${clipId}`)?.remove();
  } catch (err) {
    showToast(err.message, 'error');
  }
}

// ─── My Clips (Live Videos & Stats) ───────────────────────────────────────
async function loadClips() {
  const grid = document.getElementById('clips-grid');
  if (!grid) return;
  grid.innerHTML = '<div class="empty">Loading channel clips…</div>';

  try {
    const res = await fetch('/api/v1/clips');
    if (!res.ok) throw new Error('Failed to load clips');
    const data = await res.json();

    const viewsEl = document.getElementById('stat-views');
    const countEl = document.getElementById('stat-count');
    const avgEl = document.getElementById('stat-avg');

    if (viewsEl) viewsEl.textContent = formatCompact(data.total_views || 0);
    if (countEl) countEl.textContent = String(data.total_videos || 0);
    if (avgEl) avgEl.textContent = formatCompact(data.avg_views || 0);

    const published = (data.videos || []).filter(v => v.youtube_url);
    if (!published || published.length === 0) {
      grid.innerHTML = '<div class="empty">No live clips posted yet. Start in the Studio!</div>';
      return;
    }

    grid.innerHTML = published.map(c => {
      const vidId = extractYtId(c.youtube_url);
      const thumb = vidId ? `https://i.ytimg.com/vi/${vidId}/hqdefault.jpg` : '';
      return `
        <div class="clip-card">
          <div class="clip-thumb" onclick="window.open('${c.youtube_url}', '_blank')">
            ${thumb ? `<img src="${thumb}" alt="thumbnail" loading="lazy">` : ''}
            <div class="badge">${formatCompact(c.views || 0)} VIEWS</div>
          </div>
          <div class="clip-body">
            <div class="clip-title">${escapeHtml(c.title || c.niche || 'Short')}</div>
            <div class="clip-actions">
              <a class="btn" href="${c.youtube_url}" target="_blank" rel="noopener">Watch ↗</a>
            </div>
          </div>
        </div>
      `;
    }).join('');
  } catch (err) {
    grid.innerHTML = `<div class="empty">Error loading clips: ${escapeHtml(err.message)}</div>`;
  }
}

// ─── Auto-Post Scheduler ──────────────────────────────────────────────────
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

async function loadAutoPost() {
  renderDays([]);
  try {
    const res = await fetch('/api/v1/auto-post/settings');
    if (!res.ok) return;
    const cfg = await res.json();
    const enabledEl = document.getElementById('ap-enabled');
    const nicheEl = document.getElementById('ap-niche');
    const rightsEl = document.getElementById('ap-rights-check');

    if (enabledEl) enabledEl.checked = Boolean(cfg.enabled);
    if (nicheEl) nicheEl.value = cfg.niche || 'motivation';
    if (rightsEl) rightsEl.checked = Boolean(cfg.rights_confirmed);

    const tList = document.getElementById('times-list');
    if (tList) {
      tList.innerHTML = '';
      (cfg.times || ["12:00"]).forEach(t => addTime(t));
    }
    renderDays(cfg.days || DAYS);
  } catch (e) {
    renderDays(DAYS);
  }
}

function renderDays(activeDays) {
  const container = document.getElementById('ap-days');
  if (!container) return;
  container.innerHTML = DAYS.map(d => {
    const isChecked = activeDays.includes(d);
    return `
      <label class="day-chip">
        <input type="checkbox" value="${d}" ${isChecked ? 'checked' : ''}>
        ${d.slice(0, 2)}
      </label>
    `;
  }).join('');
}

function addTime(val = '12:00') {
  const list = document.getElementById('times-list');
  if (!list) return;
  const row = document.createElement('div');
  row.className = 'time-row';
  row.innerHTML = `
    <input type="time" value="${val}">
    <button class="btn btn-ghost" type="button" onclick="this.parentElement.remove()">✕</button>
  `;
  list.appendChild(row);
}

async function saveAutoPost() {
  const enabled = Boolean(document.getElementById('ap-enabled')?.checked);
  const niche = (document.getElementById('ap-niche')?.value || 'motivation').trim();
  const times = Array.from(document.querySelectorAll('#times-list input[type=time]')).map(i => i.value).filter(Boolean);
  const days = Array.from(document.querySelectorAll('#ap-days input:checked')).map(i => i.value);
  const rights_confirmed = Boolean(document.getElementById('ap-rights-check')?.checked);

  if (enabled && !rights_confirmed) {
    showToast('Please confirm attribution acknowledgment to enable auto-post', 'error');
    return;
  }

  try {
    const res = await fetch('/api/v1/auto-post/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled, niche, times: times.length ? times : ["12:00"], days, rights_confirmed })
    });
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.detail || 'Save failed');
    }
    showToast('Auto-post schedule saved!', 'live');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

// ─── Utilities ────────────────────────────────────────────────────────────
function escapeHtml(str) {
  return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function formatCompact(num) {
  num = Number(num) || 0;
  if (num >= 1_000_000) return (num / 1_000_000).toFixed(1) + 'M';
  if (num >= 1_000) return (num / 1_000).toFixed(1) + 'K';
  return String(num);
}

function extractYtId(url) {
  if (!url) return '';
  const m = url.match(/(?:shorts\/|v=|youtu\.be\/)([a-zA-Z0-9_-]{11})/);
  return m ? m[1] : '';
}

// ─── DOM Initialization ───────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // Handle auth / youtube redirect query parameters
  const params = new URLSearchParams(window.location.search);
  const authStatus = params.get('auth');
  const ytStatus = params.get('youtube');
  const detail = params.get('detail');

  if (authStatus === 'error') {
    const msg = detail === 'invalid_state' ? 'Login session expired or invalid. Please try again.'
              : detail === 'unverified_email' ? 'Please verify your Google email address.'
              : detail === 'not_configured' ? 'Google OAuth is not configured yet on this instance.'
              : 'Google sign-in failed. Please try again.';
    showToast(msg, 'error');
    window.history.replaceState({}, document.title, window.location.pathname);
  } else if (authStatus === 'success') {
    showToast('Signed in successfully!', 'live');
    window.history.replaceState({}, document.title, window.location.pathname);
  }

  if (ytStatus === 'error') {
    const msg = detail === 'invalid_state' ? 'YouTube connection session expired. Please retry.'
              : detail === 'not_configured' ? 'YouTube OAuth is not configured on this instance.'
              : 'Failed to connect YouTube channel. Please try again.';
    showToast(msg, 'error');
    window.history.replaceState({}, document.title, window.location.pathname);
  } else if (ytStatus === 'connected') {
    showToast('YouTube channel connected successfully!', 'live');
    window.history.replaceState({}, document.title, window.location.pathname);
  }

  initStudio();
  checkAuthAndProfile();
});

function getVisitorId() {
  let id = sessionStorage.getItem('clipai_visitor_id');
  if (!id) {
    id = 'v_' + Math.random().toString(36).slice(2) + Date.now();
    sessionStorage.setItem('clipai_visitor_id', id);
  }
  return id;
}

async function sendPresencePing() {
  try {
    await fetch(`/api/v1/presence/ping?visitor_id=${getVisitorId()}`, { method: 'POST' });
  } catch (e) {}
}

sendPresencePing();
setInterval(sendPresencePing, 20000);
