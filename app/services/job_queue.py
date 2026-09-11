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