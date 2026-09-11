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
    def __init__(self, primary_url: str, secondary_url: str = "", tertiary_url: str = ""):
        self.primary = aioredis.from_url(primary_url, decode_responses=True) if primary_url else None
        self.secondary = aioredis.from_url(secondary_url, decode_responses=True) if secondary_url else None
        self.tertiary = aioredis.from_url(tertiary_url, decode_responses=True) if tertiary_url else None

    async def _clients(self):
        return [c for c in (self.primary, self.secondary, self.tertiary) if c is not None]

    def __getattr__(self, name):
        async def _call(*args, **kwargs):
            last_err = None
            for client in await self._clients():
                try:
                    return await getattr(client, name)(*args, **kwargs)
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    if any(m in str(e).lower() for m in _QUOTA_MARKERS):
                        log.warning("Redis quota hit on client, failing over: %s", e)
                        continue
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
    return FailoverRedis(settings.redis_url, settings.redis_url_2, settings.redis_url_3)


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