"""
video_downloader.py
Downloads a video + auto-captions for the two supported modes.

- own_content / uploaded file: nothing to download — handled by the
  caller via video_finder.register_uploaded_file().
- own_content / user's own channel, and licensed_cc: a single
  straightforward yt-dlp call, no client-fingerprint rotation and no
  proxy routing. Neither mode needs to evade bot-detection.
"""
from __future__ import annotations

import glob
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yt_dlp
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import settings
from logging_setup import get_logger

log = get_logger("video_downloader")


class DownloadError(Exception):
    """Raised when a video/subtitle download fails after retries."""


@dataclass
class DownloadResult:
    video_path: str
    sub_path: Optional[str] = None


def _get_ffmpeg_exe() -> str:
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "bin", "ffmpeg.exe")
    if sys.platform == "win32":
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return "ffmpeg"
    return "/usr/bin/ffmpeg"


class _TransientDownloadError(Exception):
    """Wraps yt-dlp failures so tenacity knows they're worth retrying."""


@retry(stop=stop_after_attempt(settings.max_retries), wait=wait_exponential(multiplier=1, min=2, max=10),
       retry=retry_if_exception_type(_TransientDownloadError), reraise=True)
def _run_ytdlp(ydl_opts: dict, url: str) -> None:
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as e:
        # Treat as transient (network blip, temporary rate limit) and let
        # tenacity retry a bounded number of times before giving up.
        raise _TransientDownloadError(str(e)) from e


def download_video_and_subs(url: str, video_id: str, start_sec: Optional[int] = None,
                             end_sec: Optional[int] = None) -> DownloadResult:
    """
    Downloads the video at 720p and its auto-generated subtitles via a
    single standard yt-dlp client — no cookies, no proxy, no client
    rotation. Retries a bounded number of times on transient failures;
    raises DownloadError if it never succeeds.
    """
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    existing_mp4 = settings.download_dir / f"{video_id}.mp4"
    existing_subs = glob.glob(str(settings.download_dir / f"{video_id}*.vtt"))

    if existing_mp4.exists() and existing_mp4.stat().st_size > 102_400:
        log.info("Using cached video: %s", existing_mp4)
        return DownloadResult(video_path=str(existing_mp4), sub_path=existing_subs[0] if existing_subs else None)

    output_template = str(settings.download_dir / f"{video_id}.%(ext)s")
    ffmpeg_exe = _get_ffmpeg_exe()
    ffmpeg_dir = os.path.dirname(ffmpeg_exe)
    if ffmpeg_dir and ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

    log.info("Downloading video: %s", url)

    ydl_opts = {
        "format": "best[height<=720][ext=mp4]/bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best",
        "outtmpl": output_template,
        "writeautomaticsub": True,
        "subtitleslangs": ["en"],
        "subtitlesformat": "vtt",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "retries": 5,
        "fragment_retries": 5,
        "nocheckcertificate": True,
        "ffmpeg_location": ffmpeg_exe,
    }
    if start_sec is not None and end_sec is not None:
        ydl_opts["download_ranges"] = lambda info, ydl: [{"start_time": start_sec, "end_time": end_sec}]
        log.info("Range-slicing active: %ss -> %ss", start_sec, end_sec)

    try:
        _run_ytdlp(ydl_opts, url)
    except Exception as e:
        raise DownloadError(f"Download failed after retries: {e}") from e

    video_files = glob.glob(str(settings.download_dir / f"{video_id}.mp4"))
    if not video_files:
        all_files = glob.glob(str(settings.download_dir / f"{video_id}.*"))
        video_files = [f for f in all_files if not any(f.endswith(ext) for ext in (".vtt", ".json", ".srt", ".ytdl"))]
    sub_files = glob.glob(str(settings.download_dir / f"{video_id}*.vtt"))

    if not video_files:
        raise DownloadError("Video file not found after download completed without error.")

    result = DownloadResult(video_path=video_files[0], sub_path=sub_files[0] if sub_files else None)
    log.info("Download complete: %s", result.video_path)
    return result
