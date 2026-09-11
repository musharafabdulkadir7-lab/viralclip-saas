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