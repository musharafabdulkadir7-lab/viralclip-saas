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
