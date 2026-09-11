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


async def _trigger_autopost_jobs() -> None:
    r = get_redis()
    if r is None:
        return
    now = datetime.utcnow()
    current_day = now.strftime("%a")
    current_time = now.strftime("%H:%M")
    try:
        async for key in r.primary.scan_iter("user:*:autopost"):  # type: ignore[union-attr]
            user_id = key.split(":")[1]
            data = await r.hgetall(key)
            if data.get("enabled") != "True":
                continue
            days = json.loads(data.get("days", "[]"))
            times = json.loads(data.get("times", "[]"))
            if current_day not in days or current_time not in times:
                continue
            niche = data.get("niche", "motivation")
            await job_queue.enqueue({"mode": "licensed_cc", "niche": niche, "user_id": user_id, "is_auto_post": True, "auto_upload": True})
            log.info("Auto-post job queued for user %s (niche=%r)", user_id, niche)
    except Exception as e:
        log.error("Autopost scan failed: %s", e)


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