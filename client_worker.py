"""
client_worker.py — Cloud worker daemon.

Polls the website's job queue and runs the ClipAI pipeline for whatever
comes back. Runs entirely on infrastructure you control (a VM, a
container, a Render/Fly/Railway worker service, etc.) — it is NOT
meant to be distributed to end users as a desktop app. WORKER_SECRET
must be set in this process's environment and must match the value
the web server (main.py) uses; it authenticates worker <-> server
calls and is safe here specifically because it stays on infra you own.

Run with:
    python client_worker.py
or under a process manager (systemd, supervisor, a Docker CMD, etc.)
so it restarts automatically on crash.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from config import settings
from logging_setup import get_logger

log = get_logger("client_worker")

LANE_USER_ID = os.environ.get("WORKER_LANE_USER_ID", "cloud")
CONCURRENT_WORKERS = int(os.environ.get("CONCURRENT_WORKERS", "3"))
POLL_INTERVAL_SEC = float(os.environ.get("WORKER_POLL_INTERVAL_SEC", "2"))

_is_running = True


def _signed_token(user_id: str) -> str:
    if not settings.worker_secret:
        raise RuntimeError("WORKER_SECRET is not set — cannot authenticate to the API server.")
    return hmac.new(settings.worker_secret.encode(), user_id.encode(), hashlib.sha256).hexdigest()


def update_yt_dlp() -> None:
    """Keep the bundled yt-dlp binary current so YouTube-side format
    changes don't silently break downloads."""
    try:
        import yt_dlp
        log.info("yt-dlp version: %s", yt_dlp.version.__version__)
    except Exception as e:
        log.warning("Could not check yt-dlp version: %s", e)


def sync_live_pipeline_scripts() -> None:
    try:
        token = _signed_token(LANE_USER_ID)
        res = requests.get(
            f"{settings.api_base_url}/api/v1/worker/scripts",
            params={"user_id": LANE_USER_ID, "token": token},
            timeout=10,
        )
        if res.status_code == 200:
            scripts = res.json().get("scripts", {})
            base_dir = os.path.dirname(os.path.abspath(__file__))
            for name, code in scripts.items():
                try:
                    with open(os.path.join(base_dir, name), "w", encoding="utf-8") as f:
                        f.write(code)
                except Exception as e:
                    log.warning("Failed to write updated %s: %s", name, e)
            log.info("Live hot-sync complete: %d pipeline scripts updated.", len(scripts))
        else:
            log.warning("Hot-sync request failed with status %s", res.status_code)
    except Exception as e:
        log.warning("Live hot-sync warning (offline/cached): %s", e)


def _load_pipeline():
    import worker as worker_module
    import hot_pipeline as hot_pipeline_module
    importlib.reload(hot_pipeline_module)
    importlib.reload(worker_module)
    return worker_module


def process_job(worker_module, job: dict) -> None:
    job_id = job.get("job_id", "unknown")
    job_user_id = job.get("user_id", LANE_USER_ID)
    log.info("Processing job %s for user %s (niche=%r)", job_id, job_user_id, job.get("niche"))
    try:
        clip_job = worker_module.ClipJob.from_queue_payload(job)
        worker_module.run_clip_pipeline(clip_job)
    except Exception as pipeline_err:
        log.exception("Pipeline error on job %s", job_id)
        try:
            requests.post(
                f"{settings.api_base_url}/api/v1/worker/complete",
                json={"job_id": job_id, "status": "error", "message": str(pipeline_err)},
                params={"user_id": job_user_id},
                timeout=10,
            )
        except Exception:
            log.warning("Could not report pipeline error back to the server for job %s", job_id)


def run_worker_loop() -> None:
    global _is_running
    log.info("Starting ClipAI cloud worker (lane=%s)", LANE_USER_ID)

    update_yt_dlp()
    sync_live_pipeline_scripts()

    try:
        worker_module = _load_pipeline()
    except ImportError as e:
        log.error("Failed to load pipeline modules: %s", e)
        return

    executor = ThreadPoolExecutor(max_workers=CONCURRENT_WORKERS)
    log.info("Worker pool initialized with %d concurrent execution slots.", CONCURRENT_WORKERS)

    token = _signed_token(LANE_USER_ID)
    consecutive_errors = 0

    while _is_running:
        try:
            res = requests.get(
                f"{settings.api_base_url}/api/v1/worker/poll",
                params={"user_id": LANE_USER_ID, "token": token},
                timeout=10,
            )
            if res.status_code == 200:
                job = res.json().get("job")
                if job:
                    executor.submit(process_job, worker_module, job)
                consecutive_errors = 0
            else:
                consecutive_errors += 1
        except requests.exceptions.RequestException as e:
            consecutive_errors += 1
            log.warning("Poll request failed: %s", e)
        except Exception as e:
            consecutive_errors += 1
            log.exception("Unexpected polling error")

        sleep_for = POLL_INTERVAL_SEC * min(consecutive_errors + 1, 10)
        time.sleep(sleep_for)


def shutdown(*_args) -> None:
    global _is_running
    log.info("Shutdown signal received, stopping after current jobs finish...")
    _is_running = False


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    run_worker_loop()
