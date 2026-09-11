"""
worker.py
Mode-branching pipeline entrypoint, usable as a library
(`run_clip_pipeline(...)`) or from the command line (`python worker.py ...`).

mode="own_content":
    source_kind="file"    -> local_path points at a file the user uploaded
    source_kind="channel" -> pick from the user's own connected YouTube channel
mode="licensed_cc":
    always searches YouTube Data API for Creative Commons licensed videos

Progress/completion is reported two ways:
  1. HTTP POST to API_BASE_URL (the existing website job-status API)
  2. An optional generic webhook (CLIPAI_WEBHOOK_URL), HMAC-signed with
     CLIPAI_WEBHOOK_SECRET if set — useful for hooking up something like
     Antigravity or any other external listener without coupling it to
     the website's specific API shape.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
from dataclasses import dataclass
from typing import Optional

import requests

from config import settings
from logging_setup import get_logger

log = get_logger("worker")

MODULES_AVAILABLE = True
try:
    import clip_cutter
    import clip_finder
    import hot_pipeline
    import video_downloader
    import video_finder
    import youtube_uploader
except ImportError as e:
    MODULES_AVAILABLE = False
    log.error("Video modules could not be imported (%s). Running in placeholder mode.", e)


class PipelineError(Exception):
    """Raised for any unrecoverable failure in run_clip_pipeline."""


# ── Status reporting ────────────────────────────────────────────────
def _send_webhook(event: dict) -> None:
    if not settings.webhook_url:
        return
    body = json.dumps(event).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if settings.webhook_secret:
        sig = hmac.new(settings.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-ClipAI-Signature"] = sig
    try:
        requests.post(settings.webhook_url, data=body, headers=headers, timeout=5)
    except Exception as e:
        log.warning("Webhook delivery failed: %s", e)


def update_job_status(job_id: str, status: str, progress: int, message: str,
                       url: str = "", title: str = "", niche: str = "", user_id: str = "") -> None:
    log.info("[%3d%%] %s: %s", progress, status, message)

    event = {"job_id": job_id, "status": status, "progress": progress, "message": message,
              "url": url, "title": title, "niche": niche, "user_id": user_id or "unknown"}
    _send_webhook(event)

    try:
        if status in ("complete", "draft_ready", "error"):
            requests.post(f"{settings.api_base_url}/api/v1/worker/complete", json={
                "job_id": job_id, "status": status, "message": message,
                "url": url, "title": title, "niche": niche,
            }, params={"user_id": user_id or "unknown"}, timeout=10)
        else:
            requests.post(f"{settings.api_base_url}/api/v1/worker/progress", json={
                "job_id": job_id, "status": status, "progress": progress, "message": message, "url": url,
            }, timeout=5)
    except Exception as e:
        log.warning("Failed to update cloud progress: %s", e)


def fetch_youtube_creds(user_id: str) -> Optional[dict]:
    if not settings.worker_secret:
        raise PipelineError("WORKER_SECRET is not configured.")
    try:
        token = hmac.new(settings.worker_secret.encode(), user_id.encode(), hashlib.sha256).hexdigest()
        res = requests.get(f"{settings.api_base_url}/api/v1/user/youtube-creds",
                            params={"user_id": user_id, "token": token}, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get("refresh_token"):
                return data
    except Exception as e:
        log.warning("Failed to fetch YouTube creds: %s", e)
    return None


# ── Job spec ────────────────────────────────────────────────────────
@dataclass
class ClipJob:
    mode: str                          # "own_content" | "licensed_cc"
    user_id: str
    job_id: str
    niche: str = ""
    source_kind: Optional[str] = None  # "file" | "channel"
    source: Optional[str] = None       # file path or channel video id
    auto_upload: bool = True
    layout: str = "cinematic_blur"     # "cinematic_blur" | "split_screen"
    broll_path: Optional[str] = None
    subtitle_style: str = "bold_captions"

    def validate(self) -> list[str]:
        problems = settings.validate_for_mode(self.mode)
        if self.mode == "own_content" and self.source_kind not in ("file", "channel"):
            problems.append("own_content mode requires source_kind of 'file' or 'channel'.")
        if self.mode not in ("own_content", "licensed_cc"):
            problems.append(f"Unknown mode: {self.mode!r}")
        if self.layout == "split_screen" and not self.broll_path:
            problems.append("split_screen layout requires broll_path (b-roll you own or have licensed).")
        return problems


def _source_video(job: ClipJob):
    if job.mode == "own_content":
        if job.source_kind == "file":
            video = video_finder.register_uploaded_file(job.source, title=job.niche or "My video")
            return video, video_downloader.DownloadResult(video_path=video.local_path, sub_path=None)

        creds_dict = fetch_youtube_creds(job.user_id)
        if not creds_dict:
            raise PipelineError("YouTube account not connected — required to pull from your own channel.")
        candidates = video_finder.get_own_channel_videos(creds_dict, max_results=10)
        if not candidates:
            raise PipelineError("No videos found on your connected channel.")
        video = next((c for c in candidates if c.id == job.source), candidates[0])
        dl = video_downloader.download_video_and_subs(video.url, video.id)
        return video, dl

    # licensed_cc
    candidates = video_finder.find_licensed_cc_videos(niche=job.niche)
    last_error: Optional[Exception] = None
    for i, candidate in enumerate(candidates):
        update_job_status(job.job_id, "running", 25 + i * 5,
                           f"Downloading: {candidate.title[:45]}...", user_id=job.user_id)
        try:
            dl = video_downloader.download_video_and_subs(candidate.url, candidate.id)
            return candidate, dl
        except video_downloader.DownloadError as e:
            last_error = e
            log.warning("Candidate %s failed, trying next: %s", candidate.id, e)
    raise PipelineError(f"All candidates failed to download: {last_error}")


def run_clip_pipeline(job: ClipJob) -> None:
    if not MODULES_AVAILABLE:
        update_job_status(job.job_id, "error", 0, "Video agent modules not found.", user_id=job.user_id)
        return

    problems = job.validate()
    if problems:
        update_job_status(job.job_id, "error", 0, "; ".join(problems), user_id=job.user_id)
        return

    try:
        # Hot-cache fast path (licensed_cc only)
        if job.mode == "licensed_cc":
            hot_pipeline.clear_stale_cache(keep_niche=job.niche)
            hot_clip = hot_pipeline.get_hot_clip(job.niche)
            if hot_clip and hot_clip.get("clip_paths"):
                clip_path = hot_clip["clip_paths"][0]
                clip_info = hot_clip.get("clip_info", {"caption": job.niche.title()})
                video_title = hot_clip.get("video_title", job.niche)
                attribution = hot_clip.get("attribution", "")
                update_job_status(job.job_id, "running", 80, "Instant clip ready — finishing up...", user_id=job.user_id)
                hot_pipeline.trigger_replenish(job.niche)
                _finish_and_publish(job, clip_path, clip_info.get("caption", job.niche.title()),
                                     video_title, attribution, video_id=hot_clip.get("video_id", ""))
                return

        update_job_status(job.job_id, "running", 10, "Finding source video...", user_id=job.user_id)
        video, dl = _source_video(job)

        update_job_status(job.job_id, "running", 50, "AI is selecting the best moment...", user_id=job.user_id)
        clip_info = clip_finder.find_best_segment(dl.sub_path, niche=job.niche or video.title) if dl.sub_path \
            else clip_finder.ClipSegment(start_sec=0, end_sec=50, caption=(job.niche or video.title)[:26])

        update_job_status(job.job_id, "running", 70, "Rendering Short...", user_id=job.user_id)
        watermark = f"@{(job.niche or 'MyChannel').replace(' ', '')}"

        clip_path = clip_cutter.cut_clip(
            video_path=dl.video_path, start_sec=clip_info.start_sec, end_sec=clip_info.end_sec,
            caption=clip_info.caption, watermark=watermark, sub_path=dl.sub_path,
            broll_path=job.broll_path, subtitle_style=job.subtitle_style,
        )

        if job.mode == "licensed_cc":
            hot_pipeline.trigger_replenish(job.niche)

        attribution = video.attribution if job.mode == "licensed_cc" else ""
        _finish_and_publish(job, clip_path, clip_info.caption, video.title, attribution, video_id=video.id)

    except (PipelineError, video_finder.VideoFinderError, video_downloader.DownloadError, clip_cutter.ClipCutError) as e:
        update_job_status(job.job_id, "error", 0, str(e), user_id=job.user_id)
    except Exception as e:
        log.exception("Unexpected pipeline error")
        update_job_status(job.job_id, "error", 0, f"Unexpected pipeline error: {e}", user_id=job.user_id)


def _finish_and_publish(job: ClipJob, clip_path: str, caption: str, video_title: str,
                         attribution: str, video_id: str) -> None:
    title = f"#Shorts {caption}"
    desc_lines = [caption, ""]
    if job.mode == "licensed_cc" and attribution:
        # Attribution is mandatory for licensed_cc, not optional.
        desc_lines += ["Source (Creative Commons):", attribution, ""]
    desc_lines.append(f"#Shorts{(' #' + job.niche.replace(' ', '')) if job.niche else ''}")
    desc = "\n".join(desc_lines)
    tags = ["Shorts"] + ([job.niche] if job.niche else [])

    if not job.auto_upload:
        update_job_status(job.job_id, "draft_ready", 100, "Rendered and ready for review.",
                           url=clip_path, title=title, niche=job.niche, user_id=job.user_id)
        return

    update_job_status(job.job_id, "running", 85, "Uploading to YouTube...", user_id=job.user_id)
    creds_dict = fetch_youtube_creds(job.user_id)
    if not creds_dict:
        update_job_status(job.job_id, "error", 85, "YouTube account not connected.", user_id=job.user_id)
        return

    def on_upload_progress(pct: int) -> None:
        update_job_status(job.job_id, "running", int(85 + pct * 0.13), f"Uploading ({pct}%)...", user_id=job.user_id)

    upload_res = youtube_uploader.upload_video_to_youtube(
        clip_path, title=title, description=desc, tags=tags,
        creds_dict=creds_dict, progress_callback=on_upload_progress,
    )

    if upload_res.get("status") == "success":
        if job.mode == "licensed_cc" and video_id:
            video_finder.mark_video_used(video_id, video_title)
        update_job_status(job.job_id, "complete", 100, "Done! Video is live on YouTube.",
                           upload_res.get("url", ""), title, job.niche, user_id=job.user_id)
    else:
        update_job_status(job.job_id, "error", 100, f"Upload failed: {upload_res.get('error')}", user_id=job.user_id)


# ── CLI ─────────────────────────────────────────────────────────────
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run the ClipAI clipping pipeline for one job.")
    p.add_argument("--mode", required=True, choices=["own_content", "licensed_cc"])
    p.add_argument("--user-id", required=True)
    p.add_argument("--job-id", default="cli-job")
    p.add_argument("--niche", default="")
    p.add_argument("--source-kind", choices=["file", "channel"], default=None)
    p.add_argument("--source", default=None, help="File path (source_kind=file) or channel video id (source_kind=channel)")
    p.add_argument("--no-upload", action="store_true", help="Render only — skip YouTube upload")
    p.add_argument("--layout", choices=["cinematic_blur", "split_screen"], default="cinematic_blur")
    p.add_argument("--broll-path", default=None)
    p.add_argument("--subtitle-style", choices=["bold_captions", "clean_minimal"], default="bold_captions")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    job = ClipJob(
        mode=args.mode, user_id=args.user_id, job_id=args.job_id, niche=args.niche,
        source_kind=args.source_kind, source=args.source, auto_upload=not args.no_upload,
        layout=args.layout, broll_path=args.broll_path, subtitle_style=args.subtitle_style,
    )
    problems = job.validate()
    if problems:
        for p in problems:
            log.error("Config problem: %s", p)
        return 1
    run_clip_pipeline(job)
    return 0


if __name__ == "__main__":
    sys.exit(main())
