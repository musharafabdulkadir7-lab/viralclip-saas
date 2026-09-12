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


def _safe_create_redis(url: str):
    if not url or not isinstance(url, str):
        return None
    url_clean = url.strip().strip("'\"")
    if not (url_clean.startswith("redis://") or url_clean.startswith("rediss://") or url_clean.startswith("unix://")):
        log.warning("Invalid Redis URL scheme skipped: %r (must start with redis://, rediss://, or unix://)", url_clean[:15] if url_clean else "")
        return None
    try:
        return aioredis.from_url(url_clean, decode_responses=True)
    except Exception as e:
        log.warning("Could not initialize Redis client from URL: %s", e)
        return None


class FailoverRedis:
    def __init__(self, primary_url: str, secondary_url: str = "", tertiary_url: str = "", quaternary_url: str = ""):
        self.primary = _safe_create_redis(primary_url)
        self.secondary = _safe_create_redis(secondary_url)
        self.tertiary = _safe_create_redis(tertiary_url)
        self.quaternary = _safe_create_redis(quaternary_url)

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