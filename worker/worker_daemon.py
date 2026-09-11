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
            requests.post(f"{settings.api_base_url}/api/v1/worker/complete",
                           json={"job_id": job_id, "status": "error", "message": str(e)},
                           params={"user_id": job_user_id}, timeout=10)
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