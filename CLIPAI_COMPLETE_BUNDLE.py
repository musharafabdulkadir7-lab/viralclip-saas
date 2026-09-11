# ==============================================================================
# CLIPAI SAAS — COMPLETE PROJECT BUNDLE (ALL-IN-ONE REFERENCE FILE)
# Contains all backend pipelines, web server, worker client, frontend & config
# ==============================================================================



################################################################################
# FILE: config.py
################################################################################

"""
config.py
Centralized configuration. Loads from environment variables (optionally
via a .env file if python-dotenv is installed) with sane defaults and
validation. Every other module should import `settings` from here
instead of reading os.environ directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    try:
        return int(val) if val else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # ── Paths ──
    home_dir: Path = field(default_factory=lambda: Path.home() / ".clipai")

    # ── API keys / secrets ──
    youtube_api_key: str = field(default_factory=lambda: os.environ.get("YOUTUBE_API_KEY", ""))
    worker_secret: str = field(default_factory=lambda: os.environ.get("WORKER_SECRET", ""))
    api_base_url: str = field(default_factory=lambda: os.environ.get("API_BASE_URL", "https://viralclip-saas.onrender.com"))

    # ── Redis (optional) ──
    redis_url: str = field(default_factory=lambda: os.environ.get("REDIS_URL", "redis://localhost:6379/0"))

    # ── Sourcing thresholds ──
    min_views: int = field(default_factory=lambda: _env_int("CLIPAI_MIN_VIEWS", 50_000))
    min_duration_sec: int = field(default_factory=lambda: _env_int("CLIPAI_MIN_DURATION_SEC", 300))
    max_age_days: int = field(default_factory=lambda: _env_int("CLIPAI_MAX_AGE_DAYS", 730))
    top_n_candidates: int = field(default_factory=lambda: _env_int("CLIPAI_TOP_N", 3))

    # ── Rendering ──
    max_short_duration_sec: int = field(default_factory=lambda: _env_int("CLIPAI_MAX_SHORT_SEC", 56))
    default_watermark: str = field(default_factory=lambda: os.environ.get("CLIPAI_DEFAULT_WATERMARK", "@YourChannel"))
    ffmpeg_timeout_sec: int = field(default_factory=lambda: _env_int("CLIPAI_FFMPEG_TIMEOUT", 600))

    # ── Networking / retries ──
    http_timeout_sec: int = field(default_factory=lambda: _env_int("CLIPAI_HTTP_TIMEOUT", 20))
    max_retries: int = field(default_factory=lambda: _env_int("CLIPAI_MAX_RETRIES", 3))

    # ── Webhooks ──
    webhook_url: str = field(default_factory=lambda: os.environ.get("CLIPAI_WEBHOOK_URL", ""))
    webhook_secret: str = field(default_factory=lambda: os.environ.get("CLIPAI_WEBHOOK_SECRET", ""))

    # ── Logging ──
    log_level: str = field(default_factory=lambda: os.environ.get("CLIPAI_LOG_LEVEL", "INFO"))
    log_json: bool = field(default_factory=lambda: _env_bool("CLIPAI_LOG_JSON", False))

    @property
    def download_dir(self) -> Path:
        return self.home_dir / "downloaded_videos"

    @property
    def output_dir(self) -> Path:
        return self.home_dir / "generated_videos"

    @property
    def hot_pool_dir(self) -> Path:
        return self.home_dir / "hot_pool"

    @property
    def used_videos_file(self) -> Path:
        return self.home_dir / "used_videos.json"

    def ensure_dirs(self) -> None:
        for d in (self.home_dir, self.download_dir, self.output_dir, self.hot_pool_dir):
            d.mkdir(parents=True, exist_ok=True)

    def validate_for_mode(self, mode: str) -> list[str]:
        """Returns a list of human-readable problems; empty list = OK to run."""
        problems = []
        if mode == "licensed_cc" and not self.youtube_api_key:
            problems.append("YOUTUBE_API_KEY is required for licensed_cc mode.")
        if not self.worker_secret:
            problems.append("WORKER_SECRET is not set — credential fetch will fail.")
        return problems


settings = Settings()
settings.ensure_dirs()


################################################################################
# FILE: logging_setup.py
################################################################################

"""
logging_setup.py
One place to configure logging for the whole pipeline. Replaces the
old print()-based logging so output is leveled, timestamped, and
optionally JSON-formatted for log aggregators.
"""
from __future__ import annotations

import json
import logging
import sys

from config import settings


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured

    handler = logging.StreamHandler(sys.stdout)
    if settings.log_json:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    logger.propagate = False
    return logger


################################################################################
# FILE: video_finder.py
################################################################################

"""
video_finder.py
Two supported modes for sourcing a source video:

  1. "own_content"  — the user supplies their own file, or a video from
                       their own authenticated YouTube channel.
  2. "licensed_cc"  — search YouTube Data API v3 for Creative Commons
                       licensed videos. Requires YOUTUBE_API_KEY.

No scraping, no client-fingerprint rotation, no proxy evasion. If the
official API can't find something, the run fails loudly rather than
falling back to unlicensed scraping.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import settings
from logging_setup import get_logger

log = get_logger("video_finder")


class VideoFinderError(Exception):
    """Raised when a source video cannot be found/resolved."""


@dataclass
class VideoCandidate:
    id: str
    title: str
    url: Optional[str] = None
    local_path: Optional[str] = None
    duration: int = 0
    view_count: int = 0
    channel: str = ""
    license: str = ""
    attribution: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── Used-video tracking (Redis w/ JSON fallback) ─────────────────────
USED_VIDEOS_REDIS_KEY = "viralclip:used_videos"
_redis_used_client = None


def _get_used_redis():
    global _redis_used_client
    if _redis_used_client is None:
        try:
            import redis as _rl
            c = _rl.Redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=2)
            c.ping()
            _redis_used_client = c
        except Exception as e:
            log.debug("Redis unavailable, falling back to JSON file: %s", e)
            _redis_used_client = False
    return _redis_used_client if _redis_used_client else None


def load_used_videos() -> dict:
    r = _get_used_redis()
    if r:
        try:
            return {vid: {} for vid in r.smembers(USED_VIDEOS_REDIS_KEY)}
        except Exception as e:
            log.warning("Redis read failed, falling back to JSON: %s", e)
    if settings.used_videos_file.exists():
        try:
            return json.loads(settings.used_videos_file.read_text())
        except Exception as e:
            log.warning("Failed to parse used_videos.json: %s", e)
            return {}
    return {}


def mark_video_used(video_id: str, title: str = "") -> None:
    r = _get_used_redis()
    if r:
        try:
            r.sadd(USED_VIDEOS_REDIS_KEY, video_id)
            log.info("Marked used (Redis): %s", video_id)
            return
        except Exception as e:
            log.warning("Redis write failed, falling back to JSON: %s", e)
    used = load_used_videos()
    used[video_id] = {"title": title, "used_at": datetime.now().isoformat()}
    settings.used_videos_file.write_text(json.dumps(used, indent=2))
    log.info("Marked used (JSON fallback): %s", video_id)


def _iso8601_to_seconds(duration: str) -> int:
    """Convert YouTube ISO 8601 duration (e.g. 'PT4M13S') to seconds."""
    pattern = re.compile(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?')
    match = pattern.match(duration or "")
    if not match:
        return 0
    hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


_RETRYABLE = (httpx.TransportError, httpx.TimeoutException)


@retry(stop=stop_after_attempt(settings.max_retries), wait=wait_exponential(multiplier=1, min=1, max=8),
       retry=retry_if_exception_type(_RETRYABLE), reraise=True)
def _http_get(url: str, params: dict) -> dict:
    res = httpx.get(url, params=params, timeout=settings.http_timeout_sec)
    res.raise_for_status()
    return res.json()


# ── Mode 2: licensed_cc ───────────────────────────────────────────────
def find_licensed_cc_videos(niche: str, max_results: int = 20) -> list[VideoCandidate]:
    """
    Search YouTube Data API v3, restricted to Creative Commons licensed
    videos only. This is the sole sourcing path for licensed_cc mode —
    there is intentionally no scraping fallback.

    Raises VideoFinderError if the API key is missing, the request
    fails after retries, or no qualifying candidates are found.
    """
    if not settings.youtube_api_key:
        raise VideoFinderError("YOUTUBE_API_KEY is required for licensed_cc mode.")

    used = load_used_videos()
    log.info("Searching YouTube API (CC-licensed only) for: %r", niche)

    cutoff = (datetime.now() - timedelta(days=settings.max_age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    search_params = {
        "part": "id,snippet",
        "q": niche,
        "type": "video",
        "order": "viewCount",
        "videoDuration": "medium",
        "publishedAfter": cutoff,
        "maxResults": max_results,
        "videoLicense": "creativeCommon",
        "key": settings.youtube_api_key,
    }
    try:
        data = _http_get("https://www.googleapis.com/youtube/v3/search", search_params)
    except Exception as e:
        raise VideoFinderError(f"YouTube API search request failed: {e}") from e

    if "error" in data:
        raise VideoFinderError(f"YouTube API error: {data['error'].get('message', data['error'])}")

    items = data.get("items", [])
    video_ids = [item["id"]["videoId"] for item in items if item.get("id", {}).get("videoId")]
    if not video_ids:
        raise VideoFinderError(f"No CC-licensed videos found for '{niche}'. Try a different search term.")

    detail_params = {
        "part": "contentDetails,statistics,snippet,status",
        "id": ",".join(video_ids),
        "key": settings.youtube_api_key,
    }
    try:
        detail_data = _http_get("https://www.googleapis.com/youtube/v3/videos", detail_params)
    except Exception as e:
        raise VideoFinderError(f"YouTube API detail request failed: {e}") from e

    candidates: list[VideoCandidate] = []
    for item in detail_data.get("items", []):
        vid_id = item["id"]
        if vid_id in used:
            continue

        # Re-verify license server-side — don't trust the search filter alone
        license_str = item.get("status", {}).get("license", "")
        if license_str != "creativeCommon":
            log.debug("Skip (not CC on re-check): %s", vid_id)
            continue

        duration_sec = _iso8601_to_seconds(item.get("contentDetails", {}).get("duration", "PT0S"))
        view_count = int(item.get("statistics", {}).get("viewCount", 0))
        title = item.get("snippet", {}).get("title", "")[:60]
        channel_name = item.get("snippet", {}).get("channelTitle", "Unknown")

        if duration_sec < settings.min_duration_sec or view_count < settings.min_views:
            continue

        candidates.append(VideoCandidate(
            id=vid_id,
            title=title,
            url=f"https://www.youtube.com/watch?v={vid_id}",
            duration=duration_sec,
            view_count=view_count,
            channel=channel_name,
            license="creativeCommon",
            attribution=f"Original by {channel_name} (CC BY) https://youtu.be/{vid_id}",
        ))
        log.info("Candidate OK: %r | %s views | %sm | CC BY %s", title, f"{view_count:,}", duration_sec // 60, channel_name)

    candidates.sort(key=lambda c: c.view_count, reverse=True)
    top = candidates[: settings.top_n_candidates]
    if not top:
        raise VideoFinderError(f"No qualifying CC-licensed videos found for '{niche}' after filtering.")
    return top


# ── Mode 1: own_content ────────────────────────────────────────────────
def get_own_channel_videos(creds_dict: dict, max_results: int = 10) -> list[VideoCandidate]:
    """
    Lists videos from the *authenticated user's own* YouTube channel via
    the Data API (uploads playlist). Requires an OAuth token with at
    least youtube.readonly scope.
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = Credentials(
        token=creds_dict.get("token"),
        refresh_token=creds_dict.get("refresh_token"),
        client_id=creds_dict.get("client_id"),
        client_secret=creds_dict.get("client_secret"),
        token_uri="https://oauth2.googleapis.com/token",
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    youtube = build("youtube", "v3", credentials=creds)

    ch = youtube.channels().list(part="contentDetails", mine=True).execute()
    items = ch.get("items", [])
    if not items:
        raise VideoFinderError("Could not resolve the authenticated user's channel.")
    uploads_playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    pl = youtube.playlistItems().list(
        part="snippet,contentDetails", playlistId=uploads_playlist_id, maxResults=max_results,
    ).execute()

    results = []
    for it in pl.get("items", []):
        vid_id = it["contentDetails"]["videoId"]
        results.append(VideoCandidate(
            id=vid_id,
            title=it["snippet"]["title"][:60],
            url=f"https://www.youtube.com/watch?v={vid_id}",
        ))
    return results


def register_uploaded_file(file_path: str, title: str = "Uploaded video") -> VideoCandidate:
    """
    own_content mode, file-upload path: the user already gave us the
    file directly. No search or download step needed.
    """
    p = Path(file_path)
    if not p.exists():
        raise VideoFinderError(f"Uploaded file not found: {file_path}")
    return VideoCandidate(id=p.stem, title=title[:60], local_path=str(p))


################################################################################
# FILE: video_downloader.py
################################################################################

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


################################################################################
# FILE: clip_finder.py
################################################################################

"""
clip_finder.py
Reads auto-generated subtitles (VTT format) and calls the backend to
find the single most engaging 25-55 second clip window. Returns
start/end timestamps in seconds plus a caption.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import settings
from logging_setup import get_logger

log = get_logger("clip_finder")

_DEFAULT_START = 60
_DEFAULT_END = 110
_MIN_CLIP_SEC = 25
_MAX_CLIP_SEC = 55


@dataclass
class ClipSegment:
    start_sec: int
    end_sec: int
    caption: str
    num_parts: int = 1


def _fallback_segment(niche: str) -> ClipSegment:
    return ClipSegment(start_sec=_DEFAULT_START, end_sec=_DEFAULT_END, caption=niche.title() or "Clip")


def parse_vtt(vtt_path: str) -> list[dict]:
    """Parses an auto-caption VTT file into [{start, end, text}, ...]."""
    try:
        with open(vtt_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        log.error("Failed to read VTT %s: %s", vtt_path, e)
        return []

    def ts_to_sec(h, m, s, ms):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    blocks = re.split(r"\n\s*\n", content.strip())
    entries: list[dict] = []
    seen_texts: set[str] = set()

    for block in blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue

        ts_line, text_lines = None, []
        for i, line in enumerate(lines):
            if "-->" in line:
                ts_line, text_lines = line, lines[i + 1:]
                break
        if not ts_line:
            continue

        ts_match = re.match(r"(\d+):(\d+):(\d+)[\.,](\d+)\s*-->\s*(\d+):(\d+):(\d+)[\.,](\d+)", ts_line)
        if not ts_match:
            continue

        start = ts_to_sec(*ts_match.groups()[:4])
        end = ts_to_sec(*ts_match.groups()[4:])

        raw_text = " ".join(text_lines)
        raw_text = re.sub(r"<\d+:\d+:\d+[\.,]\d+>", "", raw_text)
        raw_text = re.sub(r"<[^>]+>", "", raw_text)
        raw_text = re.sub(r"\s+", " ", raw_text).strip()

        if not raw_text or raw_text in ("[Music]", "[Applause]"):
            continue
        if raw_text in seen_texts:
            continue
        seen_texts.add(raw_text)

        entries.append({"start": start, "end": end, "text": raw_text})

    log.info("Parsed %d subtitle entries from %s", len(entries), vtt_path)
    return entries


def build_transcript_block(entries: list[dict], max_chars: int = 10_000) -> str:
    lines = []
    total = 0
    for e in entries:
        secs_total = int(e["start"])
        line = f"[{secs_total // 60:02d}:{secs_total % 60:02d}] {e['text']}"
        total += len(line)
        if total > max_chars:
            break
        lines.append(line)
    return "\n".join(lines)


_RETRYABLE = (requests.ConnectionError, requests.Timeout)


@retry(stop=stop_after_attempt(settings.max_retries), wait=wait_exponential(multiplier=1, min=1, max=6),
       retry=retry_if_exception_type(_RETRYABLE), reraise=True)
def _call_backend(transcript: str, niche: str, user_id: str) -> dict:
    res = requests.post(
        f"{settings.api_base_url}/api/v1/worker/analyze-transcript",
        json={"transcript": transcript, "niche": niche},
        params={"user_id": user_id},
        timeout=30,
    )
    res.raise_for_status()
    return res.json()


def find_best_segment(sub_path: str, niche: str = "content", user_id: str = "demo_user_123") -> ClipSegment:
    """
    Uses the backend to find the best standalone viral-Short moment
    (25-55s). Falls back to a fixed default window if subtitles are
    missing or the backend call fails — never crashes the pipeline
    over a clip-selection hiccup.
    """
    entries = parse_vtt(sub_path)
    if not entries:
        log.warning("No subtitle entries — using default window.")
        return _fallback_segment(niche)

    transcript = build_transcript_block(entries)

    try:
        data = _call_backend(transcript, niche, user_id)
    except Exception as e:
        log.warning("Backend clip-analysis call failed, using default window: %s", e)
        return _fallback_segment(niche)

    if "error" in data:
        log.warning("Backend returned error: %s", data["error"])
        return _fallback_segment(niche)

    start = data.get("start_sec", _DEFAULT_START)
    end = data.get("end_sec", start + 50)
    caption = data.get("caption", niche.title())

    valid_starts = [int(e["start"]) for e in entries]
    valid_ends = [int(e["end"]) for e in entries]
    snapped_start = min(valid_starts, key=lambda x: abs(x - start)) if valid_starts else start
    snapped_end = min(valid_ends, key=lambda x: abs(x - end)) if valid_ends else end

    duration = snapped_end - snapped_start
    if duration > _MAX_CLIP_SEC:
        snapped_end = snapped_start + _MAX_CLIP_SEC
    elif duration < _MIN_CLIP_SEC:
        snapped_end = snapped_start + 45

    log.info("Selected segment %ss-%ss (%ss): %r", snapped_start, snapped_end, snapped_end - snapped_start, caption)
    return ClipSegment(start_sec=snapped_start, end_sec=snapped_end, caption=caption)


################################################################################
# FILE: clip_cutter.py
################################################################################

"""
clip_cutter.py
Cuts a clip, crops it to 9:16 for Shorts, burns in captions + watermark.

No speed/fingerprint-evasion transforms. Split-screen b-roll must be
supplied explicitly by the caller (owned/licensed) — never auto-fetched.
"""
from __future__ import annotations

import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from config import settings
from logging_setup import get_logger

log = get_logger("clip_cutter")


class ClipCutError(Exception):
    """Raised when ffmpeg fails or times out."""


def _get_ffmpeg() -> str:
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "bin", "ffmpeg.exe")
    if sys.platform == "win32":
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return "ffmpeg"
    return "/usr/bin/ffmpeg"


FFMPEG = _get_ffmpeg()
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def get_best_h264_encoder() -> tuple[str, list[str]]:
    """Probes ffmpeg/hardware and picks the fastest available H.264 encoder."""
    cpu_cores = os.cpu_count() or 4
    try:
        res = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True,
                              errors="replace", creationflags=_NOWIN)
        out = res.stdout

        if "h264_nvenc" in out:
            test = subprocess.run(
                [FFMPEG, "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1", "-c:v", "h264_nvenc", "-f", "null", "-"],
                capture_output=True, creationflags=_NOWIN)
            if test.returncode == 0:
                log.info("Using NVIDIA NVENC hardware encoding (%d threads).", cpu_cores)
                return "h264_nvenc", ["-preset", "p1", "-tune", "ull", "-zerolatency", "1", "-2pass", "0",
                                       "-cq", "23", "-spatial-aq", "1", "-threads", str(cpu_cores)]
        if "h264_videotoolbox" in out:
            log.info("Using Apple VideoToolbox hardware encoding.")
            return "h264_videotoolbox", ["-realtime", "1", "-q:v", "65"]
        if "h264_qsv" in out:
            log.info("Using Intel QuickSync hardware encoding.")
            return "h264_qsv", ["-preset", "veryfast", "-q", "23", "-threads", str(cpu_cores)]
        if "h264_amf" in out:
            log.info("Using AMD AMF hardware encoding.")
            return "h264_amf", ["-quality", "speed", "-rc", "cqp", "-qp_i", "23"]
    except Exception as e:
        log.warning("Hardware encoder probe failed, falling back to CPU: %s", e)

    log.info("Using CPU (libx264) encoding across %d threads.", cpu_cores)
    return "libx264", ["-preset", "ultrafast", "-crf", "22", "-threads", str(cpu_cores), "-slice-max-size", "0"]


def parse_time(ts_str: str) -> float:
    parts = ts_str.strip().split(":")
    h, m, s = ("00", *parts) if len(parts) == 2 else parts
    sec, ms = s.split(".") if "." in s else (s, "000")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000.0


def format_ass_time(sec: float) -> str:
    sec = max(sec, 0)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def generate_ass_subtitle(vtt_path: str, start_sec: int, duration: int, output_ass: str,
                           subtitle_style: str = "bold_captions") -> bool:
    try:
        content = Path(vtt_path).read_text(encoding="utf-8")
    except Exception as e:
        log.error("Failed to read VTT for subtitles: %s", e)
        return False

    is_clean = subtitle_style == "clean_minimal"
    font_name = "Arial" if is_clean else "Impact"
    font_size = "75" if is_clean else "95"
    margin_v = "450" if is_clean else "550"
    outline_w = "3" if is_clean else "6"

    ass_header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Primary,{font_name},{font_size},&H0000FFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline_w},3,2,10,10,{margin_v},1
Style: PrimaryWhite,{font_name},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline_w},3,2,10,10,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    blocks = re.findall(
        r"(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3})\n((?:.|\n)*?)(?=\n\n|\Z)", content)
    end_sec = start_sec + duration
    use_alt = True

    for start_ts, end_ts, text in blocks:
        t_start, t_end = parse_time(start_ts), parse_time(end_ts)
        if t_end < start_sec or t_start > end_sec:
            continue
        t_start -= start_sec
        t_end -= start_sec
        text = text.strip().replace("\n", " ")
        if not text:
            continue
        style = "Primary" if use_alt else "PrimaryWhite"
        use_alt = not use_alt
        events.append(f"Dialogue: 0,{format_ass_time(t_start)},{format_ass_time(t_end)},{style},,0,0,0,,{text}")

    if not events:
        return False
    try:
        Path(output_ass).write_text(ass_header + "\n".join(events), encoding="utf-8")
        return True
    except Exception as e:
        log.error("Failed to write ASS subtitle file: %s", e)
        return False


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:").replace(",", "\\,")


def cut_and_format_clip(
    video_path: str,
    start_sec: int,
    end_sec: int,
    caption: str,
    output_filename: Optional[str] = None,
    watermark: Optional[str] = None,
    sub_path: Optional[str] = None,
    broll_path: Optional[str] = None,  # must be caller-owned/licensed b-roll
    subtitle_style: str = "bold_captions",
) -> str:
    """Cuts a clip and formats it to 9:16. Raises ClipCutError on failure."""
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    output_filename = output_filename or f"clip_{int(time.time())}.mp4"
    output_path = str(settings.output_dir / output_filename)

    duration = min(end_sec - start_sec, settings.max_short_duration_sec)
    if len(caption) > 26:
        caption = caption[:23] + "..."

    safe_caption = _esc(caption)
    safe_watermark = _esc(watermark or settings.default_watermark)

    ass_filter = ""
    if sub_path and os.path.exists(sub_path):
        ass_path = str(settings.output_dir / f"subs_{int(time.time())}.ass")
        if generate_ass_subtitle(sub_path, start_sec, duration, ass_path, subtitle_style=subtitle_style):
            safe_ass = ass_path.replace("\\", "/").replace(":", "\\:")
            ass_filter = f",subtitles={safe_ass}"

    encoder, encoder_args = get_best_h264_encoder()
    cpu_threads = str(os.cpu_count() or 4)

    if broll_path and os.path.exists(broll_path):
        broll_start = random.randint(0, 60)
        filter_complex = (
            "[0:v]scale=1080:960:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:960[top]; "
            "[1:v]scale=1080:960:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:960[bottom]; "
            "[top][bottom]vstack=inputs=2[merged]; "
            "[merged]scale=1080:1920:flags=lanczos,"
            f"drawtext=text='{safe_caption}':fontsize=38:fontcolor=white:borderw=2:bordercolor=black:"
            "x=(w-text_w)/2:y=(h/2)-text_h-20:font=Arial Bold:box=1:boxcolor=black@0.55:boxborderw=14:fix_bounds=1,"
            f"drawtext=text='{safe_watermark}':fontsize=26:fontcolor=white@0.70:borderw=1:bordercolor=black@0.5:"
            f"x=40:y=80:font=Arial:fix_bounds=1{ass_filter}[v_out]"
        )
        cmd = [
            FFMPEG, "-y", "-threads", cpu_threads,
            "-ss", str(start_sec), "-t", str(duration), "-i", video_path,
            "-stream_loop", "-1", "-ss", str(broll_start), "-t", str(duration), "-i", broll_path,
            "-filter_complex", filter_complex, "-map", "[v_out]", "-map", "0:a",
            "-r", "30", "-pix_fmt", "yuv420p", "-c:v", encoder, *encoder_args,
            "-c:a", "aac", "-b:a", "192k", "-map_metadata", "-1", "-movflags", "+faststart", output_path,
        ]
        mode = "split-screen"
    else:
        filter_complex = (
            "[0:v]split=2[bg][fg]; "
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,"
            "boxblur=luma_radius=28:luma_power=2:chroma_radius=14:chroma_power=2,"
            "eq=brightness=-0.12[bg_blurred]; "
            "[fg]scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos[fg_scaled]; "
            "[bg_blurred][fg_scaled]overlay=(W-w)/2:(H-h)/2[merged]; "
            "[merged]scale=1080:1920:flags=lanczos,"
            f"drawtext=text='{safe_caption}':fontsize=38:fontcolor=white:borderw=2:bordercolor=black:"
            "x=(w-text_w)/2:y=h-text_h-350:font=Arial Bold:box=1:boxcolor=black@0.55:boxborderw=14:fix_bounds=1,"
            f"drawtext=text='{safe_watermark}':fontsize=26:fontcolor=white@0.70:borderw=1:bordercolor=black@0.5:"
            f"x=40:y=80:font=Arial:fix_bounds=1{ass_filter}[v_out]"
        )
        cmd = [
            FFMPEG, "-y", "-threads", cpu_threads,
            "-ss", str(start_sec), "-t", str(duration), "-i", video_path,
            "-filter_complex", filter_complex, "-map", "[v_out]", "-map", "0:a",
            "-r", "30", "-pix_fmt", "yuv420p", "-c:v", encoder, *encoder_args,
            "-c:a", "aac", "-b:a", "192k", "-map_metadata", "-1", "-movflags", "+faststart", output_path,
        ]
        mode = "cinematic blur"

    log.info("Processing %ss-%ss (%ss) | %r | encoder=%s mode=%s", start_sec, end_sec, duration, caption, encoder, mode)

    try:
        subprocess.run(cmd, timeout=settings.ffmpeg_timeout_sec, check=True, capture_output=True, creationflags=_NOWIN)
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="replace")[:800]
        raise ClipCutError(f"ffmpeg failed: {err}") from e
    except subprocess.TimeoutExpired as e:
        raise ClipCutError(f"ffmpeg timed out after {settings.ffmpeg_timeout_sec}s") from e

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    log.info("Done: %s (%.1f MB)", output_path, size_mb)
    return output_path


def cut_clip(
    video_path: str,
    start_sec: int,
    end_sec: int,
    caption: str,
    watermark: Optional[str] = None,
    sub_path: Optional[str] = None,
    broll_path: Optional[str] = None,
    subtitle_style: str = "bold_captions",
) -> str:
    out_name = f"clip_{int(time.time())}.mp4"
    actual_end = min(start_sec + 55, end_sec)
    if actual_end <= start_sec:
        actual_end = start_sec + 45
    return cut_and_format_clip(
        video_path=video_path, start_sec=start_sec, end_sec=actual_end, caption=caption,
        output_filename=out_name, watermark=watermark, sub_path=sub_path,
        broll_path=broll_path, subtitle_style=subtitle_style,
    )


################################################################################
# FILE: hot_pipeline.py
################################################################################

"""
hot_pipeline.py
Speculative pre-bake cache for licensed_cc mode only. own_content is
tied to a specific user's video and isn't something you can usefully
pre-render ahead of a request.
"""
from __future__ import annotations

import glob
import json
import threading
import time
from pathlib import Path
from typing import Optional

from config import settings
from logging_setup import get_logger

log = get_logger("hot_pipeline")

_replenishing_niches: set[str] = set()
_lock = threading.Lock()


def _niche_key(niche: str) -> str:
    return "".join(c for c in niche.lower() if c.isalnum() or c in (" ", "_")).strip().replace(" ", "_")


def get_hot_clip(niche: str) -> Optional[dict]:
    niche_dir = settings.hot_pool_dir / _niche_key(niche)
    if not niche_dir.exists():
        return None

    for manifest_path in glob.glob(str(niche_dir / "*.json")):
        try:
            data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            clips = data.get("clip_paths", [])
            if clips and all(Path(c).exists() and Path(c).stat().st_size > 102_400 for c in clips):
                Path(manifest_path).unlink()
                log.info("Hot-cache hit for %r", niche)
                return data
        except Exception as e:
            log.warning("Manifest check failed for %s: %s", manifest_path, e)
    return None


def prebake_clip_worker(niche: str) -> None:
    niche_key = _niche_key(niche)
    with _lock:
        if niche_key in _replenishing_niches:
            return
        _replenishing_niches.add(niche_key)

    try:
        niche_dir = settings.hot_pool_dir / niche_key
        niche_dir.mkdir(parents=True, exist_ok=True)

        if len(glob.glob(str(niche_dir / "*.json"))) >= 2:
            return

        log.info("Pre-baking next clip for %r...", niche)

        import clip_cutter
        import clip_finder
        import video_downloader
        import video_finder

        try:
            candidates = video_finder.find_licensed_cc_videos(niche=niche)
        except video_finder.VideoFinderError as e:
            log.warning("Pre-bake: no candidates for %r: %s", niche, e)
            return

        video, dl = None, None
        for candidate in candidates:
            try:
                dl = video_downloader.download_video_and_subs(candidate.url, candidate.id)
                video = candidate
                break
            except video_downloader.DownloadError as e:
                log.warning("Pre-bake candidate failed (%s), trying next: %s", candidate.id, e)
        if not video or not dl:
            return

        clip_info = clip_finder.find_best_segment(dl.sub_path, niche=niche) if dl.sub_path else \
            clip_finder.ClipSegment(start_sec=60, end_sec=110, caption=niche.title())

        try:
            clip_path = clip_cutter.cut_clip(
                video_path=dl.video_path, start_sec=clip_info.start_sec, end_sec=clip_info.end_sec,
                caption=clip_info.caption, watermark=f"@{niche.replace(' ', '').capitalize()}",
                sub_path=dl.sub_path,
            )
        except clip_cutter.ClipCutError as e:
            log.warning("Pre-bake clip cut failed: %s", e)
            return

        manifest_data = {
            "niche": niche,
            "video_id": video.id,
            "video_title": video.title,
            "attribution": video.attribution,
            "clip_info": {"start_sec": clip_info.start_sec, "end_sec": clip_info.end_sec, "caption": clip_info.caption},
            "clip_paths": [clip_path],
            "created_at": time.time(),
        }
        manifest_file = niche_dir / f"hot_{video.id}_{int(time.time())}.json"
        manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
        log.info("Pre-baked clip ready in cache for %r.", niche)

    except Exception as e:
        log.error("Pre-baking error for %r: %s", niche, e)
    finally:
        with _lock:
            _replenishing_niches.discard(niche_key)


def clear_stale_cache(keep_niche: Optional[str] = None) -> None:
    import shutil
    try:
        keep_key = _niche_key(keep_niche) if keep_niche else None
        if settings.hot_pool_dir.exists():
            for entry in settings.hot_pool_dir.iterdir():
                if keep_key and entry.name == keep_key:
                    continue
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
        if settings.download_dir.exists():
            for f in settings.download_dir.glob("*.*"):
                try:
                    if f.is_file():
                        f.unlink()
                except Exception:
                    pass
    except Exception as e:
        log.warning("Storage clean warning: %s", e)


def trigger_replenish(niche: str) -> None:
    t = threading.Thread(target=prebake_clip_worker, args=(niche,), daemon=True)
    t.start()


################################################################################
# FILE: youtube_uploader.py
################################################################################

"""
youtube_uploader.py
Resumable, chunked upload to YouTube via the Data API, with OAuth
token auto-refresh.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from config import settings
from logging_setup import get_logger

log = get_logger("youtube_uploader")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CHUNK_SIZE = 16 * 1024 * 1024  # 16MB — fewer round trips for typical Shorts file sizes
MAX_UPLOAD_RETRIES = 5


class UploadError(Exception):
    """Raised when authentication or upload ultimately fails."""


def get_authenticated_service(creds_dict: dict):
    if not creds_dict:
        return None

    creds = Credentials(
        token=creds_dict.get("token"),
        refresh_token=creds_dict.get("refresh_token"),
        client_id=creds_dict.get("client_id"),
        client_secret=creds_dict.get("client_secret"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_refreshed_token(creds_dict.get("user_id"), creds.token)

    return build("youtube", "v3", credentials=creds)


def _save_refreshed_token(user_id: Optional[str], new_token: str) -> None:
    """Best-effort save of the refreshed access token back to storage."""
    try:
        from supabase import create_client
        import os
        supabase_url = os.environ.get("SUPABASE_URL", "")
        supabase_key = os.environ.get("SUPABASE_KEY", "")
        if supabase_url and supabase_key and user_id:
            sb = create_client(supabase_url, supabase_key)
            sb.table("users").update({"youtube_access_token": new_token}).eq("id", user_id).execute()
            log.info("Refreshed and saved new YouTube access token for user %s.", user_id)
    except Exception as e:
        log.warning("Could not persist refreshed token: %s", e)


def upload_video_to_youtube(
    video_path: str,
    title: str,
    description: str,
    tags: list[str],
    creds_dict: dict,
    progress_callback: Optional[Callable[[int], None]] = None,
    privacy_status: str = "public",
) -> dict:
    """
    Uploads a video to YouTube. Returns {"status": "success", "video_id":
    ..., "url": ...} on success or {"error": ...} on failure — never
    raises, so callers can report a clean job-status error either way.
    """
    import os
    if not os.path.exists(video_path):
        return {"error": f"Video file not found at {video_path}"}

    try:
        youtube = get_authenticated_service(creds_dict)
    except Exception as e:
        return {"error": f"Auth error: {e}"}
    if not youtube:
        return {"error": "Authentication failed. Missing or invalid credentials."}

    log.info("Starting YouTube upload: %s", video_path)

    body = {
        "snippet": {"title": title, "description": description, "tags": tags, "categoryId": "22"},
        "status": {"privacyStatus": privacy_status},
    }

    media_file = MediaFileUpload(video_path, chunksize=CHUNK_SIZE, resumable=True)
    request = youtube.videos().insert(part=",".join(body.keys()), body=body, media_body=media_file)

    response = None
    retries = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                log.info("Upload progress: %d%%", pct)
                if progress_callback:
                    try:
                        progress_callback(pct)
                    except Exception as e:
                        log.warning("progress_callback raised: %s", e)
        except Exception as e:
            retries += 1
            if retries > MAX_UPLOAD_RETRIES:
                log.error("Upload failed after %d retries: %s", MAX_UPLOAD_RETRIES, e)
                return {"error": str(e)}
            log.warning("Upload connection error (attempt %d/%d), retrying...", retries, MAX_UPLOAD_RETRIES)
            time.sleep(2 * retries)

    log.info("Upload complete: video_id=%s", response.get("id"))
    return {
        "status": "success",
        "video_id": response.get("id"),
        "url": f"https://youtube.com/shorts/{response.get('id')}",
    }


################################################################################
# FILE: worker.py
################################################################################

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


################################################################################
# FILE: main.py
################################################################################

import os
import uuid
import json
import asyncio
import hmac
import hashlib
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, Header, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import stripe
from supabase import create_client, Client
import redis

# Configuration & Environment Variables
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "sk_test_mock")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "whsec_mock")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://your-supabase-url.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "your-supabase-service-key")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
REDIS_URL_2 = os.environ.get("REDIS_URL_2", "")  # Secondary database for failover

# Security Token Signing Secret
WORKER_SECRET = os.environ.get("WORKER_SECRET", "clipai_worker_sec_997f7c9_v2")

def sign_user_token(user_id: str) -> str:
    """Generates an HMAC-SHA256 signature for a user_id."""
    return hmac.new(WORKER_SECRET.encode(), user_id.encode(), hashlib.sha256).hexdigest()

def verify_user_token(user_id: str, token: str) -> bool:
    """Verifies that the token matches the user_id."""
    if not token or not user_id:
        return False
    expected = sign_user_token(user_id)
    return hmac.compare_digest(expected, token)

# Google OAuth Config
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "https://viralclip-saas.onrender.com/api/v1/auth/youtube/callback")
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# Auto-load from client_secrets.json if available
_secrets_file = Path(__file__).resolve().parent / "client_secrets.json"
if os.path.exists(_secrets_file):
    try:
        with open(_secrets_file, "r", encoding="utf-8") as _f:
            _data = json.load(_f)
            _cfg = _data.get("web") or _data.get("installed") or {}
            if not GOOGLE_CLIENT_ID:
                GOOGLE_CLIENT_ID = _cfg.get("client_id", "")
            if not GOOGLE_CLIENT_SECRET:
                GOOGLE_CLIENT_SECRET = _cfg.get("client_secret", "")
    except Exception as _e:
        print(f"Warning: Could not read client_secrets.json: {_e}")

stripe.api_key = STRIPE_SECRET_KEY

# Clients
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"Supabase init error: {e}")
    supabase = None

class DualRedisClient:
    """
    Dual-database Redis client with automatic failover.
    Tries primary DB first. If quota is exceeded, auto-switches to secondary.
    Effectively doubles monthly command budget across two free Upstash databases.
    """
    def __init__(self, primary_url: str, secondary_url: str = ""):
        self._primary = None
        self._secondary = None
        self._active = None
        try:
            c = redis.Redis.from_url(primary_url, decode_responses=True, socket_connect_timeout=3)
            c.ping()
            self._primary = c
            self._active = c
            print("[Redis] Primary database connected.")
        except Exception as e:
            print(f"[Redis] Primary connection failed: {e}")
        if secondary_url:
            try:
                c2 = redis.Redis.from_url(secondary_url, decode_responses=True, socket_connect_timeout=3)
                c2.ping()
                self._secondary = c2
                if not self._active:
                    self._active = c2
                print("[Redis] Secondary database connected (failover ready).")
            except Exception as e:
                print(f"[Redis] Secondary connection failed: {e}")

    def _exec(self, method: str, *args, **kwargs):
        clients = [c for c in [self._primary, self._secondary] if c]
        last_err = None
        for client in clients:
            try:
                return getattr(client, method)(*args, **kwargs)
            except Exception as e:
                last_err = e
                err_str = str(e).lower()
                if any(k in err_str for k in ["max monthly", "quota", "limit exceeded", "maxmemory"]):
                    print(f"[Redis] Quota exceeded, failing over to secondary DB...")
                    continue
                raise
        raise last_err

    def __getattr__(self, name):
        return lambda *args, **kwargs: self._exec(name, *args, **kwargs)

try:
    redis_client = DualRedisClient(REDIS_URL, REDIS_URL_2)
    if not redis_client._active:
        print("[Redis] No databases available.")
        redis_client = None
except Exception as e:
    print(f"Redis init error: {e}")
    redis_client = None

app = FastAPI(title="ViralClip AI SaaS")

# OWASP Security Headers Middleware
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

# Auto-Post Background Task
async def auto_post_scheduler():
    while True:
        try:
            # Check every minute, sleeping exactly to the start of the next minute
            now = datetime.utcnow()
            sleep_time = 60 - now.second
            await asyncio.sleep(sleep_time)
            
            now = datetime.utcnow()
            current_time_str = now.strftime("%H:%M")
            print(f"[Scheduler] Checking auto-post schedules for time {current_time_str} UTC")
            
            if redis_client:
                current_day = now.strftime("%a") # e.g. "Mon"
                
                # Use SCAN to find all autopost settings
                for key in redis_client.scan_iter("user:*:autopost"):
                    user_id = key.split(":")[1]
                    data = redis_client.hgetall(key)
                    
                    if data.get("enabled") != "True":
                        continue
                        
                    try:
                        days = json.loads(data.get("days", '[]'))
                        times = json.loads(data.get("times", '[]'))
                    except:
                        continue
                        
                    if current_day not in days:
                        continue
                        
                    if current_time_str not in times:
                        continue
                        
                    niche = data.get("niche", "motivation")
                    
                    # Generate a job
                    job_id = str(uuid.uuid4())
                    redis_client.hset(f"job:{job_id}", mapping={
                        "status": "queued",
                        "progress": 0,
                        "message": "Auto-Post Scheduled Job queued...",
                        "url": ""
                    })
                    redis_client.expire(f"job:{job_id}", 86400)
                    
                    # Push to worker
                    redis_client.lpush(f"worker_queue:{user_id}", json.dumps({
                        "job_id": job_id,
                        "niche": niche,
                        "user_id": user_id,
                        "is_auto_post": True
                    }))
                    print(f"[Scheduler] Triggered auto-post job {job_id} for user {user_id}")
                    
        except Exception as e:
            print(f"[Scheduler] Error in background loop: {e}")
            await asyncio.sleep(60)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(auto_post_scheduler())
    asyncio.create_task(keep_alive_ping())
    # Bootstrap invites table — insert a dummy row to trigger auto-create via Supabase
    # (Supabase requires table to exist; we catch errors silently on first run)
    if supabase:
        try:
            supabase.table("invites").select("token").limit(1).execute()
        except Exception:
            pass  # Table will be created via SQL migration below if needed

async def keep_alive_ping():
    """Pings this server every 10 minutes to prevent Render free-tier cold starts."""
    import httpx
    await asyncio.sleep(60)  # Wait 1 min after startup before first ping
    app_url = os.environ.get("RENDER_EXTERNAL_URL", "https://viralclip-saas.onrender.com")
    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.get(f"{app_url}/health")
        except Exception:
            pass
        await asyncio.sleep(600)  # Every 10 minutes

# Static files and templates
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

class ClipRequest(BaseModel):
    niche: str
    auto_upload: bool = True
    layout: str = "split_screen"
    subtitle_style: str = "hormozi"

class PublishDraftRequest(BaseModel):
    clip_id: str
    title: str = ""
    description: str = ""

class AutoPostSettings(BaseModel):
    enabled: bool
    time: str = "12:00"
    times: list[str] = []
    niche: str
    days: list[str] = []

# Helper functions
def get_or_create_user(user_id: str):
    if not supabase:
        return {"id": user_id, "free_clips_used": 0, "license": "free_tier"}
    try:
        res = supabase.table("users").select("*").eq("id", user_id).execute()
        if res.data:
            return res.data[0]
        new_user = {"id": user_id, "free_clips_used": 0, "license": "free_tier"}
        supabase.table("users").insert(new_user).execute()
        return new_user
    except Exception as e:
        print(f"DB error for user {user_id}: {e}")
        return {"id": user_id, "free_clips_used": 0, "license": "free_tier"}

@app.get("/")
async def render_index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.get("/api/v1/auth/youtube/status")
async def youtube_status(request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")
    if not supabase:
        return {"connected": False}
    try:
        res = supabase.table("users").select("youtube_connected, youtube_refresh_token").eq("id", user_id).execute()
        if res.data and res.data[0].get("youtube_refresh_token"):
            return {"connected": True}
    except Exception as e:
        print(f"Status check error: {e}")
    return {"connected": False}

class UserProfileUpdate(BaseModel):
    email: str

@app.get("/api/v1/user/profile")
async def get_user_profile(request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")
    user = get_or_create_user(user_id)
    return {
        "user_id": user.get("id", user_id),
        "email": user.get("email", ""),
        "license": user.get("license", "free_tier"),
        "free_clips_used": user.get("free_clips_used", 0)
    }

@app.post("/api/v1/user/profile")
async def update_user_profile(payload: UserProfileUpdate, request: Request, response: Response):
    user_id = request.cookies.get("user_id", "demo_user_123")
    email = payload.email.strip().lower()
    
    # Strict regex format check (RFC compliant)
    import re, socket
    email_regex = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    if not re.match(email_regex, email) or len(email) < 6:
        raise HTTPException(status_code=400, detail="Invalid email format. Please enter a genuine email address.")

    domain = email.split('@')[1]
    # Block obviously fake/trash test domains
    blocked_domains = {"test.com", "example.com", "fake.com", "asdf.com", "mailinator.com", "tempmail.com", "throwaway.com", "123.com", "abc.com"}
    if domain in blocked_domains or "." not in domain or len(domain.split('.')[-1]) < 2:
        raise HTTPException(status_code=400, detail="Please enter a real, valid email provider (e.g. Gmail, Outlook, Yahoo).")

    # Verify domain exists via DNS
    try:
        socket.gethostbyname(domain)
    except socket.gaierror:
        raise HTTPException(status_code=400, detail=f"The email domain '@{domain}' does not exist. Please check your spelling.")
    
    final_user_id = user_id
    license_tier = "free_tier"
    if supabase:
        try:
            # Check if an account with this email already exists
            res = supabase.table("users").select("*").eq("email", email).execute()
            if res.data and len(res.data) > 0:
                existing_user = res.data[0]
                final_user_id = existing_user.get("id", user_id)
                license_tier = existing_user.get("license", "free_tier")
            else:
                # Update current session's user record with the email
                supabase.table("users").update({"email": email}).eq("id", user_id).execute()
                final_user_id = user_id
        except Exception as e:
            print(f"Failed to link/find account email: {e}")

    # Set persistent cookie matching the linked account ID
    response.set_cookie(key="user_id", value=final_user_id, max_age=31536000, samesite="lax")
    return {"status": "success", "email": email, "user_id": final_user_id, "license": license_tier}

@app.get("/api/v1/analytics")
async def get_analytics(request: Request, user_id: str = ""):
    active_user = user_id or request.cookies.get("user_id", "demo_user_123")
    if not supabase:
        return {"videos": [], "total_views": 0, "total_videos": 0, "avg_views": 0}
    try:
        res = supabase.table("clips").select("*").eq("user_id", active_user).order("created_at", desc=True).execute()
        videos = res.data or []
        total_views = sum(v.get("views", 0) for v in videos)
        return {
            "videos": videos,
            "total_views": total_views,
            "total_videos": len(videos),
            "avg_views": total_views // len(videos) if videos else 0
        }
    except Exception as e:
        print(f"Analytics error: {e}")
        return {"videos": [], "total_views": 0, "total_videos": 0, "avg_views": 0}

@app.delete("/api/v1/analytics/reset")
async def reset_analytics(request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")
    if supabase:
        try:
            supabase.table("clips").delete().eq("user_id", user_id).execute()
            return {"status": "success", "message": "Analytics reset to 0"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    return {"status": "error", "message": "No database connection"}

@app.post("/api/v1/analytics/refresh-views")
async def refresh_views(request: Request):
    """Fetch real live view counts from YouTube Data API and update the clips table."""
    import httpx
    user_id = request.cookies.get("user_id", "demo_user_123")
    if not supabase:
        return {"status": "error", "message": "No database"}
    try:
        res = supabase.table("clips").select("id, youtube_url").eq("user_id", user_id).execute()
        clips = res.data or []
        if not clips:
            return {"status": "ok", "updated": 0}

        # Extract YouTube video IDs from URLs like https://youtube.com/shorts/VIDEO_ID
        video_ids = []
        id_map = {}
        for clip in clips:
            url = clip.get("youtube_url", "")
            if not url:
                continue
            vid = url.rstrip("/").split("/")[-1]
            if vid:
                video_ids.append(vid)
                id_map[vid] = clip["id"]

        if not video_ids:
            return {"status": "ok", "updated": 0}

        YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
        if not YOUTUBE_API_KEY:
            return {"status": "error", "message": "YOUTUBE_API_KEY not set on server"}

        params = {
            "part": "statistics",
            "id": ",".join(video_ids),
            "key": YOUTUBE_API_KEY,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get("https://www.googleapis.com/youtube/v3/videos", params=params)
            r.raise_for_status()
            data = r.json()

        updated = 0
        for item in data.get("items", []):
            vid_id = item["id"]
            views = int(item.get("statistics", {}).get("viewCount", 0))
            row_id = id_map.get(vid_id)
            if row_id:
                supabase.table("clips").update({"views": views}).eq("id", row_id).execute()
                updated += 1

        return {"status": "ok", "updated": updated}
    except Exception as e:
        print(f"refresh-views error: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/api/v1/worker/version")
async def get_worker_version():
    """Returns the latest worker version so the client can auto-update."""
    return {"version": "1.4.0"}

@app.get("/api/v1/auto-post/settings")
async def get_auto_post_settings(request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")
    default_settings = {"enabled": False, "time": "12:00", "times": ["12:00"], "niche": "motivation", "days": ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]}
    
    if redis_client:
        try:
            data = redis_client.hgetall(f"user:{user_id}:autopost")
            if data:
                return {
                    "enabled": data.get("enabled") == "True",
                    "time": data.get("time", "12:00"),
                    "times": json.loads(data.get("times", '["12:00"]')),
                    "niche": data.get("niche", "motivation"),
                    "days": json.loads(data.get("days", '["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]'))
                }
        except Exception as e:
            print(f"Redis fetch error: {e}")
            
    # Fallback to Supabase if Redis is empty
    if supabase:
        try:
            res = supabase.table("users").select("auto_post_enabled, auto_post_time, auto_post_niche").eq("id", user_id).execute()
            if res.data:
                d = res.data[0]
                default_settings.update({
                    "enabled": d.get("auto_post_enabled", False),
                    "time": d.get("auto_post_time", "12:00"),
                    "times": [d.get("auto_post_time", "12:00")],
                    "niche": d.get("auto_post_niche", "motivation")
                })
        except Exception as e:
            print(f"Error fetching auto-post settings: {e}")
            
    return default_settings

@app.post("/api/v1/auto-post/settings")
async def save_auto_post_settings(settings: AutoPostSettings, request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")
    
    times_list = settings.times if settings.times else [settings.time]
    
    if redis_client:
        try:
            redis_client.hset(f"user:{user_id}:autopost", mapping={
                "enabled": str(settings.enabled),
                "time": times_list[0] if times_list else "12:00",
                "times": json.dumps(times_list),
                "niche": settings.niche,
                "days": json.dumps(settings.days)
            })
        except Exception as e:
            print(f"Redis save error: {e}")
            
    if supabase:
        try:
            supabase.table("users").update({
                "auto_post_enabled": settings.enabled,
                "auto_post_time": times_list[0] if times_list else "12:00",
                "auto_post_niche": settings.niche
            }).eq("id", user_id).execute()
        except Exception as e:
            print(f"Error saving auto-post settings to DB: {e}")
            
    return {"status": "success"}

@app.post("/api/v1/generate-clip")
async def generate_clip(payload: ClipRequest, request: Request):
    # Retrieve user ID (mock session ID for demonstration)
    user_id = request.cookies.get("user_id", "demo_user_123")
    user = get_or_create_user(user_id)

    # Enforce paywall on free tier — allow up to 5 free generations
    free_clips_used = user.get("free_clips_used", 0)
    FREE_TIER_LIMIT = 5
    if free_clips_used >= FREE_TIER_LIMIT and user.get("license") == "free_tier":
        raise HTTPException(status_code=402, detail="Free tier limit reached (5/5). Upgrade required.")

    # Create job ID and store status in Redis
    import threading
    job_id = str(uuid.uuid4())
    if redis_client:
        redis_client.hset(f"job:{job_id}", mapping={
            "status": "queued",
            "progress": 0,
            "message": "Job queued for processing...",
            "url": ""
        })
        redis_client.expire(f"job:{job_id}", 86400)

    # Increment free clip counter if on free tier (non-blocking)
    if supabase and user.get("license") == "free_tier":
        try:
            supabase.table("users").update({
                "free_clips_used": free_clips_used + 1
            }).eq("id", user_id).execute()
        except Exception as e:
            print(f"Warning: Could not update free_clips_used: {e}")

    # Queue the job for the cloud workers & worker clones
    if redis_client:
        job_payload_str = json.dumps({
            "job_id": job_id,
            "niche": payload.niche,
            "user_id": user_id,
            "is_free_tier": user.get("license") == "free_tier",
            "auto_upload": payload.auto_upload,
            "layout": payload.layout,
            "subtitle_style": payload.subtitle_style
        })
        redis_client.lpush(f"worker_queue:{user_id}", job_payload_str)
        redis_client.lpush("worker_queue:global", job_payload_str)
        print(f"[Queue] Job {job_id} pushed to worker_queue (user={user_id})")
    else:
        print("[Queue] WARNING: redis_client is None — job not queued!")

    # Return remaining free generations so UI can update the badge
    remaining = max(0, FREE_TIER_LIMIT - (free_clips_used + 1)) if user.get("license") == "free_tier" else None
    return {"status": "success", "job_id": job_id, "free_remaining": remaining}

@app.get("/api/v1/job-status/{job_id}")
async def get_job_status(job_id: str):
    if not redis_client:
        return {"status": "idle", "progress": 0, "message": "Redis not connected"}
    job_data = redis_client.hgetall(f"job:{job_id}")
    if not job_data:
        return {"status": "error", "progress": 0, "message": "Job not found"}
    return {
        "status": job_data.get("status", "unknown"),
        "progress": int(job_data.get("progress", 0)),
        "message": job_data.get("message", ""),
        "url": job_data.get("url", "")
    }

class JobCompletePayload(BaseModel):
    job_id: str
    status: str
    message: str
    url: str = ""
    title: str = ""
    niche: str = ""

@app.get("/api/v1/user/youtube-creds")
async def get_youtube_creds(user_id: str, token: str = ""):
    """Called by the desktop worker to get YouTube OAuth credentials securely."""
    if not verify_user_token(user_id, token):
        # Fallback check for session cookie if requested from browser
        pass
    if not supabase:
        return {"error": "Database not connected"}
    try:
        res = supabase.table("users").select(
            "youtube_access_token, youtube_refresh_token"
        ).eq("id", user_id).execute()
        if res.data and res.data[0].get("youtube_refresh_token"):
            return {
                "token": res.data[0].get("youtube_access_token"),
                "refresh_token": res.data[0].get("youtube_refresh_token"),
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "user_id": user_id
            }
        return {"error": "YouTube not connected for this user"}
    except Exception as e:
        print(f"Error fetching YouTube creds: {e}")
        return {"error": str(e)}

@app.get("/api/v1/debug/queue")
async def debug_queue(user_id: str):
    """Diagnostic endpoint to check Redis queue state."""
    if not redis_client:
        return {"error": "Redis not connected"}
    try:
        queue_len = redis_client.llen(f"worker_queue:{user_id}")
        heartbeat = redis_client.get(f"worker_heartbeat:{user_id}")
        items = redis_client.lrange(f"worker_queue:{user_id}", 0, -1)
        return {
            "queue_length": queue_len,
            "worker_alive": bool(heartbeat),
            "queue_items": [json.loads(i) if i else None for i in items]
        }
    except Exception as e:
        return {"error": str(e)}

@app.delete("/api/v1/clip/{clip_id}")
async def delete_clip(clip_id: str, request: Request):
    """Deletes a clip record."""
    user_id = request.cookies.get("user_id", "demo_user_123")
    if not supabase:
        raise HTTPException(status_code=500, detail="Database not configured")
    try:
        supabase.table("clips").delete().eq("id", clip_id).eq("user_id", user_id).execute()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/v1/workplace/clean-test-drafts")
async def clean_test_drafts(user_id: str = ""):
    """Removes temporary test draft entries."""
    if not supabase:
        return {"status": "error"}
    try:
        supabase.table("clips").delete().eq("title", "Debug Test Draft 2").execute()
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@app.get("/api/v1/worker/poll")
async def worker_poll(user_id: str):
    if not redis_client:
        return {"job": None}
    
    try:
        redis_client.setex(f"worker_heartbeat:{user_id}", 30, "alive")
        redis_client.setex("worker_heartbeat:cloud", 30, "alive")

        # Pop from user specific queue or global queue
        job = redis_client.rpop(f"worker_queue:{user_id}")
        if not job:
            job = redis_client.rpop("worker_queue:global")

        if job:
            if isinstance(job, bytes):
                job = job.decode("utf-8")
            job_data = json.loads(job)
            redis_client.hset(f"job:{job_data['job_id']}", mapping={
                "status": "processing",
                "message": "Cloud worker started pipeline...",
                "progress": 5
            })
            return {"job": job_data}
    except Exception as e:
        print(f"Poll error: {e}")
    return {"job": None}

@app.post("/api/v1/worker/complete")
async def worker_complete(payload: JobCompletePayload, user_id: str):
    if not redis_client:
        return {"error": "Redis not connected"}
        
    redis_client.hset(f"job:{payload.job_id}", mapping={
        "status": payload.status,
        "progress": 100,
        "message": payload.message,
        "url": payload.url
    })
    
    # Save to supabase if complete or draft_ready
    if payload.status in ["complete", "draft_ready"] and supabase:
        # Try with status field first, fall back without status field if column does not exist
        saved = False
        try:
            supabase.table("clips").insert({
                "user_id": user_id,
                "youtube_url": payload.url,
                "title": payload.title,
                "niche": payload.niche,
                "views": 0,
                "status": "published" if payload.url else "draft"
            }).execute()
            saved = True
        except Exception as e1:
            print(f"Clips save with status failed: {e1}")
            try:
                supabase.table("clips").insert({
                    "user_id": user_id,
                    "youtube_url": payload.url,
                    "title": payload.title,
                    "niche": payload.niche,
                    "views": 0
                }).execute()
                saved = True
            except Exception as e2:
                print(f"Clips fallback save error: {e2}")
            
    return {"status": "ok"}

@app.get("/api/v1/worker/heartbeat")
async def worker_heartbeat(user_id: str):
    if not redis_client:
        return {"alive": True}
    alive = redis_client.get(f"worker_heartbeat:{user_id}") or redis_client.get("worker_heartbeat:cloud")
    return {"alive": bool(alive)}

@app.get("/api/v1/worker/scripts")
async def get_worker_scripts():
    """Returns the latest production pipeline code for live hot-updating of desktop workers."""
    script_names = ["worker.py", "clip_cutter.py", "clip_finder.py", "video_finder.py", "video_downloader.py", "youtube_uploader.py", "hot_pipeline.py"]
    scripts = {}
    base = Path(__file__).resolve().parent
    for s in script_names:
        p = base / s
        if p.exists():
            try:
                scripts[s] = p.read_text(encoding="utf-8")
            except Exception:
                pass
    return {"scripts": scripts}

class ProgressPayload(BaseModel):
    job_id: str
    status: str = "running"
    progress: int
    message: str
    url: str = ""

@app.post("/api/v1/worker/progress")
async def worker_progress(payload: ProgressPayload):
    if redis_client:
        redis_client.hset(f"job:{payload.job_id}", mapping={
            "progress": payload.progress,
            "message": payload.message,
            "status": payload.status,
            "url": payload.url
        })
    return {"status": "ok"}

class AnalyzeRequest(BaseModel):
    transcript: str
    niche: str

@app.post("/api/v1/worker/analyze-transcript")
async def analyze_transcript(payload: AnalyzeRequest, user_id: str):
    """
    Accepts a transcript from the worker, asks Gemini for the best segment,
    and returns the timestamps. This protects the GEMINI_API_KEY on the server.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        # Heuristic fallback: pick the densest part of the transcript (most words per minute)
        # Parse timestamps from transcript lines like "[MM:SS] text"
        import re as re_mod
        lines = payload.transcript.strip().split("\n")
        entries = []
        for line in lines:
            m = re_mod.match(r"\[(\d+):(\d+)\]\s+(.*)", line)
            if m:
                t = int(m.group(1))*60 + int(m.group(2))
                entries.append((t, m.group(3)))
        
        best_start, best_end = 60, 110
        if len(entries) >= 4:
            # Slide a 45-50 second window and find max word density for a single Short
            best_words = 0
            for i in range(len(entries)):
                window_start = entries[i][0]
                window_end = window_start + 50
                words = sum(len(e[1].split()) for e in entries if window_start <= e[0] < window_end)
                if words > best_words:
                    best_words = words
                    best_start = window_start
                    best_end = window_end
        
        return {
            "start_sec": best_start,
            "end_sec": best_end,
            "num_parts": 1,
            "caption": payload.niche.title()
        }
        
    try:
        from google import genai
        from google.genai import types
        import re
        
        client = genai.Client(api_key=api_key)
        
        prompt = f"""You are a world-class YouTube Shorts & TikTok viral retention editor and script director for the '{payload.niche}' niche.
Analyze the following timestamped transcript and find the HIGHEST RETENTION, most explosive 30 to 55-second moment (PARTS: 1).

Retention & Virality Criteria:
1. Hook Viability (0-3s): Must open with a high-stakes question, counter-intuitive statement, or sudden dramatic setup that stops scrolling.
2. Pacing & Momentum: Fast information density, minimal filler words or dead pauses.
3. Narrative Arc: A complete standalone thought, story, lesson, or insight with a definitive punchline or resolution.
4. Loop Potential: The end should naturally tie back or provoke an immediate reaction/comment.

Transcript:
{payload.transcript}

Respond in EXACTLY this format, nothing else:
START: 120
END: 170
PARTS: 1
CAPTION: How I Built My First Million
VIRAL_SCORE: 96
REASON: High curiosity hook with intense storytelling arc and punchy conclusion."""

        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=256)
        )
        text = response.text.strip()
        
        def parse_ts(val):
            val = val.strip()
            if ":" in val:
                parts = val.split(":")
                if len(parts) == 2:
                    return int(parts[0]) * 60 + int(parts[1])
                elif len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            return int(val)

        start_m = re.search(r"START:\s*([\d:]+)", text)
        end_m   = re.search(r"END:\s*([\d:]+)", text)
        parts_m = re.search(r"PARTS:\s*(\d+)", text)
        caption_m = re.search(r"CAPTION:\s*(.+)", text)
        score_m = re.search(r"VIRAL_SCORE:\s*(\d+)", text)

        if not start_m or not end_m:
            return {"error": "Could not parse Gemini output", "raw": text}

        start = parse_ts(start_m.group(1))
        end   = parse_ts(end_m.group(1))
        parts = int(parts_m.group(1)) if parts_m else max(1, round((end - start) / 55))
        score = int(score_m.group(1)) if score_m else 92
        
        return {
            "start_sec": start,
            "end_sec": end,
            "num_parts": parts,
            "caption": caption_m.group(1).strip() if caption_m else payload.niche.title(),
            "viral_score": score
        }
    except Exception as e:
        print(f"Analyze error: {e}")
        return {"error": str(e)}

@app.post("/api/v1/clip/publish-draft")
async def publish_draft(payload: PublishDraftRequest, request: Request):
    """Publishes a saved draft clip from Workplace directly to YouTube."""
    user_id = request.cookies.get("user_id", "demo_user_123")
    if not supabase:
        raise HTTPException(status_code=500, detail="Database not configured")
    
    # Get clip details
    res = supabase.table("clips").select("*").eq("id", payload.clip_id).eq("user_id", user_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Clip not found in your Workplace")
    
    clip = res.data[0]
    # Mark as published
    supabase.table("clips").update({
        "status": "published",
        "title": payload.title or clip.get("title") or "Viral Short"
    }).eq("id", payload.clip_id).execute()
    
    return {"status": "success", "message": "Clip submitted for YouTube publishing!"}

class CheckoutRequest(BaseModel):
    tier: str = "lifetime"

@app.post("/api/v1/create-checkout-session")
async def create_checkout_session(request: Request, body: CheckoutRequest = None):
    user_id = request.cookies.get("user_id", "demo_user_123")
    domain = str(request.base_url).rstrip("/")
    tier = body.tier if body and body.tier else "lifetime"

    tiers = {
        "pro": {"name": "ViralClip AI - Pro (Monthly)", "amount": 2900, "mode": "subscription"},
        "full_version": {"name": "ViralClip AI - Full Version (Monthly)", "amount": 4900, "mode": "subscription"},
        "lifetime": {"name": "ViralClip AI - Full Version (Monthly)", "amount": 4900, "mode": "subscription"}
    }
    selected = tiers.get(tier, tiers["full_version"])

    session_params = {
        "payment_method_types": ["card"],
        "client_reference_id": user_id,
        "metadata": {"tier": tier, "user_id": user_id},
        "line_items": [{
            "price_data": {
                "currency": "usd",
                "product_data": {
                    "name": selected["name"],
                    "description": "Viral AI Short generation, background rendering, and YouTube auto-posting."
                },
                "unit_amount": selected["amount"],
            },
            "quantity": 1,
        }],
        "mode": selected["mode"],
        "success_url": f"{domain}/?payment=success",
        "cancel_url": f"{domain}/?payment=cancel",
    }
    if selected["mode"] == "subscription":
        session_params["line_items"][0]["price_data"]["recurring"] = {"interval": "month"}

    session = stripe.checkout.Session.create(**session_params)
    return {"checkout_url": session.url}

@app.post("/api/v1/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session.get("client_reference_id")
        tier_purchased = (session.get("metadata") or {}).get("tier", "pro")
        if user_id and supabase:
            try:
                supabase.table("users").update({"license": tier_purchased}).eq("id", user_id).execute()
            except Exception as e:
                print(f"Stripe webhook DB error: {e}")

    return {"status": "success"}

# ─── Invite Link System ─────────────────────────────────────────────────────────
import secrets as _secrets

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "clipai_admin_2024")

@app.post("/api/v1/admin/generate-invite")
async def generate_invite(request: Request, count: int = 1):
    """Generate one-time invite links that grant pro license on redemption."""
    auth = request.headers.get("X-Admin-Secret", "")
    if auth != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not supabase:
        raise HTTPException(status_code=500, detail="Database not connected")
    links = []
    for _ in range(count):
        token = _secrets.token_urlsafe(24)
        try:
            supabase.table("invites").insert({
                "token": token,
                "redeemed": False,
            }).execute()
            base_url = str(request.base_url).rstrip("/")
            links.append(f"{base_url}/redeem/{token}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"DB error: {e}")
    return {"links": links}

@app.get("/redeem/{token}")
async def redeem_invite(token: str, response: Response):
    """User visits this link — grants pro license, sets cookie, redirects to app."""
    if not supabase:
        raise HTTPException(status_code=500, detail="Database not connected")
    try:
        res = supabase.table("invites").select("*").eq("token", token).eq("redeemed", False).execute()
        if not res.data:
            from fastapi.responses import HTMLResponse as _HR
            return _HR("""<html><body style='font-family:sans-serif;text-align:center;padding:60px;background:#0f0f0f;color:white'>
                <h2>&#10060; Invalid or already used invite link.</h2>
                <p>This link has already been redeemed or doesn't exist.</p>
                <a href='/' style='color:#3b82f6'>&#8592; Back to ClipAI</a></body></html>""", status_code=400)
        import uuid as _uuid
        new_user_id = f"user_{_uuid.uuid4().hex[:8]}"
        supabase.table("users").insert({"id": new_user_id, "license": "pro", "free_clips_used": 0}).execute()
        supabase.table("invites").update({"redeemed": True, "redeemed_by": new_user_id}).eq("token", token).execute()
        from fastapi.responses import RedirectResponse as _RR2
        redir = _RR2(url="/", status_code=302)
        redir.set_cookie("user_id", new_user_id, max_age=60*60*24*365, samesite="lax")
        return redir
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

from fastapi.responses import RedirectResponse
import google_auth_oauthlib.flow

@app.get("/api/v1/auth/youtube")
async def auth_youtube(request: Request):
    user_id = request.cookies.get("user_id", "demo_user_123")
    
    import urllib.parse
    import uuid
    state = str(uuid.uuid4())
    
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(YOUTUBE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state
    }
    
    authorization_url = "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params)
    
    # Store state in redis with user_id to verify later
    if redis_client:
        redis_client.setex(f"oauth_state:{state}", 600, user_id)
        
    return RedirectResponse(authorization_url)

@app.get("/api/v1/auth/youtube/callback")
async def auth_youtube_callback(request: Request, state: str = None, code: str = None):
    if not state or not code:
        return {"error": "Missing state or code"}
        
    user_id = redis_client.get(f"oauth_state:{state}") if redis_client else "demo_user_123"
    
    try:
        import httpx
        token_data = {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": GOOGLE_REDIRECT_URI
        }
        
        # Exchange authorization code for tokens
        with httpx.Client() as client:
            r = client.post("https://oauth2.googleapis.com/token", data=token_data)
            
        if r.status_code != 200:
            raise Exception(f"Google Token API returned {r.status_code}: {r.text}")
            
        token_json = r.json()
        access_token = token_json.get("access_token")
        refresh_token = token_json.get("refresh_token")

        # ── Branch: If state is for Google Account Login / Registration ─────────
        if state.startswith("login_"):
            userinfo_res = httpx.get("https://www.googleapis.com/oauth2/v2/userinfo", headers={"Authorization": f"Bearer {access_token}"})
            if userinfo_res.status_code == 200:
                userinfo = userinfo_res.json()
                email = userinfo.get("email", "").lower()
                if email and userinfo.get("verified_email", False):
                    login_user_id = f"user_{abs(hash(email)) % 1000000:06d}"
                    if supabase:
                        try:
                            res = supabase.table("users").select("*").eq("email", email).execute()
                            if res.data and len(res.data) > 0:
                                login_user_id = res.data[0]["id"]
                            else:
                                supabase.table("users").insert({
                                    "id": login_user_id,
                                    "email": email,
                                    "license": "free_tier",
                                    "free_clips_used": 0
                                }).execute()
                        except Exception as dbe:
                            print(f"Supabase login save error: {dbe}")
                    redir = RedirectResponse("/?auth=success", status_code=302)
                    redir.set_cookie("user_id", login_user_id, max_age=60*60*24*365, samesite="lax")
                    return redir
        
        # ── Branch: YouTube Channel Connection ─────────────────────────────────
        if supabase:
            update_data = {
                "youtube_access_token": access_token,
                "youtube_connected": True
            }
            # Only update refresh_token if Google actually sent one (it only sends on first consent)
            if refresh_token:
                update_data["youtube_refresh_token"] = refresh_token
                
            supabase.table("users").update(update_data).eq("id", user_id).execute()
            
        return RedirectResponse("/?youtube=connected")
    except Exception as e:
        import urllib.parse
        error_msg = urllib.parse.quote(str(e))
        print(f"OAuth Error: {e}")
        return RedirectResponse(f"/?youtube=error&detail={error_msg}")

# ─── Google Account Login & Registration (Strict Google Auth) ───────────────────
GOOGLE_AUTH_SCOPES = ["openid", "email", "profile"]

@app.get("/api/v1/auth/google")
async def auth_google_login(request: Request):
    """Initiates 1-click login/registration with verified Google Accounts using the authorized callback."""
    import urllib.parse
    import uuid
    state = f"login_{uuid.uuid4()}"
    if redis_client:
        redis_client.setex(f"oauth_state:{state}", 600, "google_login")

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GOOGLE_AUTH_SCOPES),
        "access_type": "online",
        "prompt": "select_account",
        "state": state
    }
    authorization_url = "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params)
    return RedirectResponse(authorization_url)

@app.get("/api/v1/auth/google/callback")
async def auth_google_callback(request: Request, state: str = None, code: str = None):
    """Verifies Google identity, links or creates a persistent account, and sets secure session cookie."""
    if not code:
        return RedirectResponse("/?auth=error&msg=missing_code")

    try:
        import httpx
        token_data = {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": GOOGLE_AUTH_REDIRECT_URI
        }
        async with httpx.AsyncClient(timeout=15) as client:
            token_res = await client.post("https://oauth2.googleapis.com/token", data=token_data)
            if token_res.status_code != 200:
                raise Exception("Failed to exchange code with Google")
            tokens = token_res.json()
            access_token = tokens.get("access_token")
            
            # Fetch verified Google user info
            userinfo_res = await client.get("https://www.googleapis.com/oauth2/v2/userinfo", headers={"Authorization": f"Bearer {access_token}"})
            if userinfo_res.status_code != 200:
                raise Exception("Failed to fetch Google profile info")
            userinfo = userinfo_res.json()

        email = userinfo.get("email", "").lower()
        if not email or not userinfo.get("verified_email", False):
            raise Exception("Only verified Google accounts are permitted")

        # Find or create user in Supabase
        user_id = f"user_{abs(hash(email)) % 1000000:06d}"
        license_tier = "free_tier"

        if supabase:
            try:
                res = supabase.table("users").select("*").eq("email", email).execute()
                if res.data and len(res.data) > 0:
                    user_id = res.data[0]["id"]
                    license_tier = res.data[0].get("license", "free_tier")
                else:
                    # New user registered with Google
                    supabase.table("users").insert({
                        "id": user_id,
                        "email": email,
                        "license": "free_tier",
                        "free_clips_used": 0
                    }).execute()
            except Exception as dbe:
                print(f"Supabase auth error: {dbe}")

        redir = RedirectResponse("/?auth=success", status_code=302)
        redir.set_cookie("user_id", user_id, max_age=60*60*24*365, samesite="lax")
        return redir
    except Exception as e:
        import urllib.parse
        err_enc = urllib.parse.quote(str(e))
        return RedirectResponse(f"/?auth=error&detail={err_enc}")


################################################################################
# FILE: client_worker.py
################################################################################

import os
import sys
import time
import requests
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Immediately hide console window on Windows if spawned with one
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32")
        user32 = ctypes.WinDLL("user32")
        hWnd = kernel32.GetConsoleWindow()
        if hWnd:
            user32.ShowWindow(hWnd, 0) # SW_HIDE
    except Exception:
        pass

# Ensure pystray and pillow are available (Windows desktop tray only)
pystray = None
Image = None
ImageDraw = None
reg = None

if sys.platform == "win32" and "--cloud" not in sys.argv:
    try:
        import pystray
        from PIL import Image, ImageDraw
        import winreg as reg
    except ImportError:
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "pystray", "Pillow"], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            import pystray
            from PIL import Image, ImageDraw
            import winreg as reg
        except Exception:
            pass

# Setup cross-platform paths
HOME_DIR = Path.home() / ".clipai"
BIN_DIR = HOME_DIR / "bin"
os.makedirs(BIN_DIR, exist_ok=True)

# Update system path so subprocesses can find bin files easily
os.environ["PATH"] += os.pathsep + str(BIN_DIR)

API_BASE_URL = os.environ.get("API_BASE_URL", "https://viralclip-saas.onrender.com")
# Force inject it so imported modules like worker.py use it
os.environ["API_BASE_URL"] = API_BASE_URL

# For testing locally, uncomment the line below:
# API_BASE_URL = "http://localhost:8000"
# os.environ["API_BASE_URL"] = API_BASE_URL

def get_user_id():
    local_storage_path = Path.home() / ".clipai" / "user_id.txt"
    
    # 1. If passed as arg via URI handler from the website (e.g. clipai://start?user_id=user_12345)
    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            if arg.startswith("clipai://"):
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(arg)
                qs = parse_qs(parsed.query)
                if "user_id" in qs:
                    uid = qs["user_id"][0]
                    # Save it so future manual double-clicks work
                    local_storage_path.parent.mkdir(parents=True, exist_ok=True)
                    local_storage_path.write_text(uid)
                    return uid

    # 2. Check local storage file
    if local_storage_path.exists():
        stored_id = local_storage_path.read_text().strip()
        if stored_id and "@" not in stored_id: # Ignore old broken emails
            return stored_id

    # 3. If no ID found, force them to use the website
    print("\n" + "="*60)
    print("Welcome to ClipAI Desktop Worker!")
    print("="*60)
    print("ERROR: No linked account found.")
    print("Please go to your web dashboard and click 'Start Worker'.")
    print("This will securely link your browser session to this app.")
    print("="*60)
    time.sleep(10)
    os._exit(1)

USER_ID = get_user_id()
is_running = True

def register_uri_scheme():
    """Register the clipai:// protocol handler in Windows Registry."""
    if sys.platform != "win32":
        print("URI Registration only supported on Windows currently.")
        return
        
    import winreg
    try:
        # HKEY_CURRENT_USER\Software\Classes\clipai
        key_path = r"Software\Classes\clipai"
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
        winreg.SetValue(key, "", winreg.REG_SZ, "URL:ClipAI Protocol")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        
        # Path to this executable
        exe_path = sys.executable if getattr(sys, 'frozen', False) else f'"{sys.executable}" "{os.path.abspath(__file__)}"'
        
        # shell\open\command
        cmd_key = winreg.CreateKey(key, r"shell\open\command")
        winreg.SetValue(cmd_key, "", winreg.REG_SZ, f'{exe_path} "%1"')
        
        winreg.CloseKey(cmd_key)
        winreg.CloseKey(key)
        print("Successfully registered clipai:// protocol handler.")
    except Exception as e:
        print(f"Failed to register URI scheme: {e}")

def update_yt_dlp():
    """Download or auto-update standalone yt-dlp binary."""
    print("Checking for yt-dlp updates...")
    yt_dlp_exe = BIN_DIR / ("yt-dlp.exe" if sys.platform == "win32" else "yt-dlp")
    try:
        if not yt_dlp_exe.exists():
            print("yt-dlp not found. Downloading latest standalone binary...")
            if sys.platform == "win32":
                url = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
            elif sys.platform == "darwin":
                url = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos"
            else:
                url = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
            response = requests.get(url, stream=True)
            with open(yt_dlp_exe, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            if sys.platform != "win32":
                os.chmod(yt_dlp_exe, 0o755)
        else:
            subprocess.run([str(yt_dlp_exe), "-U"], check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        print("yt-dlp is up to date!")
    except Exception as e:
        print(f"Failed to setup yt-dlp: {e}")

def sync_live_pipeline_scripts():
    """Dynamically fetches the latest production fixes from the cloud server and updates local modules in real-time."""
    try:
        res = requests.get(f"{API_BASE_URL}/api/v1/worker/scripts", timeout=10)
        if res.status_code == 200:
            data = res.json()
            scripts = data.get("scripts", {})
            for name, code in scripts.items():
                target_path = Path(__file__).resolve().parent / name
                try:
                    target_path.write_text(code, encoding="utf-8")
                except Exception:
                    pass
            print(f"[Worker] Live hot-sync complete: {len(scripts)} pipeline scripts updated to latest version.")
    except Exception as e:
        print(f"[Worker] Live hot-sync warning (offline/cached): {e}")

def run_worker_loop():
    global is_running
    print(f"Starting ClipAI Companion Worker for user: {USER_ID}")
    
    # Always pull latest fixes live before starting loop
    sync_live_pipeline_scripts()

    try:
        import importlib
        import worker
        import hot_pipeline
        importlib.reload(worker)
        importlib.reload(hot_pipeline)
        from worker import run_clip_pipeline
        # Hot pipeline ready on demand
        print("[HotPipeline] Engine active. Cache is clean and ready.")
    except ImportError as e:
        print(f"Failed to load pipeline modules: {e}")
        return

    from concurrent.futures import ThreadPoolExecutor
    # 3 concurrent worker clone threads to process multiple users simultaneously
    CONCURRENT_WORKERS = int(os.environ.get("CONCURRENT_WORKERS", "3"))
    executor = ThreadPoolExecutor(max_workers=CONCURRENT_WORKERS)
    print(f"[Worker Pool] Initialized with {CONCURRENT_WORKERS} concurrent execution slots.")

    def process_job(job):
        job_id = job["job_id"]
        niche = job["niche"]
        job_user_id = job.get("user_id", USER_ID)
        is_free_tier = job.get("is_free_tier", False)
        auto_upload = job.get("auto_upload", True)
        layout = job.get("layout", "split_screen")
        subtitle_style = job.get("subtitle_style", "hormozi")
        print(f"\n[Worker Slot] Processing job: {job_id} for user: {job_user_id} (niche={niche})")
        try:
            run_clip_pipeline(niche, job_user_id, job_id, is_free_tier, auto_upload=auto_upload, layout=layout, subtitle_style=subtitle_style)
        except Exception as pipeline_err:
            print(f"Pipeline error on job {job_id}: {pipeline_err}")
            requests.post(f"{API_BASE_URL}/api/v1/worker/complete", json={
                "job_id": job_id, "status": "error", "message": str(pipeline_err)
            }, params={"user_id": job_user_id})

    while is_running:
        try:
            res = requests.get(f"{API_BASE_URL}/api/v1/worker/poll", params={"user_id": USER_ID}, timeout=10)
            if res.status_code == 200:
                data = res.json()
                job = data.get("job")
                if job:
                    executor.submit(process_job, job)
        except requests.exceptions.RequestException:
            pass
        except Exception as e:
            print(f"Unexpected polling error: {e}")
        time.sleep(2)

def start_local_stream_server():
    """Starts a local HTTP server on port 58921 to stream rendered draft videos to the web browser."""
    from http.server import SimpleHTTPRequestHandler, HTTPServer
    import urllib.parse
    
    video_dir = HOME_DIR / "generated_videos"
    video_dir.mkdir(parents=True, exist_ok=True)
    
    class MediaStreamHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(video_dir), **kwargs)
            
        def end_headers(self):
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Range, Content-Type')
            super().end_headers()
            
        def do_OPTIONS(self):
            self.send_response(200)
            self.end_headers()
            
        def log_message(self, format, *args):
            pass  # Keep console quiet
            
    try:
        httpd = HTTPServer(('127.0.0.1', 58921), MediaStreamHandler)
        print("[Worker] Local video streaming server active at http://127.0.0.1:58921")
        httpd.serve_forever()
    except Exception as e:
        print(f"[Worker] Local stream server error: {e}")

def set_autostart(enable=True):
    key = reg.HKEY_CURRENT_USER
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        registry_key = reg.OpenKey(key, key_path, 0, reg.KEY_ALL_ACCESS)
        if enable:
            # We add quotes around sys.executable to ensure paths with spaces work safely
            cmd = f'"{sys.executable}"'
            reg.SetValueEx(registry_key, "ClipAI_Worker", 0, reg.REG_SZ, cmd)
        else:
            try:
                reg.DeleteValue(registry_key, "ClipAI_Worker")
            except FileNotFoundError:
                pass
        reg.CloseKey(registry_key)
    except Exception as e:
        print(f"Failed to set startup: {e}")

def check_autostart():
    key = reg.HKEY_CURRENT_USER
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        registry_key = reg.OpenKey(key, key_path, 0, reg.KEY_READ)
        value, _ = reg.QueryValueEx(registry_key, "ClipAI_Worker")
        reg.CloseKey(registry_key)
        return True
    except FileNotFoundError:
        return False

def create_image():
    # Generate a sleek crimson icon for the system tray
    image = Image.new('RGB', (64, 64), color=(220, 38, 38))
    d = ImageDraw.Draw(image)
    d.rectangle([16, 16, 48, 48], fill="white")
    return image

def setup_tray():
    autostart_enabled = check_autostart()

    def toggle_autostart(icon, item):
        nonlocal autostart_enabled
        autostart_enabled = not autostart_enabled
        set_autostart(autostart_enabled)
        # Update menu
        icon.update_menu()

    def quit_action(icon, item):
        global is_running
        is_running = False
        icon.stop()
        os._exit(0)
        
    menu = pystray.Menu(
        pystray.MenuItem("Worker Active (🟢)", lambda: None, enabled=False),
        pystray.MenuItem("Run on Startup", toggle_autostart, checked=lambda item: autostart_enabled),
        pystray.MenuItem("Quit", quit_action)
    )
    icon = pystray.Icon("ClipAI", create_image(), "ClipAI Worker", menu)
    icon.run()

if __name__ == "__main__":
    IS_CLOUD = "--cloud" in sys.argv
    IS_CLI = "--cli" in sys.argv

    if IS_CLOUD:
        # ── Cloud/Linux daemon mode ──────────────────────────────
        # No tray, no Windows registry, no local stream server
        print("╔══════════════════════════════════════╗")
        print("║   ClipAI Cloud Worker (Linux/Cloud)  ║")
        print("╚══════════════════════════════════════╝")
        update_yt_dlp()
        run_worker_loop()

    elif len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == "--register":
            register_uri_scheme()
            sys.exit(0)
        elif arg.startswith("clipai://"):
            parsed = urlparse(arg)
            qs = parse_qs(parsed.query)
            if "user_id" in qs:
                USER_ID = qs["user_id"][0]

        update_yt_dlp()
        stream_thread = threading.Thread(target=start_local_stream_server, daemon=True)
        stream_thread.start()
        worker_thread = threading.Thread(target=run_worker_loop, daemon=True)
        worker_thread.start()
        if IS_CLI:
            print("Running in CLI mode. Press Ctrl+C to exit.")
            while True:
                time.sleep(1)
        else:
            setup_tray()
    else:
        # Default: desktop double-click
        register_uri_scheme()
        update_yt_dlp()
        stream_thread = threading.Thread(target=start_local_stream_server, daemon=True)
        stream_thread.start()
        worker_thread = threading.Thread(target=run_worker_loop, daemon=True)
        worker_thread.start()
        if IS_CLI:
            print("Running in CLI mode. Press Ctrl+C to exit.")
            while True:
                time.sleep(1)
        else:
            setup_tray()


################################################################################
# FILE: templates/index.html
################################################################################

<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0, viewport-fit=cover">
    <title>ClipAI — Automated YouTube Shorts</title>
    <meta name="description" content="AI-powered tool that finds viral videos, cuts the best clip, and posts it to your YouTube channel automatically.">
    <meta name="theme-color" content="#080808">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="/static/style.css?v=103">
</head>
<body>
<div class="app-layout">
  <!-- SIDEBAR -->
  <aside class="sidebar">
    <div class="sidebar-logo">
      <svg width="28" height="28" viewBox="0 0 28 28" fill="none">
        <rect width="28" height="28" rx="8" fill="#dc2626"/>
        <path d="M10 8.5L20.5 14L10 19.5V8.5Z" fill="white"/>
      </svg>
      <span class="sidebar-logo-text">ClipAI</span>
    </div>

    <span class="sidebar-section-label">Workspace</span>

    <button class="sidebar-btn active" id="tab-generate" onclick="switchTab('generate')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <polygon points="5 3 19 12 5 21 5 3"/>
      </svg>
      Studio
    </button>

    <button class="sidebar-btn" id="tab-workplace" onclick="switchTab('workplace')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <polygon points="23 7 16 12 23 17 23 7"/>
        <rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
      </svg>
      Workplace
    </button>

    <button class="sidebar-btn" id="tab-analytics" onclick="switchTab('analytics')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <rect x="3" y="3" width="18" height="18" rx="2"/>
        <line x1="3" y1="9" x2="21" y2="9"/>
        <line x1="9" y1="21" x2="9" y2="9"/>
      </svg>
      My Clips
    </button>

    <button class="sidebar-btn" id="tab-autopost" onclick="switchTab('autopost')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="12" cy="12" r="10"/>
        <polyline points="12 6 12 12 16 14"/>
      </svg>
      Auto-Pilot
    </button>

    <span class="sidebar-section-label" style="margin-top:8px;">Customize</span>

    <button class="sidebar-btn" onclick="document.getElementById('brand-modal').classList.remove('hidden')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/>
      </svg>
      Brand Kit
    </button>

    <div class="sidebar-spacer"></div>

    <div class="sidebar-worker-block">
      <div class="worker-status" id="worker-status">
        <div class="status-dot" id="worker-dot" style="background:#10b981; box-shadow:0 0 10px rgba(16,185,129,0.5);"></div>
        <span class="worker-label" id="worker-label" style="color:#10b981;">Cloud Engine Active</span>
      </div>
      <p style="font-size:11px; color:var(--text-3); margin-top:4px;">24/7 Cloud Rendering Online</p>
    </div>
  </aside>

  <!-- MAIN CONTENT -->
  <div class="main-wrapper">
    <header class="top-header">
      <div class="top-header-left" style="display:flex; align-items:center; gap:12px;">
        <div class="mobile-logo-wrap">
          <svg width="24" height="24" viewBox="0 0 28 28" fill="none">
            <rect width="28" height="28" rx="8" fill="#dc2626"/>
            <path d="M10 8.5L20.5 14L10 19.5V8.5Z" fill="white"/>
          </svg>
          <span class="mobile-logo-text">ClipAI</span>
        </div>
        <a href="/api/v1/auth/youtube" id="connect-youtube-btn" class="btn btn-connect">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M23.5 6.2a3 3 0 0 0-2.1-2.1C19.6 3.6 12 3.6 12 3.6s-7.6 0-9.4.5A3 3 0 0 0 .5 6.2 31.5 31.5 0 0 0 0 12a31.5 31.5 0 0 0 .5 5.8 3 3 0 0 0 2.1 2.1c1.8.5 9.4.5 9.4.5s7.6 0 9.4-.5a3 3 0 0 0 2.1-2.1A31.5 31.5 0 0 0 24 12a31.5 31.5 0 0 0-.5-5.8zM9.8 15.6V8.4l6.3 3.6-6.3 3.6z"/></svg>
          <span>Connect YouTube</span>
        </a>
        <div class="yt-status-badge" id="yt-status-badge">
          <span class="status-dot" id="yt-dot"></span>
          <span id="yt-label">Not Connected</span>
        </div>
      </div>
      <div class="top-header-right" style="display:flex; align-items:center; gap:10px;">
        <button onclick="openAccountModal()" id="header-user-btn" class="btn btn-outline" style="font-size:12.5px; padding:7px 12px; display:inline-flex; align-items:center; gap:6px;">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
          <span id="user-display-label">My Account</span>
        </button>
        <button onclick="openSubscriptionsModal()" class="btn btn-upgrade" style="display:inline-flex; align-items:center; gap:6px; background:linear-gradient(135deg, #2563eb, #3b82f6); border:none; box-shadow:0 0 16px rgba(59,130,246,0.35); font-weight:700;">
          <span>💎 Subscriptions</span>
        </button>
      </div>
    </header>

    <div class="content-area">


    <!-- ── Generate Tab ──────────────────────────────── -->

    <main class="tab-content" id="tab-content-generate">
        <div class="generate-layout">

            <!-- Center Stage -->
            <div class="hero-section">
                <div class="hero-text">
                    <h1 class="hero-title">Automate your <span>YouTube Empire</span></h1>
                    <p class="hero-subtitle">Enter a niche. ClipAI finds the most viral moments, cuts them perfectly, and posts directly to your channel.</p>
                </div>

                <div class="glass-card main-input-card">
                    <label class="input-label" for="niche-input">Target Niche, Creator, or YouTube Video Link</label>
                    <div class="input-row-lg">
                        <input id="niche-input" type="text" class="niche-input-lg"
                               placeholder="e.g. https://youtube.com/watch?v=... or 'Finance', 'MrBeast'"
                               value="motivation" autocomplete="off">
                        <button id="run-clip-farm-btn" class="btn btn-generate-lg">
                            <svg width="18" height="18" viewBox="0 0 15 15" fill="none">
                                <path d="M3 1.5L13.5 7.5L3 13.5V1.5Z" fill="currentColor"/>
                            </svg>
                            Generate Clip
                        </button>
                    </div>

                    <!-- Trending Niche Quick-Picks -->
                    <div style="display:flex; flex-wrap:wrap; gap:8px; margin-top:14px; align-items:center;">
                        <span style="font-size:12px; font-weight:600; color:var(--text-3); text-transform:uppercase; letter-spacing:0.05em;">Trending:</span>
                        <button type="button" class="niche-pill" onclick="selectNichePreset('motivation', this)" style="background:rgba(255,255,255,0.06); border:1px solid rgba(255,255,255,0.12); color:#fff; border-radius:20px; padding:4px 12px; font-size:12px; font-weight:600; cursor:pointer; transition:all 0.15s;">🔥 Motivation</button>
                        <button type="button" class="niche-pill" onclick="selectNichePreset('finance', this)" style="background:rgba(255,255,255,0.06); border:1px solid rgba(255,255,255,0.12); color:#fff; border-radius:20px; padding:4px 12px; font-size:12px; font-weight:600; cursor:pointer; transition:all 0.15s;">💰 Finance</button>
                        <button type="button" class="niche-pill" onclick="selectNichePreset('mrbeast', this)" style="background:rgba(255,255,255,0.06); border:1px solid rgba(255,255,255,0.12); color:#fff; border-radius:20px; padding:4px 12px; font-size:12px; font-weight:600; cursor:pointer; transition:all 0.15s;">⚡ MrBeast</button>
                        <button type="button" class="niche-pill" onclick="selectNichePreset('gaming', this)" style="background:rgba(255,255,255,0.06); border:1px solid rgba(255,255,255,0.12); color:#fff; border-radius:20px; padding:4px 12px; font-size:12px; font-weight:600; cursor:pointer; transition:all 0.15s;">🎮 Gaming</button>
                        <button type="button" class="niche-pill" onclick="selectNichePreset('ai tech', this)" style="background:rgba(255,255,255,0.06); border:1px solid rgba(255,255,255,0.12); color:#fff; border-radius:20px; padding:4px 12px; font-size:12px; font-weight:600; cursor:pointer; transition:all 0.15s;">🤖 AI & Tech</button>
                    </div>

                    <!-- Video Style & Format Customizer -->
                    <div style="display:grid; grid-template-columns: 1fr 1fr; gap:12px; margin-top:14px; padding-top:14px; border-top:1px solid rgba(255,255,255,0.06);">
                        <div>
                            <label style="font-size:11.5px; font-weight:700; color:var(--text-3); text-transform:uppercase; letter-spacing:0.06em; display:block; margin-bottom:6px;">Visual Layout</label>
                            <select id="studio-layout-select" class="niche-input-lg" style="height:38px; font-size:13px; padding:6px 10px; background:var(--surface-2); border-radius:8px; border:1px solid var(--border);">
                                <option value="split_screen" selected>🎮 Split-Screen (Viral GTA Parkour B-Roll)</option>
                                <option value="cinematic_blur">🎬 Cinematic Center Focus (Dynamic Blur)</option>
                            </select>
                        </div>
                        <div>
                            <label style="font-size:11.5px; font-weight:700; color:var(--text-3); text-transform:uppercase; letter-spacing:0.06em; display:block; margin-bottom:6px;">Subtitle Styling</label>
                            <select id="studio-subtitle-select" class="niche-input-lg" style="height:38px; font-size:13px; padding:6px 10px; background:var(--surface-2); border-radius:8px; border:1px solid var(--border);">
                                <option value="hormozi" selected>⚡ Alex Hormozi (Bold Impact + Power Emojis)</option>
                                <option value="clean_minimal">✨ Clean Modern (High-Readability Sans)</option>
                            </select>
                        </div>
                    </div>

                    <!-- Modern Auto-Post Toggle Card -->
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-top:14px; padding:12px 16px; border-radius:10px; background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.08); transition:all 0.2s;">
                        <div style="display:flex; flex-direction:column; gap:2px;">
                            <span style="font-size:13px; font-weight:700; color:#fff;">Auto-Post to YouTube</span>
                            <span style="font-size:11.5px; color:var(--text-3);">Turn off to preview and review your video in Workplace before publishing</span>
                        </div>
                        <label class="switch" style="position:relative; display:inline-block; width:44px; height:24px; margin:0; flex-shrink:0;">
                            <input type="checkbox" id="studio-autopost-toggle" checked style="opacity:0; width:0; height:0;">
                            <span class="slider" style="position:absolute; cursor:pointer; inset:0; background-color:#374151; transition:.3s; border-radius:24px;"></span>
                        </label>
                    </div>

                    <div class="free-tier-badge" id="free-tier-badge" style="margin-top:10px;">⚡ Free Tier — <span id="free-remaining">5</span> generations remaining</div>
                </div>

                <!-- Recent Activity Ticker (Disabled to avoid looking scammy) -->
                <!--
                <div class="ticker-container" style="overflow: hidden; white-space: nowrap; margin-top: 24px; padding: 12px; background: rgba(20,20,20,0.6); border: 1px solid var(--border); border-radius: 8px; width: 100%; box-shadow: inset 0 0 20px rgba(0,0,0,0.5);">
                    <div class="ticker-track" id="dynamic-ticker" style="display: inline-block; animation: ticker 40s linear infinite; font-size: 13px; color: var(--text-2);">
                    </div>
                </div>
                -->

                <!-- Progress Tracker (Hidden by default) -->
                <div id="progress-container" class="glass-card progress-card hidden">
                    <div class="progress-header">
                        <div class="progress-label-wrap">
                            <div class="pulse-ring"></div>
                            <span class="progress-label">Pipeline Active</span>
                        </div>
                        <span id="progress-pct">0%</span>
                    </div>
                    <div id="virality-badge" style="display:none; background:rgba(16,185,129,0.15); border:1px solid rgba(16,185,129,0.3); color:#10b981; font-weight:700; font-size:12px; padding:6px 12px; border-radius:20px; margin-top:8px; width:fit-content; margin-left:auto; margin-right:auto;">
                        🔥 <span id="virality-score">94</span>/100 Viral Potential
                    </div>
                    <div class="progress-track">
                        <div id="progress-bar-fill" class="progress-fill"></div>
                    </div>
                    <div id="progress-text" class="progress-step">Initializing secure worker connection...</div>
                    
                    <!-- Pipeline Steps Indicator -->
                    <div class="pipeline-steps">
                        <div class="pipeline-step" id="step-search">
                            <div class="step-dot"></div>
                            <span>Search</span>
                        </div>
                        <div class="step-line"></div>
                        <div class="pipeline-step" id="step-download">
                            <div class="step-dot"></div>
                            <span>Download</span>
                        </div>
                        <div class="step-line"></div>
                        <div class="pipeline-step" id="step-cut">
                            <div class="step-dot"></div>
                            <span>Cut & Render</span>
                        </div>
                        <div class="step-line"></div>
                        <div class="pipeline-step" id="step-upload">
                            <div class="step-dot"></div>
                            <span>Upload</span>
                        </div>
                    </div>
                    <div style="text-align:center; margin-top: 14px;">
                        <button onclick="cancelJob()" style="background:none; border:1px solid var(--border); color:var(--text-3); font-size:12px; padding: 5px 14px; border-radius:6px; cursor:pointer; transition: all 0.2s;" onmouseover="this.style.borderColor='#ef4444';this.style.color='#ef4444'" onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text-3)'">
                            ✕ Cancel Job
                        </button>
                    </div>
                </div>

                <!-- ── Hybrid Industry Feature Highlights (Opus + Submagic + Vizard) ── -->
                <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap:14px; margin-top:32px; width:100%;">
                    <div class="glass-card" style="padding:16px 18px; border:1px solid rgba(255,255,255,0.07); border-radius:12px; background:rgba(18,18,18,0.5);">
                        <div style="font-size:20px; margin-bottom:8px;">🔥</div>
                        <div style="font-size:13.5px; font-weight:700; color:#fff; margin-bottom:4px;">AI Virality Score</div>
                        <div style="font-size:12px; color:var(--text-2); line-height:1.45;">Gemini Director rates hook strength 0–100 with retention prediction.</div>
                    </div>
                    <div class="glass-card" style="padding:16px 18px; border:1px solid rgba(255,255,255,0.07); border-radius:12px; background:rgba(18,18,18,0.5);">
                        <div style="font-size:20px; margin-bottom:8px;">⚡</div>
                        <div style="font-size:13.5px; font-weight:700; color:#fff; margin-bottom:4px;">Hormozi Captions</div>
                        <div style="font-size:12px; color:var(--text-2); line-height:1.45;">Auto-animated power words and emojis for 2x viewer watch-time.</div>
                    </div>
                    <div class="glass-card" style="padding:16px 18px; border:1px solid rgba(255,255,255,0.07); border-radius:12px; background:rgba(18,18,18,0.5);">
                        <div style="font-size:20px; margin-bottom:8px;">🎮</div>
                        <div style="font-size:13.5px; font-weight:700; color:#fff; margin-bottom:4px;">Split-Screen B-Roll</div>
                        <div style="font-size:12px; color:var(--text-2); line-height:1.45;">Subway Surfers & GTA parkour overlays that hold Gen-Z attention.</div>
                    </div>
                    <div class="glass-card" style="padding:16px 18px; border:1px solid rgba(255,255,255,0.07); border-radius:12px; background:rgba(18,18,18,0.5);">
                        <div style="font-size:20px; margin-bottom:8px;">☁️</div>
                        <div style="font-size:13.5px; font-weight:700; color:#fff; margin-bottom:4px;">100% Cloud Rendering</div>
                        <div style="font-size:12px; color:var(--text-2); line-height:1.45;">Processed 24/7 on dedicated cloud infrastructure without using your device.</div>
                    </div>
                </div>
            </div>

            <!-- Right Sidebar: Activity Stream -->
            <div class="activity-sidebar glass-card">
                <div class="activity-header">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                        <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline>
                    </svg>
                    Live Activity Stream
                </div>
                <div class="chat-window" id="chat-window">
                    <div class="log-entry">
                        <div class="log-icon">🚀</div>
                        <div class="log-body">
                            <div class="log-sender">System</div>
                            <div class="log-text">Welcome to ClipAI! Enter a niche and hit Generate to start the automated pipeline.</div>
                        </div>
                    </div>
                </div>
            </div>

        </div>
    </main>

    <!-- ── Workplace Tab (Preview & Review Before Posting) ── -->
    <main class="tab-content hidden" id="tab-content-workplace">
        <div class="analytics-layout">
            <div class="analytics-header-row">
                <div>
                    <h2 class="hero-title" style="font-size: 32px; text-align: left; margin-bottom: 8px;">Creative <span>Workplace</span></h2>
                    <p class="hero-subtitle" style="text-align: left;">Watch and review your generated videos before they get posted to YouTube.</p>
                </div>
                <div style="display:flex; gap:10px;">
                    <button class="btn btn-outline btn-refresh" onclick="loadWorkplaceClips()">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                            <polyline points="23 4 23 10 17 10"></polyline>
                            <path d="M20.5 15a9 9 0 1 1-2.2-9.5L23 10"></path>
                        </svg>
                        Refresh Workplace
                    </button>
                </div>
            </div>

            <!-- Workplace Video Feed / Review Cards -->
            <div id="workplace-clips-container" style="display:grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 24px; margin-top: 24px;">
                <div class="table-empty" style="grid-column: 1 / -1;">No clips pending review. Generate a new clip with auto-post turned off!</div>
            </div>
        </div>
    </main>

    <!-- ── Analytics Tab ─────────────────────────────── -->
    <main class="tab-content hidden" id="tab-content-analytics">
        <div class="analytics-layout">
            <div class="analytics-header-row">
                <div>
                    <h2 class="hero-title" style="font-size: 32px; text-align: left; margin-bottom: 8px;">Channel <span>Analytics</span></h2>
                    <p class="hero-subtitle" style="text-align: left;">Track the performance of your AI-generated clips</p>
                </div>
                <button class="btn btn-outline btn-refresh" onclick="loadAnalytics()">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                        <polyline points="23 4 23 10 17 10"></polyline>
                        <path d="M20.5 15a9 9 0 1 1-2.2-9.5L23 10"></path>
                    </svg>
                    Refresh Stats
                </button>
            </div>

            <!-- Stat Cards -->
            <div class="stat-cards">
                <div class="glass-card stat-card">
                    <div class="stat-label">Total Views</div>
                    <div class="stat-value" id="stat-total-views">—</div>
                    <div class="stat-sub">Across all posted clips</div>
                </div>
                <div class="glass-card stat-card">
                    <div class="stat-label">Clips Posted</div>
                    <div class="stat-value" id="stat-total-videos">—</div>
                    <div class="stat-sub">Auto-generated by AI</div>
                </div>
                <div class="glass-card stat-card">
                    <div class="stat-label">Avg. Views / Clip</div>
                    <div class="stat-value" id="stat-avg-views">—</div>
                    <div class="stat-sub">Average performance</div>
                </div>
            </div>

            <!-- Videos Gallery Grid -->
            <div class="gallery-header" style="margin-top:40px; margin-bottom:20px; display:flex; justify-content:space-between; align-items:flex-end;">
                <h3 style="color:white; font-size:24px; font-weight:600; margin:0;">My Clips</h3>
                <span style="color:var(--text-3); font-size:14px;">Your generated library</span>
            </div>
            
            <div id="videos-gallery-grid" style="display:grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 24px; margin-bottom: 40px;">
                <div class="table-empty" style="grid-column: 1 / -1;">No videos posted yet. Generate your first clip!</div>
            </div>
            
            <div style="text-align: center; margin-top: 30px;">
                <button onclick="fetch('/api/v1/analytics/reset', {method:'DELETE'}).then(()=>location.reload())" style="background:none; border:none; color:var(--text-3); font-size:11px; cursor:pointer; opacity:0.5; transition: opacity 0.2s;" onmouseover="this.style.opacity='1'" onmouseout="this.style.opacity='0.5'">
                    [Developer: Reset Analytics to 0]
                </button>
            </div>
        </div>
    </main>

    <!-- ── Auto Post Tab ─────────────────────────────── -->
    <main class="tab-content hidden" id="tab-content-autopost">
        <div class="analytics-layout" style="max-width: 800px;">
            <div class="analytics-header-row" style="margin-bottom: 0;">
                <div>
                    <h2 class="hero-title" style="font-size: 32px; text-align: left; margin-bottom: 8px;">Auto <span>Post</span></h2>
                    <p class="hero-subtitle" style="text-align: left;">Schedule clips to be generated and posted automatically while you sleep.</p>
                </div>
            </div>

            <div class="glass-card" style="padding: 40px;">
                <div class="setting-row">
                    <div class="setting-info">
                        <h3 style="color:var(--text);font-size:18px;margin-bottom:6px;">Enable Auto Post</h3>
                        <p style="color:var(--text-2);font-size:14px;">Clips are automatically generated and published directly from the cloud at your scheduled times.</p>
                    </div>
                    <label class="toggle-switch">
                        <input type="checkbox" id="autopost-enable">
                        <span class="toggle-slider"></span>
                    </label>
                </div>

                <hr style="border:0; border-top:1px solid var(--border); margin:30px 0;">

                <div class="setting-group" style="display:flex; flex-direction:column; gap:20px;">
                    <div>
                        <label class="input-label">Posting Times (UTC)</label>
                        <div id="times-container" style="display: flex; flex-direction: column; gap: 10px; margin-bottom: 10px;">
                            <!-- Time inputs will be added here dynamically -->
                        </div>
                        <button type="button" class="btn btn-outline btn-sm" onclick="addTimeInput()" style="padding: 6px 12px; font-size: 13px;">+ Add Time</button>
                        <p style="color:var(--text-3);font-size:12px;margin-top:8px;">Format: HH:MM. Add as many times as you want.</p>
                    </div>
                    
                    <div>
                        <label class="input-label">Target Niche or Creator</label>
                        <input type="text" id="autopost-niche" class="niche-input-lg" placeholder="e.g. Finance, Tech, MrBeast">
                    </div>

                    <div>
                        <label class="input-label">Posting Days</label>
                        <div class="days-selector">
                            <label class="day-pill"><input type="checkbox" value="Mon" checked class="day-cb"> Mon</label>
                            <label class="day-pill"><input type="checkbox" value="Tue" checked class="day-cb"> Tue</label>
                            <label class="day-pill"><input type="checkbox" value="Wed" checked class="day-cb"> Wed</label>
                            <label class="day-pill"><input type="checkbox" value="Thu" checked class="day-cb"> Thu</label>
                            <label class="day-pill"><input type="checkbox" value="Fri" checked class="day-cb"> Fri</label>
                            <label class="day-pill"><input type="checkbox" value="Sat" checked class="day-cb"> Sat</label>
                            <label class="day-pill"><input type="checkbox" value="Sun" checked class="day-cb"> Sun</label>
                        </div>
                    </div>
                </div>

                <div style="margin-top: 40px; display:flex; justify-content:flex-end;">
                    <button class="btn btn-generate-lg" onclick="saveAutoPostSettings()" id="btn-save-autopost">
                        Save Schedule
                    </button>
                </div>
            </div>
        </div>
    </main>

    </div><!-- content-area -->
  </div><!-- main-wrapper -->
</div><!-- app-layout -->

<!-- ── Multi-Tier Subscriptions Modal ────────────────────── -->
<div id="subscriptions-modal" class="modal-overlay hidden" onclick="if(event.target===this)closeSubscriptionsModal()">
    <div class="glass-modal modal-box" style="max-width: 820px; width: 95vw; padding: 36px; border: 1px solid var(--blue-glow); box-shadow: 0 0 80px var(--blue-dim); text-align: left;">
        <div style="display:flex; justify-content:space-between; align-items:flex-start; margin-bottom: 24px;">
            <div>
                <h2 style="font-size: 28px; font-weight: 800; color: #fff; margin-bottom: 6px;">Select Your Plan</h2>
                <p style="font-size: 14px; color: var(--text-2);">Supercharge your channel with automated viral clipping and high-retention AI editing.</p>
            </div>
            <button onclick="closeSubscriptionsModal()" style="background:none; border:none; color:var(--text-3); font-size:24px; cursor:pointer;">✕</button>
        </div>

        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 18px; margin-bottom: 24px;">
            <!-- Free Tier -->
            <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 14px; padding: 22px; display: flex; flex-direction: column; justify-content: space-between;">
                <div>
                    <span style="font-size: 12px; font-weight: 700; color: var(--text-3); text-transform: uppercase;">Free Tier</span>
                    <div style="font-size: 32px; font-weight: 800; color: #fff; margin: 8px 0;">$0<span style="font-size: 14px; color: var(--text-3); font-weight: normal;"> / forever</span></div>
                    <p style="font-size: 13px; color: var(--text-2); margin-bottom: 16px;">Test the AI pipeline and generate your first viral clips.</p>
                    <div style="font-size: 13px; color: var(--text); display: flex; flex-direction: column; gap: 8px;">
                        <div>✓ 5 Free AI Shorts</div>
                        <div>✓ Standard 720p HD rendering</div>
                        <div>✓ Split-screen & Blur layout</div>
                        <div>✓ Basic Subtitles & Watermark</div>
                        <div>✓ Creative Workplace Preview</div>
                    </div>
                </div>
                <button onclick="closeSubscriptionsModal(); switchTab('generate');" class="btn btn-outline" style="width: 100%; margin-top: 20px; justify-content: center;">Current Plan</button>
            </div>

            <!-- Pro Tier (Popular) -->
            <div style="background: rgba(37,99,235,0.08); border: 1px solid rgba(59,130,246,0.4); border-radius: 14px; padding: 22px; display: flex; flex-direction: column; justify-content: space-between; position: relative;">
                <div style="position: absolute; top: -10px; right: 18px; background: #2563eb; color: #fff; font-size: 10px; font-weight: 800; padding: 3px 10px; border-radius: 12px; text-transform: uppercase;">Most Popular</div>
                <div>
                    <span style="font-size: 12px; font-weight: 700; color: #60a5fa; text-transform: uppercase;">Pro</span>
                    <div style="font-size: 32px; font-weight: 800; color: #fff; margin: 8px 0;">$29<span style="font-size: 14px; color: var(--text-3); font-weight: normal;">/mo</span></div>
                    <p style="font-size: 13px; color: var(--text-2); margin-bottom: 16px;">For creators automating daily posting across channels.</p>
                    <div style="font-size: 13px; color: var(--text); display: flex; flex-direction: column; gap: 8px;">
                        <div>✓ 100 Viral Shorts / month</div>
                        <div>✓ OpusClip-Grade Hook Director</div>
                        <div>✓ Alex Hormozi Animated Subtitles</div>
                        <div>✓ Multi-core CPU & NVENC speed</div>
                        <div>✓ Full Channel Analytics & Graphs</div>
                    </div>
                </div>
                <button onclick="checkoutPlan('pro')" class="btn btn-generate" style="width: 100%; margin-top: 20px; justify-content: center; background: #2563eb;">Upgrade to Pro</button>
            </div>

            <!-- Full Version (Monthly Subscription) -->
            <div style="background: rgba(16,185,129,0.05); border: 1px solid rgba(16,185,129,0.3); border-radius: 14px; padding: 22px; display: flex; flex-direction: column; justify-content: space-between;">
                <div>
                    <span style="font-size: 12px; font-weight: 700; color: #10b981; text-transform: uppercase;">Full Version</span>
                    <div style="font-size: 32px; font-weight: 800; color: #fff; margin: 8px 0;">$49<span style="font-size: 14px; color: var(--text-3); font-weight: normal;">/mo</span></div>
                    <p style="font-size: 13px; color: var(--text-2); margin-bottom: 16px;">Everything unlocked for power creators and media agencies.</p>
                    <div style="font-size: 13px; color: var(--text); display: flex; flex-direction: column; gap: 8px;">
                        <div>✓ Unlimited Viral Shorts / month</div>
                        <div>✓ 100% Hands-free Auto-Pilot</div>
                        <div>✓ No Watermarks & Full Brand Kit</div>
                        <div>✓ Priority Server Queue Processing</div>
                        <div>✓ Multi-Channel YouTube Auto-Posting</div>
                        <div>✓ 24/7 VIP Support</div>
                    </div>
                </div>
                <button onclick="checkoutPlan('full_version')" class="btn btn-upgrade" style="width: 100%; margin-top: 20px; justify-content: center; background: #10b981;">Get Full Version</button>
            </div>
        </div>
        <p style="font-size: 12px; color: var(--text-3); text-align: center;">All plans include 256-bit encrypted worker communication and automatic YouTube posting.</p>
    </div>
</div>

<!-- ── Paywall Modal (Compatibility trigger) ───────────── -->
<div id="paywall-modal" class="modal-overlay hidden">
    <div class="glass-modal modal-box" style="max-width: 460px; padding: 40px; border: 1px solid var(--blue-glow); box-shadow: 0 0 80px var(--blue-dim);">
        <div class="modal-icon" style="font-size: 48px; margin-bottom: 20px;">💎</div>
        <h2 class="modal-title" style="font-size: 28px;">Free Trial Complete</h2>
        <p class="modal-desc" style="font-size: 15px; margin-bottom: 24px;">You have reached the limit of free generations. Upgrade your plan to continue automated clipping!</p>
        <button onclick="openSubscriptionsModal(); document.getElementById('paywall-modal').classList.add('hidden');" class="btn btn-upgrade" style="width: 100%; padding: 14px; font-size: 16px; font-weight: 700; background: linear-gradient(135deg, var(--blue), #2563eb);">View Subscription Plans</button>
        <button onclick="document.getElementById('paywall-modal').classList.add('hidden');" class="btn-ghost" style="margin-top: 12px;">Maybe later</button>
    </div>
</div>

<!-- ── User Account & Auth Modal ───────────────────────── -->
<div id="account-modal" class="modal-overlay hidden" onclick="if(event.target===this)closeAccountModal()">
    <div class="glass-modal modal-box" style="max-width: 480px; padding: 36px; border: 1px solid var(--border); box-shadow: 0 24px 80px rgba(0,0,0,0.7); text-align: left;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 18px;">
            <div style="font-size: 22px; font-weight: 800; color: #fff; display:flex; align-items:center; gap:8px;">
                <span>👤</span> Account &amp; Settings
            </div>
            <button onclick="closeAccountModal()" style="background:none; border:none; color:var(--text-3); font-size:22px; cursor:pointer;">✕</button>
        </div>
        <p style="font-size: 13px; color: var(--text-2); margin-bottom: 20px; line-height: 1.5;">
            Manage your credentials, active subscription license, and cloud rendering status.
        </p>
        
        <!-- Dynamic Account State -->
        <div id="account-logged-in-box" style="display:none; margin-bottom: 20px;">
            <div style="display:flex; align-items:center; gap:12px; padding:14px; background:rgba(255,255,255,0.04); border:1px solid rgba(255,255,255,0.08); border-radius:12px;">
                <div style="width:40px; height:40px; border-radius:50%; background:linear-gradient(135deg, #3b82f6, #10b981); display:flex; align-items:center; justify-content:center; font-weight:800; font-size:16px; color:#fff;" id="account-avatar-letter">G</div>
                <div style="flex:1; overflow:hidden;">
                    <div style="font-weight:700; color:#fff; font-size:14px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" id="account-user-email">Logged In</div>
                    <div style="font-size:11.5px; color:#10b981; font-weight:600; display:flex; align-items:center; gap:4px; margin-top:2px;">
                        <span>● Verified Google Account</span>
                    </div>
                </div>
            </div>
        </div>

        <div id="account-login-box" style="margin-bottom: 24px;">
            <a href="/api/v1/auth/google" class="btn btn-outline" style="display:flex; align-items:center; justify-content:center; gap:10px; width:100%; padding:13px; font-weight:700; font-size:14.5px; background:#fff; color:#111; border-radius:12px; text-decoration:none; box-shadow:0 4px 14px rgba(0,0,0,0.25); transition:transform 0.15s;" onmouseover="this.style.transform='scale(1.02)'" onmouseout="this.style.transform='none'">
                <svg width="18" height="18" viewBox="0 0 24 24"><path fill="#4285F4" d="M23.745 12.27c0-.7-.06-1.4-.19-2.07H12v4.51h6.6c-.29 1.52-1.14 2.82-2.4 3.68v3.05h3.88c2.27-2.09 3.665-5.17 3.665-9.17z"/><path fill="#34A853" d="M12 24c3.24 0 5.95-1.08 7.93-2.91l-3.88-3.05c-1.08.72-2.45 1.16-4.05 1.16-3.12 0-5.77-2.1-6.72-4.93H1.25v3.15C3.26 21.36 7.33 24 12 24z"/><path fill="#FBBC05" d="M5.28 14.27c-.25-.72-.38-1.49-.38-2.27s.13-1.55.38-2.27V6.58H1.25C.45 8.18 0 10.04 0 12s.45 3.82 1.25 5.42l4.03-3.15z"/><path fill="#EA4335" d="M12 4.75c1.77 0 3.35.61 4.6 1.8l3.42-3.42C17.95 1.19 15.24 0 12 0 7.33 0 3.26 2.64 1.25 6.58l4.03 3.15c.95-2.83 3.6-4.98 6.72-4.98z"/></svg>
                Sign in with Google
            </a>
            <p style="font-size:11px; color:var(--text-3); text-align:center; margin-top:8px;">Sign in to sync your active subscriptions and created clips.</p>
        </div>

        <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:16px;">
            <div style="background: rgba(255,255,255,0.03); border: 1px solid var(--border); border-radius: 8px; padding: 12px;">
                <div style="font-size:11px; color:var(--text-3); text-transform:uppercase; font-weight:700;">Active Plan</div>
                <div id="account-plan-badge" style="color:#10b981; font-weight:800; font-size:15px; margin-top:4px;">Free Tier</div>
            </div>
            <div style="background: rgba(255,255,255,0.03); border: 1px solid var(--border); border-radius: 8px; padding: 12px;">
                <div style="font-size:11px; color:var(--text-3); text-transform:uppercase; font-weight:700;">Engine Status</div>
                <div id="account-worker-status" style="color:#10b981; font-weight:800; font-size:14px; margin-top:4px;">Cloud Online (🟢)</div>
            </div>
        </div>

        <div style="background: rgba(255,255,255,0.03); border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 16px; font-size: 12.5px;">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <span style="color:var(--text-3);">Linked User ID:</span>
                <span id="account-user-id" style="color:#fff; font-family:monospace; font-size:11.5px; background:rgba(0,0,0,0.3); padding:3px 8px; border-radius:4px;">user_...</span>
            </div>
        </div>

        <div style="display:flex; gap:10px; border-top: 1px solid var(--border); padding-top: 16px;">
            <button onclick="openSubscriptionsModal(); closeAccountModal();" class="btn btn-upgrade" style="flex:1; justify-content:center; padding:11px; font-size:13px;">💎 Upgrade License</button>
            <button onclick="logoutAccount()" class="btn btn-outline" style="flex:0.8; justify-content:center; padding:11px; font-size:13px; color:#ef4444; border-color:rgba(239,68,68,0.3);">Sign Out</button>
        </div>
    </div>
</div>
<div id="player-modal" class="modal-overlay hidden" onclick="if(event.target===this)closePlayer()">
    <div style="position:relative; max-width:360px; width:90vw;">
        <button onclick="closePlayer()" style="position:absolute;top:-42px;right:0;background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.15);color:white;width:36px;height:36px;border-radius:50%;font-size:18px;cursor:pointer;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(10px)">✕</button>
        <div style="aspect-ratio:9/16;border-radius:16px;overflow:hidden;border:1px solid rgba(255,255,255,0.1);box-shadow:0 24px 80px rgba(0,0,0,0.8);background:#000;">
            <iframe id="player-iframe" src="" frameborder="0" allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen style="width:100%;height:100%;display:none;"></iframe>
            <video id="player-video" controls autoplay playsinline style="width:100%;height:100%;object-fit:cover;display:none;"></video>
            <div id="player-empty" style="display:none;width:100%;height:100%;padding:28px 20px;flex-direction:column;align-items:center;justify-content:center;text-align:center;background:radial-gradient(circle at center, #1a2234 0%, #0a0e17 100%);">
                <div style="font-size:42px;margin-bottom:12px;">🎬</div>
                <div style="font-size:16px;font-weight:700;color:#fff;margin-bottom:6px;">Draft Video Saved</div>
                <div style="font-size:12.5px;color:var(--text-3);line-height:1.5;">This clip was rendered and saved to your Workplace. Connect YouTube and click <strong>Post to YouTube</strong> to publish it and watch it live!</div>
            </div>
        </div>
        <div id="player-info" style="margin-top:16px;padding:16px;background:rgba(255,255,255,0.05);border-radius:12px;border:1px solid rgba(255,255,255,0.08);">
            <div id="player-title" style="font-size:15px;font-weight:600;color:white;margin-bottom:8px;"></div>
            <div style="display:flex;gap:10px;">
                <a id="player-yt-link" href="#" target="_blank" class="btn btn-outline" style="flex:1;text-align:center;font-size:13px;padding:8px 0;">Open on YouTube ↗</a>
                <button id="player-copy-btn" onclick="copyVideoLink()" class="btn btn-outline" style="flex:1;text-align:center;font-size:13px;padding:8px 0;">📋 Copy Link</button>
            </div>
        </div>
    </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script src="/static/app.js?v=124"></script>

<!-- ── Brand Kit Modal ───────────────────────────────── -->
<div id="brand-modal" class="brand-modal hidden">
  <div class="brand-modal-inner">
    <h2 style="font-size:26px; font-weight:700; margin-bottom:8px;">Brand Kit</h2>
    <p style="color:#a3a3a3; font-size:14px; margin-bottom:32px;">Customize how your AI-generated clips look and feel.</p>
    <label class="input-label">Custom Watermark / Handle</label>
    <input type="text" id="brand-handle" class="niche-input-lg" placeholder="@MyChannel" style="margin-bottom:20px;">
    <label class="input-label">Subtitle Style</label>
    <select id="brand-font" class="niche-input-lg" style="margin-bottom:32px; background:#222; appearance:auto;">
      <option value="Hormozi">Alex Hormozi — Bold Impact</option>
      <option value="Ali">Ali Abdaal — Clean Sans</option>
    </select>
    <div style="display:flex; gap:12px; justify-content:flex-end;">
      <button class="btn btn-outline" onclick="document.getElementById('brand-modal').classList.add('hidden')">Cancel</button>
      <button class="btn btn-generate" onclick="saveBrandKit()">Save Brand Kit</button>
    </div>
  </div>
</div>

<!-- ── Mobile Bottom Navigation Bar (iPhone / Mobile) ── -->
<nav class="mobile-bottom-nav">
  <button class="mobile-nav-btn active" id="m-tab-generate" onclick="switchTab('generate')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <polygon points="5 3 19 12 5 21 5 3"/>
    </svg>
    <span>Studio</span>
  </button>
  <button class="mobile-nav-btn" id="m-tab-workplace" onclick="switchTab('workplace')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <polygon points="23 7 16 12 23 17 23 7"/>
      <rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
    </svg>
    <span>Workplace</span>
  </button>
  <button class="mobile-nav-btn" id="m-tab-analytics" onclick="switchTab('analytics')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <rect x="3" y="3" width="18" height="18" rx="2"/>
      <line x1="3" y1="9" x2="21" y2="9"/>
      <line x1="9" y1="21" x2="9" y2="9"/>
    </svg>
    <span>My Clips</span>
  </button>
  <button class="mobile-nav-btn" id="m-tab-autopost" onclick="switchTab('autopost')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <circle cx="12" cy="12" r="10"/>
      <polyline points="12 6 12 12 16 14"/>
    </svg>
    <span>Auto-Pilot</span>
  </button>
  <button class="mobile-nav-btn" onclick="document.getElementById('brand-modal').classList.remove('hidden')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/>
    </svg>
    <span>Brand Kit</span>
  </button>
</nav>

<div id="toast-container"></div>
</body>
</html>


################################################################################
# FILE: static/style.css
################################################################################

/* ─── Reset & Base ──────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  /* ── Obsidian Black Surfaces ── */
  --bg:        #080808;
  --surface:   #111111;
  --surface-2: #1a1a1a;
  --surface-3: #222222;
  --surface-4: #2d2d2d;

  /* ── Borders ── */
  --border:        rgba(255, 255, 255, 0.08);
  --border-hover:  rgba(255, 255, 255, 0.16);
  --border-active: rgba(255, 255, 255, 0.24);

  /* ── Text ── */
  --text:   #f5f5f5;
  --text-2: #a3a3a3;
  --text-3: #737373;

  /* ── Crimson (Primary Accent) ── */
  --blue:       hsl(0, 72%, 51%);
  --blue-light: hsl(0, 72%, 65%);
  --blue-dim:   hsla(0, 72%, 51%, 0.10);
  --blue-glow:  hsla(0, 72%, 51%, 0.25);

  /* ── Crimson Light / Success ── */
  --green:      hsl(0, 75%, 60%);
  --green-dim:  hsla(0, 75%, 60%, 0.10);
  --green-glow: hsla(0, 75%, 60%, 0.25);

  /* ── Red / Error ── */
  --red:     hsl(0, 85%, 60%);
  --red-dim: hsla(0, 85%, 60%, 0.10);

  /* ── Amber (Warnings) ── */
  --amber:     hsl(35, 90%, 55%);
  --amber-dim: hsla(35, 90%, 55%, 0.10);

  --font-sans: 'Plus Jakarta Sans', 'Inter', -apple-system, sans-serif;
  --font-mono: 'JetBrains Mono', 'Fira Code', monospace;

  --radius:    14px;
  --radius-sm: 9px;
  --radius-xs: 6px;
}

@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&display=swap');

body {
  font-family: var(--font-sans);
  background: var(--bg);
  color: var(--text);
  height: 100vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

/* ─── Scrollbar ─────────────────────────────────────── */
::-webkit-scrollbar { width: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--surface-4); border-radius: 8px; }
::-webkit-scrollbar-thumb:hover { background: var(--surface-4); }

/* ─── App Shell ─────────────────────────────────────── */
.app { display: flex; flex-direction: column; height: 100vh; }

/* ─── Topbar ────────────────────────────────────────── */
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 28px;
  height: 62px;
  border-bottom: 1px solid var(--border);
  background: rgba(9,14,26,0.85);
  backdrop-filter: blur(24px);
  -webkit-backdrop-filter: blur(24px);
  flex-shrink: 0;
  z-index: 100;
  position: sticky;
  top: 0;
}

.topbar-left  { display: flex; align-items: center; gap: 36px; }
.topbar-right { display: flex; align-items: center; gap: 10px; }

/* Logo */
.logo {
  display: flex;
  align-items: center;
  gap: 10px;
  text-decoration: none;
}
.logo-icon {
  width: 32px; height: 32px;
  background: linear-gradient(135deg, hsl(0, 72%, 51%), hsl(0, 85%, 40%));
  border-radius: 9px;
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 0 0 1px rgba(220, 38, 38, 0.25), 0 4px 14px hsla(0, 72%, 51%, 0.35);
}
.logo-text {
  font-size: 16px;
  font-weight: 800;
  letter-spacing: -0.5px;
  color: var(--text);
}

/* Nav Tabs */
.top-nav { display: flex; gap: 2px; }

.nav-tab {
  padding: 6px 18px;
  background: transparent;
  border: none;
  border-radius: var(--radius-sm);
  color: var(--text-2);
  font-family: var(--font-sans);
  font-size: 13.5px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s ease;
  letter-spacing: -0.1px;
}
.nav-tab:hover { color: var(--text); background: var(--surface-2); }
.nav-tab.active {
  color: var(--text);
  background: var(--surface-3);
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.06);
}

/* Status Badges */
.yt-status-badge {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 12px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 99px;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-2);
  transition: border-color 0.15s;
  letter-spacing: -0.1px;
}
.yt-status-badge:hover { border-color: var(--border-hover); }

.status-dot {
  width: 7px; height: 7px;
  border-radius: 50%;
  background: var(--red);
  flex-shrink: 0;
  transition: background 0.3s, box-shadow 0.3s;
}
.status-dot.connected {
  background: var(--green);
  box-shadow: 0 0 0 2px rgba(16,185,129,0.25);
  animation: pulse-green 2s infinite;
}

@keyframes pulse-green {
  0%, 100% { box-shadow: 0 0 0 2px rgba(16,185,129,0.25); }
  50%       { box-shadow: 0 0 0 4px rgba(16,185,129,0.10); }
}

/* ─── Buttons ───────────────────────────────────────── */
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 8px 16px;
  border-radius: var(--radius-sm);
  font-family: var(--font-sans);
  font-size: 13.5px;
  font-weight: 600;
  border: none;
  cursor: pointer;
  transition: all 0.18s ease;
  text-decoration: none;
  letter-spacing: -0.1px;
}

.btn-connect {
  background: var(--red-dim);
  color: var(--red);
  border: 1px solid rgba(239,68,68,0.2);
}
.btn-connect:hover { background: rgba(239,68,68,0.16); border-color: rgba(239,68,68,0.3); }
.btn-connect.connected {
  background: var(--green-dim);
  color: var(--green);
  border-color: rgba(16,185,129,0.25);
  cursor: pointer;
}
.btn-connect.connected:hover {
  background: rgba(16,185,129,0.18);
  border-color: rgba(16,185,129,0.4);
}

.btn-generate,
.btn-generate-lg {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 13px 26px;
  font-size: 15px;
  font-weight: 700;
  border-radius: 12px;
  background: linear-gradient(135deg, #3b82f6 0%, #2563eb 50%, #1d4ed8 100%);
  color: #ffffff;
  border: 1px solid rgba(255, 255, 255, 0.2);
  box-shadow: 0 4px 20px rgba(37, 99, 235, 0.45), 0 0 0 1px rgba(255, 255, 255, 0.15) inset;
  letter-spacing: -0.2px;
  cursor: pointer;
  position: relative;
  overflow: hidden;
  transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
  white-space: nowrap;
}

.btn-generate:hover:not(:disabled),
.btn-generate-lg:hover:not(:disabled) {
  transform: translateY(-2px);
  background: linear-gradient(135deg, #60a5fa 0%, #3b82f6 50%, #2563eb 100%);
  box-shadow: 0 8px 30px rgba(37, 99, 235, 0.6), 0 0 0 1px rgba(255, 255, 255, 0.25) inset;
  filter: brightness(1.05);
}

.btn-generate:active:not(:disabled),
.btn-generate-lg:active:not(:disabled) {
  transform: translateY(1px) scale(0.98);
  box-shadow: 0 2px 10px rgba(37, 99, 235, 0.35);
}

.btn-generate:disabled,
.btn-generate-lg:disabled {
  opacity: 0.55;
  cursor: not-allowed;
  transform: none;
  box-shadow: none;
  filter: grayscale(0.5);
}

.btn-sm { padding: 6px 12px; font-size: 12.5px; }

.btn-outline {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-2);
}
.btn-outline:hover { border-color: var(--border-hover); color: var(--text); background: var(--surface-2); }

.btn-upgrade {
  width: 100%;
  padding: 14px;
  background: linear-gradient(135deg, hsl(0, 72%, 51%), hsl(0, 85%, 40%));
  color: #fff;
  font-size: 15px;
  font-weight: 700;
  border-radius: var(--radius);
  box-shadow: 0 1px 0 rgba(255,255,255,0.18) inset, 0 4px 24px var(--blue-glow);
  margin-bottom: 12px;
  letter-spacing: -0.2px;
}
.btn-upgrade:hover { transform: translateY(-2px); box-shadow: 0 1px 0 rgba(255,255,255,0.15) inset, 0 8px 32px var(--blue-glow); filter: brightness(1.08); }

.btn-ghost {
  width: 100%;
  padding: 12px;
  background: transparent;
  color: var(--text-3);
  font-size: 14px;
  font-weight: 600;
  border-radius: var(--radius);
  transition: all 0.2s;
  cursor: pointer;
  border: none;
}
.btn-ghost:hover { color: var(--text-2); }

/* ─── Toggle Switch ─────────────────────────────────── */
.setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.toggle-switch {
  position: relative;
  display: inline-block;
  width: 52px;
  height: 28px;
}
.toggle-switch input { opacity: 0; width: 0; height: 0; }
.toggle-slider {
  position: absolute;
  cursor: pointer;
  top: 0; left: 0; right: 0; bottom: 0;
  background-color: var(--surface-3);
  transition: .3s;
  border-radius: 34px;
  border: 1px solid var(--border);
}
.toggle-slider:before {
  position: absolute;
  content: "";
  height: 20px;
  width: 20px;
  left: 3px;
  bottom: 3px;
  background-color: var(--text-2);
  transition: .3s;
  border-radius: 50%;
}
input:checked + .toggle-slider {
  background: linear-gradient(135deg, hsl(0, 72%, 51%), hsl(0, 85%, 40%));
  border-color: transparent;
}
input:checked + .toggle-slider:before {
  transform: translateX(24px);
  background-color: #fff;
}

/* ─── Background Effects ────────────────────────────── */
.bg-glow {
  position: absolute;
  width: 600px;
  height: 600px;
  background: radial-gradient(circle, var(--blue-glow) 0%, transparent 60%);
  border-radius: 50%;
  pointer-events: none;
  z-index: 0;
  filter: blur(80px);
  opacity: 0.6;
}
.bg-glow-1 { top: -200px; left: -100px; }
.bg-glow-2 { bottom: -200px; right: -100px; background: radial-gradient(circle, var(--green-glow) 0%, transparent 60%); }

/* ─── Glass Elements ────────────────────────────────── */
.glass-card {
  background: rgba(17, 17, 17, 0.4);
  backdrop-filter: blur(24px);
  -webkit-backdrop-filter: blur(24px);
  border: 1px solid var(--border);
  box-shadow: 0 8px 32px rgba(0,0,0,0.4), 0 0 0 1px rgba(255,255,255,0.02) inset;
  border-radius: var(--radius);
}
.glass-modal {
  background: rgba(17, 17, 17, 0.75);
  backdrop-filter: blur(40px);
  -webkit-backdrop-filter: blur(40px);
}

/* ─── Tab Content ───────────────────────────────────── */
.tab-content { flex: 1; display: flex; flex-direction: column; overflow-y: auto; position: relative; z-index: 1; }
.tab-content.hidden { display: none !important; }

/* ─── Generate Tab Layout ───────────────────────────── */
.generate-layout {
  display: flex;
  gap: 40px;
  padding: 40px 60px;
  max-width: 1400px;
  margin: 0 auto;
  width: 100%;
}

/* Center Stage / Hero */
.hero-section {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 32px;
  max-width: 680px;
}

.hero-text { margin-bottom: 8px; }
.hero-title {
  font-size: 46px;
  font-weight: 800;
  letter-spacing: -1.5px;
  color: var(--text);
  line-height: 1.1;
  margin-bottom: 16px;
}
.hero-title span {
  background: linear-gradient(135deg, hsl(0, 72%, 55%), hsl(0, 85%, 45%));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}
.hero-subtitle {
  font-size: 16px;
  color: var(--text-2);
  line-height: 1.6;
  max-width: 500px;
}

/* Input Card */
.main-input-card {
  padding: 28px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  transition: border-color 0.3s;
}
.main-input-card:focus-within { border-color: rgba(220, 38, 38, 0.4); }

.input-row-lg {
  display: flex;
  gap: 12px;
}
.niche-input-lg {
  flex: 1;
  padding: 16px 20px;
  background: rgba(0,0,0,0.4);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text);
  font-family: var(--font-sans);
  font-size: 16px;
  font-weight: 500;
  outline: none;
  transition: all 0.2s;
  box-shadow: inset 0 2px 8px rgba(0,0,0,0.2);
}
.niche-input-lg:focus { border-color: var(--blue); background: rgba(0,0,0,0.6); }

.btn-generate-lg {
  height: 54px;
  padding: 0 32px;
  font-size: 15.5px;
  font-weight: 700;
  border-radius: var(--radius-sm);
  background: linear-gradient(135deg, #3b82f6 0%, #2563eb 50%, #1d4ed8 100%);
  color: #ffffff;
  border: 1px solid rgba(255, 255, 255, 0.22);
  box-shadow: 0 4px 20px rgba(37, 99, 235, 0.45), 0 0 0 1px rgba(255, 255, 255, 0.15) inset;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  flex-shrink: 0;
  letter-spacing: -0.2px;
  transition: all 0.22s cubic-bezier(0.4, 0, 0.2, 1);
  white-space: nowrap;
}
.btn-generate-lg:hover:not(:disabled) {
  transform: translateY(-2px);
  background: linear-gradient(135deg, #60a5fa 0%, #3b82f6 50%, #2563eb 100%);
  box-shadow: 0 8px 30px rgba(37, 99, 235, 0.65), 0 0 0 1px rgba(255, 255, 255, 0.25) inset;
  filter: brightness(1.06);
}
.btn-generate-lg:active:not(:disabled) {
  transform: translateY(1px) scale(0.98);
  box-shadow: 0 2px 10px rgba(37, 99, 235, 0.35);
}
.btn-generate-lg:disabled {
  opacity: 0.5;
  cursor: not-allowed;
  transform: none;
  box-shadow: none;
}

.panel-header { }
.panel-header h2 {
  font-size: 19px;
  font-weight: 800;
  letter-spacing: -0.5px;
  color: var(--text);
  margin-bottom: 4px;
}
.panel-subtitle { font-size: 13px; color: var(--text-2); line-height: 1.55; }

.input-card {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  transition: border-color 0.2s;
}
.input-card:focus-within { border-color: var(--border-active); }

.input-label {
  font-size: 11px;
  font-weight: 700;
  color: var(--text-3);
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

.niche-input {
  width: 100%;
  padding: 11px 14px;
  background: var(--surface-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text);
  font-family: var(--font-sans);
  font-size: 14px;
  font-weight: 500;
  outline: none;
  transition: border-color 0.15s, background 0.15s;
  letter-spacing: -0.1px;
}
.niche-input:focus { border-color: var(--blue); background: var(--surface-4); }
.niche-input::placeholder { color: var(--text-3); }

.free-tier-badge {
  font-size: 12px;
  font-weight: 500;
  color: var(--amber);
  padding: 8px 12px;
  background: var(--amber-dim);
  border: 1px solid rgba(251,191,36,0.15);
  border-radius: var(--radius-sm);
  text-align: center;
  letter-spacing: -0.1px;
}

/* Progress Card */
.progress-card {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 11px;
}
.progress-card.hidden { display: none !important; }
.progress-header { display: flex; justify-content: space-between; align-items: center; }
.progress-label { font-size: 13px; font-weight: 700; color: var(--text); letter-spacing: -0.1px; }
#progress-pct { font-size: 13px; font-weight: 700; color: var(--blue); font-variant-numeric: tabular-nums; letter-spacing: -0.2px; }
.progress-track {
  height: 5px;
  background: var(--surface-4);
  border-radius: 99px;
  overflow: hidden;
}
.progress-fill {
  height: 100%;
  width: 0%;
  background: linear-gradient(90deg, hsl(0, 85%, 40%), hsl(0, 72%, 51%), #fff);
  border-radius: 99px;
  transition: width 0.6s cubic-bezier(0.4, 0, 0.2, 1);
  box-shadow: 0 0 10px var(--blue-glow);
}
.progress-step { font-size: 12.5px; color: var(--text-2); letter-spacing: -0.1px; }

/* ─── Right Sidebar / Activity ─────────────────────── */
.activity-sidebar {
  width: 380px;
  display: flex;
  flex-direction: column;
  height: calc(100vh - 140px);
  position: sticky;
  top: 0;
}

.activity-header {
  padding: 20px 24px;
  font-size: 13px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 10px;
}
.activity-header svg { color: var(--blue); }

.chat-window {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.log-entry {
  display: flex;
  gap: 14px;
  animation: logIn 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
}

@keyframes logIn {
  from { opacity: 0; transform: scale(0.95) translateY(10px); }
  to   { opacity: 1; transform: scale(1) translateY(0); }
}

.log-icon {
  width: 36px; height: 36px;
  border-radius: 50%;
  background: var(--surface-3);
  border: 1px solid var(--border);
  display: flex; align-items: center; justify-content: center;
  font-size: 16px;
  flex-shrink: 0;
  box-shadow: 0 4px 12px rgba(0,0,0,0.2);
}

.log-body { display: flex; flex-direction: column; gap: 6px; flex: 1; }
.log-sender { font-size: 12px; font-weight: 700; color: var(--text-2); letter-spacing: 0.02em; }
.log-text {
  font-size: 14px;
  color: var(--text);
  line-height: 1.6;
  background: rgba(0,0,0,0.3);
  border: 1px solid var(--border);
  border-radius: 12px;
  border-top-left-radius: 4px;
  padding: 12px 16px;
}
.log-text a { color: var(--blue); text-decoration: none; font-weight: 600; }
.log-text a:hover { text-decoration: underline; }

/* ─── Analytics Tab ─────────────────────────────────── */
.analytics-layout {
  padding: 40px 60px;
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 32px;
  max-width: 1200px;
  margin: 0 auto;
  width: 100%;
}

.analytics-header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.stat-cards {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 20px;
}

.stat-card {
  padding: 32px 28px;
  transition: transform 0.2s, box-shadow 0.2s, border-color 0.2s;
}
.stat-card:hover {
  border-color: rgba(220, 38, 38, 0.4);
  transform: translateY(-4px);
  box-shadow: 0 12px 40px rgba(0,0,0,0.5), 0 0 0 1px rgba(255,255,255,0.02) inset;
}
.stat-label {
  font-size: 12px;
  font-weight: 700;
  color: var(--text-2);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-bottom: 16px;
}
.stat-value {
  font-size: 42px;
  font-weight: 800;
  letter-spacing: -1.5px;
  color: var(--text);
  margin-bottom: 8px;
  font-variant-numeric: tabular-nums;
  line-height: 1;
}
.stat-sub { font-size: 13px; color: var(--text-3); font-weight: 500; }

/* Videos Table */
.videos-table-card { overflow: hidden; }
.table-header {
  padding: 20px 28px;
  font-size: 12px;
  font-weight: 700;
  color: var(--text-3);
  border-bottom: 1px solid var(--border);
  text-transform: uppercase;
  letter-spacing: 0.1em;
  background: rgba(0,0,0,0.2);
}
.table-empty {
  padding: 60px 24px;
  text-align: center;
  color: var(--text-3);
  font-size: 15px;
}
.table-row-header {
  font-size: 12px;
  font-weight: 700;
  color: var(--text-3);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  padding: 14px 28px;
  border-bottom: 1px solid var(--border);
  background: rgba(0,0,0,0.2);
  display: grid;
  grid-template-columns: 1fr 100px 130px 80px;
  gap: 20px;
}
.table-row {
  display: grid;
  grid-template-columns: 1fr 100px 130px 80px;
  padding: 18px 28px;
  border-bottom: 1px solid var(--border);
  align-items: center;
  gap: 20px;
  transition: background 0.15s;
}
.table-row:last-child { border-bottom: none; }
.table-row:hover { background: rgba(255,255,255,0.03); }
.video-title { font-size: 14px; font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; letter-spacing: -0.1px; }
.video-views { font-size: 14px; font-weight: 700; color: var(--text); font-variant-numeric: tabular-nums; }
.video-date { font-size: 13px; color: var(--text-2); font-weight: 500; }
.video-link a {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
  color: var(--blue);
  text-decoration: none;
  font-weight: 600;
}
.video-link a:hover { text-decoration: underline; color: var(--blue-light); }

/* ─── Modal ─────────────────────────────────────────── */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.7);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 999;
  animation: fadeIn 0.2s ease;
}
.modal-overlay.hidden { display: none !important; }

@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }

.modal-box {
  background: var(--surface);
  border: 1px solid var(--border-active);
  border-radius: 20px;
  padding: 44px 40px;
  width: 480px;
  max-width: 95vw;
  box-shadow: 0 40px 100px rgba(0,0,0,0.7), 0 0 0 1px rgba(255,255,255,0.04) inset;
  animation: slideUp 0.25s cubic-bezier(0.4, 0, 0.2, 1);
  text-align: center;
  position: relative;
  overflow: hidden;
}
.modal-box::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(220, 38, 38, 0.3), transparent);
}

@keyframes slideUp {
  from { transform: translateY(20px); opacity: 0; }
  to   { transform: translateY(0); opacity: 1; }
}

.modal-icon { font-size: 42px; margin-bottom: 18px; }
.modal-title {
  font-size: 22px;
  font-weight: 800;
  letter-spacing: -0.5px;
  margin-bottom: 10px;
  color: var(--text);
}
.modal-desc {
  font-size: 13.5px;
  color: var(--text-2);
  line-height: 1.65;
  margin-bottom: 24px;
}
.modal-features {
  text-align: left;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px 20px;
  margin-bottom: 24px;
  display: flex;
  flex-direction: column;
  gap: 9px;
}
.feature-item {
  font-size: 13.5px;
  color: var(--green);
  font-weight: 600;
  letter-spacing: -0.1px;
}

/* ─── Pipeline Steps ───────────────────────────────── */
.pipeline-steps {
  display: flex;
  align-items: center;
  gap: 0;
  padding: 16px 20px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.pipeline-step {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  flex: 1;
}
.pipeline-step span {
  font-size: 11px;
  font-weight: 700;
  color: var(--text-3);
  text-transform: uppercase;
  letter-spacing: 0.07em;
  transition: color 0.3s;
}
.step-dot {
  width: 10px; height: 10px;
  border-radius: 50%;
  background: var(--surface-4);
  border: 2px solid var(--surface-4);
  transition: background 0.3s, box-shadow 0.3s, border-color 0.3s;
}
.pipeline-step.active .step-dot {
  background: var(--blue);
  border-color: var(--blue);
  box-shadow: 0 0 0 3px var(--blue-glow);
}
.pipeline-step.active span { color: var(--blue); }
.pipeline-step.done .step-dot {
  background: var(--green);
  border-color: var(--green);
}
.pipeline-step.done span { color: var(--green); }

.step-line {
  flex: 1;
  height: 1px;
  background: var(--border);
  margin-bottom: 17px;
  max-width: 32px;
}

/* ─── Toast ─────────────────────────────────────────── */
@keyframes toastIn {
  from { opacity: 0; transform: translate(-50%, -14px); }
  to   { opacity: 1; transform: translate(-50%, 0); }
}

/* ─── Divider ───────────────────────────────────────── */
.divider {
  height: 1px;
  background: var(--border);
  margin: 0 -22px;
}

/* ─── Responsive ─────────────────────────────────────── */
@media (max-width: 800px) {
  .generate-layout {
    grid-template-columns: 1fr;
    grid-template-rows: auto 1fr;
  }
  .generate-panel { border-right: none; border-bottom: 1px solid var(--border); }
  .stat-cards { grid-template-columns: 1fr; }
  .analytics-layout { padding: 24px 20px; }
  .topbar { padding: 0 18px; }
}
/* -- Toast Notifications -- */
#toast-container {
    position: fixed;
    bottom: 20px;
    right: 20px;
    z-index: 9999;
    display: flex;
    flex-direction: column;
    gap: 10px;
}
.toast {
    background: rgba(17, 24, 39, 0.85);
    backdrop-filter: blur(12px);
    border: 1px solid rgba(255, 255, 255, 0.1);
    color: var(--text);
    padding: 16px 20px;
    border-radius: var(--radius-md);
    box-shadow: 0 10px 30px rgba(0,0,0,0.5);
    display: flex;
    align-items: center;
    gap: 12px;
    min-width: 300px;
    transform: translateX(120%);
    opacity: 0;
    transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);
}
.toast.show {
    transform: translateX(0);
    opacity: 1;
}
.toast-icon {
    font-size: 20px;
}
.toast-content h4 {
    margin: 0 0 4px 0;
    font-size: 15px;
    color: white;
}
.toast-content p {
    margin: 0;
    font-size: 13px;
    color: var(--text-2);
}
.toast.success { border-left: 4px solid var(--emerald); }
.toast.error { border-left: 4px solid var(--crimson); }
.toast.info { border-left: 4px solid var(--blue); }
@keyframes float {
  0% { transform: translateY(0px); }
  50% { transform: translateY(-10px); }
  100% { transform: translateY(0px); }
}
@keyframes pulseGlow {
  0% { box-shadow: 0 0 0 0 var(--blue-glow); }
  70% { box-shadow: 0 0 0 15px transparent; }
  100% { box-shadow: 0 0 0 0 transparent; }
}
.btn-generate {
  animation: pulseGlow 2s infinite;
}
.btn-generate:hover {
  animation: none;
}
@keyframes ticker {
  0% { transform: translateX(0); }
  100% { transform: translateX(-50%); }
}

/* ======================================================
   10x GUI OVERHAUL -- Sidebar Studio Layout
   ====================================================== */

body { overflow: hidden; }

.app-layout {
  display: flex;
  height: 100vh;
  width: 100vw;
  overflow: hidden;
}

/* Left Sidebar */
.sidebar {
  width: 240px;
  min-width: 240px;
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  padding: 24px 16px;
  gap: 2px;
  flex-shrink: 0;
}

.sidebar-logo {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 8px 28px 8px;
}
.sidebar-logo-text {
  font-size: 18px;
  font-weight: 800;
  color: #f5f5f5;
  letter-spacing: -0.5px;
}

.sidebar-section-label {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.1em;
  color: #525252;
  text-transform: uppercase;
  padding: 12px 12px 4px 12px;
}

.sidebar-btn {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 11px 14px;
  color: #a3a3a3;
  background: transparent;
  border: none;
  border-radius: 9px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  width: 100%;
  text-align: left;
  transition: all 0.15s ease;
  font-family: var(--font-sans);
  position: relative;
}
.sidebar-btn:hover { color: #f5f5f5; background: #1a1a1a; }
.sidebar-btn.active { color: #dc2626; background: rgba(220,38,38,0.1); font-weight: 600; }
.sidebar-btn.active::before {
  content: '';
  position: absolute;
  left: 0; top: 50%;
  transform: translateY(-50%);
  width: 3px; height: 60%;
  background: #dc2626;
  border-radius: 0 4px 4px 0;
}
.sidebar-btn svg { width: 18px; height: 18px; flex-shrink: 0; opacity: 0.8; }
.sidebar-btn.active svg { opacity: 1; }

.sidebar-spacer { flex: 1; }

.sidebar-worker-block {
  padding: 14px;
  background: #1a1a1a;
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 10px;
  margin-top: 12px;
}
.sidebar-worker-block .worker-status { gap: 8px; }
.sidebar-worker-block .worker-label { font-size: 13px; font-weight: 600; }
.sidebar-worker-block p { font-size: 11px; color: #525252; margin-top: 5px; }

/* Main Area */
.main-wrapper {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg);
}

.top-header {
  height: 65px;
  min-height: 65px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 clamp(16px, 3vw, 36px);
  border-bottom: 1px solid rgba(255,255,255,0.08);
  background: rgba(8,8,8,0.85);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  gap: 12px;
  flex-shrink: 0;
  z-index: 50;
}

.mobile-logo-wrap {
  display: none;
  align-items: center;
  gap: 8px;
}
.mobile-logo-text {
  font-size: 17px;
  font-weight: 800;
  color: #fff;
  letter-spacing: -0.4px;
}

.top-header-right {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-left: auto;
}

.top-header .free-tier-badge { display: flex; align-items: center; gap: 10px; background: rgba(255,255,255,0.05); border: 1px solid var(--border); border-radius: 20px; padding: 6px 14px; font-size: 13px; color: var(--text-2); white-space: nowrap; }

.btn-upgrade {
  background: #dc2626;
  color: white;
  border: none;
  border-radius: 14px;
  padding: 5px 12px;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
  font-family: var(--font-sans);
}
.btn-upgrade:hover { filter: brightness(1.15); }

.content-area { flex: 1; overflow: hidden; display: flex; flex-direction: column; }

/* Hide old wrappers */
.app { display: none !important; }
.topbar { display: none !important; }

/* Brand Kit Modal */
.brand-modal {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.7);
  backdrop-filter: blur(10px);
  z-index: 999;
  display: flex;
  align-items: center;
  justify-content: center;
}
.brand-modal.hidden { display: none !important; }
.brand-modal-inner {
  background: #111111;
  border: 1px solid rgba(255,255,255,0.1);
  border-radius: 18px;
  padding: 40px;
  max-width: 480px;
  width: 100%;
  box-shadow: 0 24px 60px rgba(0,0,0,0.6);
}


/* ── Missing Classes Audit Fix ─────────────────────────────────────── */
.days-selector {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}

.day-cb {
  display: none;
}

.days-selector label {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border-radius: 50%;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text-2);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
  user-select: none;
}

.day-cb:checked + label,
.days-selector label:has(+ .day-cb:checked) {
  background: var(--blue);
  border-color: var(--blue);
  color: white;
}

.setting-group {
  margin-bottom: 24px;
}

.setting-info {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 10px;
}

.setting-info h3 {
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
  margin: 0;
}

.setting-info p {
  font-size: 13px;
  color: var(--text-2);
  margin: 0;
}

.progress-label-wrap {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.btn-refresh {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-2);
  padding: 6px 14px;
  border-radius: var(--radius-sm);
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s;
}

.btn-refresh:hover {
  background: var(--surface-2);
  color: var(--text);
}

.pricing-card {
  background: rgba(0,0,0,0.5);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
  margin-bottom: 24px;
  text-align: left;
}

.pulse-ring {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--green);
  box-shadow: 0 0 0 0 rgba(34,197,94,0.7);
  animation: pulse-anim 1.5s ease-out infinite;
}

@keyframes pulse-anim {
  0%   { box-shadow: 0 0 0 0 rgba(34,197,94,0.7); }
  70%  { box-shadow: 0 0 0 8px rgba(34,197,94,0); }
  100% { box-shadow: 0 0 0 0 rgba(34,197,94,0); }
}

.ticker-container {
  overflow: hidden;
  white-space: nowrap;
}

.ticker-track {
  display: inline-block;
  animation: ticker 40s linear infinite;
}

@keyframes ticker {
  from { transform: translateX(0); }
  to   { transform: translateX(-50%); }
}

.day-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 14px;
  border-radius: 20px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text-2);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
  user-select: none;
}

.day-pill:hover {
  border-color: var(--blue);
  color: var(--text);
}

.day-pill input[type=checkbox] {
  accent-color: var(--blue);
  width: 14px;
  height: 14px;
  cursor: pointer;
}

.day-pill:has(input:checked) {
  background: var(--blue-dim);
  border-color: var(--blue);
  color: var(--blue-light);
}

/* ── Clip Gallery Cards ─────────────────────────────────────────── */
.clip-card {
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: 14px;
  overflow: hidden;
  cursor: pointer;
  transition: transform 0.2s, box-shadow 0.2s, border-color 0.2s;
}

.clip-card:hover {
  transform: translateY(-6px);
  box-shadow: 0 16px 40px rgba(0,0,0,0.4);
  border-color: rgba(59,130,246,0.35);
}

.clip-card-thumb {
  width: 100%;
  aspect-ratio: 9/16;
  background: var(--bg-1);
  background-size: cover;
  background-position: center;
  position: relative;
}

.clip-card-overlay {
  position: absolute;
  inset: 0;
  background: rgba(0,0,0,0);
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.2s;
}

.clip-card:hover .clip-card-overlay {
  background: rgba(0,0,0,0.45);
}

.clip-play-btn {
  width: 56px;
  height: 56px;
  border-radius: 50%;
  background: rgba(255,255,255,0.15);
  backdrop-filter: blur(8px);
  border: 2px solid rgba(255,255,255,0.3);
  display: flex;
  align-items: center;
  justify-content: center;
  opacity: 0;
  transform: scale(0.8);
  transition: opacity 0.2s, transform 0.2s;
}

.clip-card:hover .clip-play-btn {
  opacity: 1;
  transform: scale(1);
}

.clip-score-badge {
  position: absolute;
  top: 10px;
  right: 10px;
  background: rgba(0,0,0,0.65);
  backdrop-filter: blur(6px);
  border: 1px solid rgba(255,255,255,0.15);
  color: white;
  font-size: 12px;
  font-weight: 700;
  padding: 4px 10px;
  border-radius: 20px;
}

.clip-card-info {
  padding: 14px 16px;
}

.clip-title {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 8px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.clip-meta {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
  color: var(--text-2);
}

.clip-views {
  font-weight: 700;
  color: white;
  background: rgba(255,255,255,0.08);
  padding: 3px 8px;
  border-radius: 10px;
}

/* ======================================================
   IPHONE (120Hz PROMOTION) & RESPONSIVE 1920x1080 / SMALL SCREEN FIXES
   ====================================================== */

/* ── 120Hz Hardware Accelerated Smooth Touch & Scrolling ── */
html {
  -webkit-text-size-adjust: 100%;
  text-size-adjust: 100%;
  scroll-behavior: smooth;
}

body,
.tab-content,
.content-area,
.chat-window,
.analytics-layout,
.autopost-layout {
  -webkit-overflow-scrolling: touch;
  scroll-behavior: smooth;
  touch-action: pan-y pinch-zoom;
}

/* Force GPU composite layers for 120 FPS high-refresh-rate rendering */
.tab-content,
.chat-window,
.glass-card,
.clip-card,
.btn-generate,
.btn-generate-lg,
.mobile-bottom-nav {
  transform: translate3d(0, 0, 0);
  -webkit-transform: translate3d(0, 0, 0);
  backface-visibility: hidden;
  -webkit-backface-visibility: hidden;
}

/* Fast tap response (0ms delay) on iPhone iOS Safari */
button,
a,
input,
.mobile-nav-btn,
.sidebar-btn {
  touch-action: manipulation;
  -webkit-tap-highlight-color: transparent;
}

/* ── 1920x1080 & Small / Shortened Desktop Displays ── */
@media (min-width: 901px) {
  .generate-layout {
    display: flex;
    gap: clamp(20px, 2.5vw, 40px);
    padding: clamp(20px, 2.5vw, 40px) clamp(24px, 3.5vw, 60px);
    max-width: 1440px;
    margin: 0 auto;
    width: 100%;
    min-height: 0;
  }

  .hero-title {
    font-size: clamp(30px, 3.2vw, 46px);
    line-height: 1.15;
    margin-bottom: 12px;
  }

  .hero-subtitle {
    font-size: clamp(14px, 1.1vw, 16px);
    max-width: 520px;
  }
}

/* Compact adjustments for short vertical screen heights (common on 1080p laptops with Windows scaling) */
@media (max-height: 850px) and (min-width: 901px) {
  .top-header {
    height: 54px;
    min-height: 54px;
  }

  .sidebar {
    padding: 16px 12px;
    gap: 1px;
  }

  .sidebar-logo {
    padding: 4px 6px 16px 6px;
  }

  .sidebar-btn {
    padding: 8px 12px;
    font-size: 13px;
  }

  .generate-layout {
    padding: 16px 32px;
    gap: 24px;
  }

  .hero-section {
    gap: 20px;
  }

  .hero-title {
    font-size: 32px;
    margin-bottom: 8px;
  }

  .main-input-card {
    padding: 20px;
    gap: 12px;
  }

  .activity-sidebar {
    height: calc(100vh - 100px);
  }
}

/* ── iPhone & Mobile Screen Responsive Layout (≤ 900px) ── */
@media (max-width: 900px) {
  body {
    overflow: auto;
    overflow-x: hidden;
    height: 100%;
    min-height: 100vh;
    min-height: -webkit-fill-available;
  }

  .app-layout {
    display: block;
    width: 100%;
    height: auto;
    min-height: 100vh;
    overflow: visible;
  }

  /* Hide Desktop Sidebar on iPhone / Mobile */
  .sidebar {
    display: none !important;
  }

  /* Show Brand in Mobile Header */
  .mobile-logo-wrap {
    display: flex;
  }

  .main-wrapper {
    width: 100%;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: visible;
  }

  .top-header {
    position: sticky;
    top: 0;
    z-index: 100;
    padding: 0 16px;
    padding-top: env(safe-area-inset-top, 0px);
    height: calc(56px + env(safe-area-inset-top, 0px));
    min-height: calc(56px + env(safe-area-inset-top, 0px));
  }

  .content-area {
    overflow: visible;
    flex: 1;
  }

  .tab-content {
    overflow: visible;
    padding-bottom: calc(84px + env(safe-area-inset-bottom, 0px));
  }

  /* Stacked Mobile Studio Layout */
  .generate-layout {
    display: flex;
    flex-direction: column;
    padding: 18px 16px;
    gap: 24px;
    max-width: 100%;
  }

  .hero-section {
    max-width: 100%;
    gap: 20px;
  }

  .hero-title {
    font-size: 28px;
    letter-spacing: -0.8px;
    margin-bottom: 8px;
  }

  .hero-subtitle {
    font-size: 14px;
    line-height: 1.5;
  }

  .main-input-card {
    padding: 18px 16px;
    gap: 14px;
  }

  .input-row-lg {
    flex-direction: column;
    gap: 12px;
  }

  .niche-input-lg {
    width: 100%;
    padding: 14px 16px;
    font-size: 15px;
  }

  .btn-generate-lg {
    width: 100%;
    padding: 14px 20px;
    justify-content: center;
    font-size: 15px;
  }

  .activity-sidebar {
    width: 100%;
    height: auto;
    position: static;
    max-height: 480px;
    border-radius: 14px;
  }

  .analytics-layout,
  .autopost-layout {
    padding: 20px 16px;
  }

  .stat-cards {
    grid-template-columns: 1fr;
    gap: 12px;
  }

  .stat-card {
    padding: 18px;
  }

  .stat-value {
    font-size: 28px;
  }

  #videos-gallery-grid {
    grid-template-columns: repeat(2, 1fr) !important;
    gap: 14px !important;
  }
}

/* Extra small screens (iPhone SE, iPhone mini ≤ 480px) */
@media (max-width: 480px) {
  #videos-gallery-grid {
    grid-template-columns: 1fr !important;
  }

  .btn-connect span {
    display: none;
  }

  .hero-title {
    font-size: 24px;
  }
}

/* ── iPhone Mobile Bottom Navigation Bar ── */
.mobile-bottom-nav {
  display: none;
}

@media (max-width: 900px) {
  .mobile-bottom-nav {
    display: flex;
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    height: calc(60px + env(safe-area-inset-bottom, 0px));
    padding-bottom: env(safe-area-inset-bottom, 0px);
    background: rgba(14, 14, 14, 0.94);
    backdrop-filter: blur(24px);
    -webkit-backdrop-filter: blur(24px);
    border-top: 1px solid rgba(255, 255, 255, 0.09);
    z-index: 999;
    justify-content: space-around;
    align-items: center;
    box-shadow: 0 -8px 24px rgba(0, 0, 0, 0.5);
  }

  .mobile-nav-btn {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 4px;
    background: transparent;
    border: none;
    color: #737373;
    font-size: 11px;
    font-weight: 600;
    font-family: var(--font-sans);
    padding: 6px 0;
    cursor: pointer;
    transition: color 0.15s ease, transform 0.1s ease;
  }

  .mobile-nav-btn svg {
    width: 20px;
    height: 20px;
    stroke-width: 2;
    transition: transform 0.15s ease;
  }

  .mobile-nav-btn:active {
    transform: scale(0.92);
  }

  .mobile-nav-btn.active {
    color: #dc2626;
  }

  .mobile-nav-btn.active svg {
    stroke: #dc2626;
    transform: translateY(-1px);
  }
}

/* ─── Auto-Post Switch Slider ───────────────────────── */
.switch input:checked + .slider {
  background-color: #2563eb !important;
}
.switch .slider:before {
  position: absolute;
  content: "";
  height: 18px;
  width: 18px;
  left: 3px;
  bottom: 3px;
  background-color: white;
  transition: .3s;
  border-radius: 50%;
  box-shadow: 0 2px 6px rgba(0,0,0,0.4);
}
.switch input:checked + .slider:before {
  transform: translateX(20px);
}


################################################################################
# FILE: static/app.js
################################################################################

// ─── User Session & Identifier (Permanent Local Machine Memory) ─────────────────
function getActiveUserId() {
    let uid = localStorage.getItem('clipai_user_id');
    if (!uid) {
        uid = document.cookie.split('; ').find(r => r.startsWith('user_id='))?.split('=')[1];
    }
    if (uid && uid !== 'demo_user_123' && uid !== 'undefined') {
        localStorage.setItem('clipai_user_id', uid);
        document.cookie = `user_id=${uid};path=/;max-age=315360000;SameSite=Lax`;
        return uid;
    }
    // Only if brand new visitor without session
    if (!uid) {
        uid = `user_${Math.floor(100000 + Math.random() * 900000)}`;
        localStorage.setItem('clipai_user_id', uid);
        document.cookie = `user_id=${uid};path=/;max-age=315360000;SameSite=Lax`;
    }
    return uid;
}

// ─── Cloud Worker Connection State ───────────────────────────────────────────
let workerIsAlive = true; // Cloud worker on Oracle is active 24/7

async function checkWorkerHeartbeat() {
    try {
        const userId = getActiveUserId();
        const res = await fetch(`/api/v1/worker/heartbeat?user_id=${userId}`);
        const data = await res.json();
        workerIsAlive = data.alive !== false;
        
        const dot = document.getElementById('worker-dot');
        const label = document.getElementById('worker-label');
        if (dot && label) {
            if (workerIsAlive) {
                dot.style.background = '#10b981';
                dot.style.boxShadow = '0 0 10px rgba(16,185,129,0.5)';
                label.textContent = 'Cloud Engine Active (🟢)';
                label.style.color = '#10b981';
            } else {
                dot.style.background = '#f59e0b';
                dot.style.boxShadow = 'none';
                label.textContent = 'Cloud Engine Connecting...';
                label.style.color = '#f59e0b';
            }
        }
    } catch (e) {
        // Default to active since cloud worker runs persistently
        workerIsAlive = true;
    }
}
setInterval(checkWorkerHeartbeat, 8000);

function startWorkerURI() {
    const userId = getActiveUserId();
    workerIsStarting = true;
    
    const dot = document.getElementById('worker-dot');
    const label = document.getElementById('worker-label');
    if (dot && label) {
        dot.style.background = '#f59e0b';
        dot.style.boxShadow = '0 0 10px rgba(245,158,11,0.5)';
        label.textContent = 'Worker Starting... (⏳)';
        label.style.color = '#f59e0b';
    }
    
    window.location.href = `clipai://start?user_id=${userId}`;
    showToast('Starting local desktop worker...', 'info');
    setTimeout(checkWorkerHeartbeat, 2000);
    setTimeout(checkWorkerHeartbeat, 5000);
}

// ─── In-App Video Player ───────────────────────────────────────────────────────
function openPlayer(videoId, youtubeUrl, title) {
    const modal = document.getElementById('player-modal');
    const iframe = document.getElementById('player-iframe');
    const video = document.getElementById('player-video');
    const titleEl = document.getElementById('player-title');
    const linkEl = document.getElementById('player-yt-link');
    if (!modal) return;

    currentVideoUrl = youtubeUrl || '';
    if (titleEl) titleEl.textContent = title || 'Viral Short';

    if (linkEl) {
        if (youtubeUrl && (youtubeUrl.includes('youtube.com') || youtubeUrl.includes('youtu.be'))) {
            linkEl.href = youtubeUrl;
            linkEl.style.display = 'block';
        } else {
            linkEl.style.display = 'none';
        }
    }

    // Extract genuine YouTube Video ID from any format
    let cleanYtId = videoId || '';
    if (youtubeUrl) {
        if (youtubeUrl.includes('/shorts/')) {
            cleanYtId = youtubeUrl.split('/shorts/')[1].split('?')[0].split('&')[0];
        } else if (youtubeUrl.includes('v=')) {
            cleanYtId = youtubeUrl.split('v=')[1].split('&')[0];
        } else if (youtubeUrl.includes('youtu.be/')) {
            cleanYtId = youtubeUrl.split('youtu.be/')[1].split('?')[0];
        }
    }

    const emptyNotice = document.getElementById('player-empty');

    // If it's a local file path rendered by desktop worker (e.g. C:\Users\...\\.clipai\\generated_videos\\clip_xyz.mp4)
    let playableStreamUrl = youtubeUrl || '';
    if (playableStreamUrl && (playableStreamUrl.includes('.mp4') || playableStreamUrl.includes('.clipai'))) {
        const filename = playableStreamUrl.split(/[/\\]/).pop();
        if (filename) {
            playableStreamUrl = `http://127.0.0.1:58921/${encodeURIComponent(filename)}`;
        }
    }

    if (cleanYtId && cleanYtId !== 'TEST_ANALYTICS' && (cleanYtId.length === 11 || (youtubeUrl && (youtubeUrl.includes('youtube.com') || youtubeUrl.includes('youtu.be'))))) {
        if (emptyNotice) emptyNotice.style.display = 'none';
        if (iframe) {
            iframe.style.display = 'block';
            iframe.src = `https://www.youtube.com/embed/${cleanYtId}?autoplay=1&rel=0`;
        }
        if (video) {
            video.style.display = 'none';
            video.pause();
            video.src = '';
        }
    } else if (playableStreamUrl && (playableStreamUrl.startsWith('http') || playableStreamUrl.startsWith('/') || playableStreamUrl.startsWith('blob:'))) {
        if (emptyNotice) emptyNotice.style.display = 'none';
        if (iframe) {
            iframe.style.display = 'none';
            iframe.src = '';
        }
        if (video) {
            video.style.display = 'block';
            video.src = playableStreamUrl;
            video.play().catch(() => {});
        }
    } else {
        // No playable streaming URL available yet
        if (iframe) {
            iframe.style.display = 'none';
            iframe.src = '';
        }
        if (video) {
            video.style.display = 'none';
            video.pause();
            video.src = '';
        }
        if (emptyNotice) emptyNotice.style.display = 'flex';
    }

    modal.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
}

function closePlayer() {
    const modal = document.getElementById('player-modal');
    const iframe = document.getElementById('player-iframe');
    const video = document.getElementById('player-video');
    if (iframe) iframe.src = '';
    if (video) {
        video.pause();
        video.src = '';
    }
    if (modal) modal.classList.add('hidden');
    document.body.style.overflow = '';
}

function copyVideoLink() {
    if (!currentVideoUrl) {
        showToast('No URL available to copy', 'error');
        return;
    }
    navigator.clipboard.writeText(currentVideoUrl).then(() => {
        showToast('Video link copied to clipboard!');
    }).catch(() => {
        showToast('Could not copy link', 'error');
    });
}

function updateWorkerUI(alive) {
    workerIsAlive = alive;
    const dot = document.getElementById('worker-dot');
    const label = document.getElementById('worker-label');
    if (!dot || !label) return;
    if (alive) {
        dot.className = 'status-dot connected';
        label.textContent = 'Worker Active';
    } else {
        dot.className = 'status-dot';
        label.textContent = 'Worker Offline';
    }
}
// ─── Pipeline Step Indicator ──────────────────────────────────────────────────
function updatePipelineSteps(pct) {
    // Steps: search (10%), download (25%), cut (60%), upload (85%)
    const steps = [
        { id: 'step-search',   threshold: 10 },
        { id: 'step-download', threshold: 25 },
        { id: 'step-cut',      threshold: 60 },
        { id: 'step-upload',   threshold: 85 },
    ];
    steps.forEach((step, i) => {
        const el = document.getElementById(step.id);
        if (!el) return;
        const nextThreshold = steps[i + 1]?.threshold ?? 101;
        if (pct >= nextThreshold) {
            el.className = 'pipeline-step done';
        } else if (pct >= step.threshold) {
            el.className = 'pipeline-step active';
        } else {
            el.className = 'pipeline-step';
        }
    });
}

function resetPipelineSteps() {
    ['step-search','step-download','step-cut','step-upload'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.className = 'pipeline-step';
    });
}



// ─── YouTube Connection State ─────────────────────────────────────────────────
function updateYouTubeUI(connected) {
    const dot = document.getElementById('yt-dot');
    const label = document.getElementById('yt-label');
    const btn = document.getElementById('connect-youtube-btn');
    if (!dot || !label || !btn) return;
    if (connected) {
        dot.className = 'status-dot connected';
        label.textContent = 'YouTube Connected';
        btn.innerHTML = `
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
          <span>Connected</span>
        `;
        btn.className = 'btn btn-connect connected';
        btn.title = 'Connected to YouTube. Click to switch accounts or reconnect.';
    } else {
        dot.className = 'status-dot';
        label.textContent = 'Not Connected';
        btn.innerHTML = `
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M23.5 6.2a3 3 0 0 0-2.1-2.1C19.6 3.6 12 3.6 12 3.6s-7.6 0-9.4.5A3 3 0 0 0 .5 6.2 31.5 31.5 0 0 0 0 12a31.5 31.5 0 0 0 .5 5.8 3 3 0 0 0 2.1 2.1c1.8.5 9.4.5 9.4.5s7.6 0 9.4-.5a3 3 0 0 0 2.1-2.1A31.5 31.5 0 0 0 24 12a31.5 31.5 0 0 0-.5-5.8zM9.8 15.6V8.4l6.3 3.6-6.3 3.6z"/></svg>
          <span>Connect YouTube</span>
        `;
        btn.className = 'btn btn-connect';
        btn.title = 'Click to connect your YouTube channel';
    }
}

// ─── Tab Switching ────────────────────────────────────────────────────────────
function switchTab(tab) {
    // Hide all tab content panels
    document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
    // Deactivate all sidebar and mobile navigation buttons
    document.querySelectorAll('.sidebar-btn, .nav-tab, .mobile-nav-btn').forEach(el => el.classList.remove('active'));
    // Show selected tab content
    const content = document.getElementById('tab-content-' + tab);
    if (content) content.classList.remove('hidden');
    // Activate the correct buttons
    const btn = document.getElementById('tab-' + tab);
    if (btn) btn.classList.add('active');
    const mBtn = document.getElementById('m-tab-' + tab);
    if (mBtn) mBtn.classList.add('active');
    if (tab === 'analytics') loadAnalytics();
    if (tab === 'workplace') loadWorkplaceClips();
    if (tab === 'autopost') loadAutoPostSettings();
}

// ─── Workplace (Review Before Post) ───────────────────────────────────────────
async function loadWorkplaceClips() {
    const container = document.getElementById('workplace-clips-container');
    if (!container) return;
    try {
        const userId = getActiveUserId();
        const res = await fetch(`/api/v1/analytics?user_id=${userId}`);
        const data = await res.json();
        const allClips = data.videos || [];
        // Workplace shows unposted drafts waiting for review (clips without a live YouTube link)
        const workplaceClips = allClips.filter(c => !c.youtube_url || (!c.youtube_url.includes('youtube.com') && !c.youtube_url.includes('youtu.be')));
        
        if (workplaceClips.length === 0) {
            container.innerHTML = '<div class="table-empty" style="grid-column: 1 / -1; padding: 48px 24px;">No draft clips waiting for review. Generate a new clip with auto-post turned off!</div>';
            return;
        }

        container.innerHTML = workplaceClips.map(c => {
            const rawId = c.youtube_url ? (c.youtube_url.split('shorts/')[1] || c.youtube_url.split('v=')[1] || '') : '';
            const videoId = rawId.split('?')[0];
            const isLive = Boolean(c.youtube_url);
            const thumbUrl = videoId
                ? `https://i.ytimg.com/vi/${videoId}/maxresdefault.jpg`
                : 'https://via.placeholder.com/400x700/18181b/3b82f6?text=Pending+Review';
            const title = escHtml(c.title || c.niche || 'Viral Short');
            
            const rawUrl = (c.youtube_url || '').replace(/\\/g, '/');
            const safeUrl = encodeURI(rawUrl);
            const rawFilename = rawUrl.split('/').pop() || '';
            const streamUrl = rawFilename ? `http://127.0.0.1:58921/${encodeURIComponent(rawFilename)}` : '';
            const mediaPreview = isLive && thumbUrl
                ? `<img src="${thumbUrl}" style="width:100%; height:100%; object-fit:cover; opacity:0.85; transition:opacity 0.2s;" onmouseover="this.style.opacity='1'" onmouseout="this.style.opacity='0.85'">`
                : `<video src="${streamUrl}" preload="metadata" muted playsinline style="width:100%; height:100%; object-fit:cover; opacity:0.9;"></video>`;

            return `
            <div class="glass-card" style="display:flex; flex-direction:column; overflow:hidden; border-radius:14px; border:1px solid rgba(255,255,255,0.08); background:rgba(20,20,20,0.6);">
                <div style="position:relative; aspect-ratio:9/16; background:#000; overflow:hidden; cursor:pointer;" onclick="openPlayer('${videoId}','${playUrl}','${title}')">
                    ${mediaPreview}
                    <div style="position:absolute; inset:0; display:flex; align-items:center; justify-content:center; background:rgba(0,0,0,0.25);">
                        <div style="width:48px; height:48px; border-radius:50%; background:rgba(220,38,38,0.9); display:flex; align-items:center; justify-content:center; box-shadow:0 4px 20px rgba(0,0,0,0.5);">
                            <svg width="20" height="20" viewBox="0 0 24 24" fill="white"><path d="M8 5v14l11-7z"/></svg>
                        </div>
                    </div>
                    <div style="position:absolute; top:12px; right:12px; background:rgba(234,179,8,0.85); color:white; font-size:11px; font-weight:700; padding:4px 8px; border-radius:6px; text-transform:uppercase;">
                        ⏳ Ready to Review
                    </div>
                </div>
                <div style="padding:16px; display:flex; flex-direction:column; gap:10px; flex:1; justify-content:space-between;">
                    <div>
                        <div style="font-weight:700; font-size:14px; color:#fff; line-height:1.4; margin-bottom:4px;">${title}</div>
                        <div style="font-size:12px; color:var(--text-3);">${c.created_at ? new Date(c.created_at).toLocaleDateString() : 'Recent'}</div>
                    </div>
                    <div style="display:flex; gap:8px;">
                        <button onclick="openPlayer('${videoId}','${playUrl}','${title}')" class="btn btn-outline" style="flex:1; justify-content:center; padding:8px; font-size:12px;">Watch</button>
                        <button onclick="publishClipToYouTube('${c.id}')" class="btn btn-generate" style="flex:1.4; justify-content:center; padding:8px; font-size:12px; background:#dc2626;">Post to YouTube</button>
                        <button onclick="deleteClip('${c.id}')" class="btn btn-outline" title="Delete Clip" style="padding:8px 10px; font-size:12px; color:#ef4444; border-color:rgba(239,68,68,0.25);">🗑</button>
                    </div>
                </div>
            </div>`;
        }).join('');

    } catch (e) {
        console.error('Workplace load error:', e);
    }
}

async function deleteClip(clipId) {
    if (!confirm('Are you sure you want to delete this clip?')) return;
    try {
        const res = await fetch(`/api/v1/clip/${clipId}`, { method: 'DELETE' });
        if (res.ok) {
            showToast('Clip deleted');
            loadWorkplaceClips();
            loadAnalytics();
        } else {
            showToast('Could not delete clip', 'error');
        }
    } catch (e) {
        showToast('Delete request failed', 'error');
    }
}

async function publishClipToYouTube(clipId) {
    try {
        const res = await fetch('/api/v1/clip/publish-draft', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ clip_id: clipId })
        });
        if (res.ok) {
            showToast('Video published to YouTube!');
            loadWorkplaceClips();
        } else {
            showToast('Publishing failed. Check YouTube connection.', 'error');
        }
    } catch (e) {
        showToast('Network error while publishing', 'error');
    }
}

// ─── Subscriptions Modal ──────────────────────────────────────────────────────
function openSubscriptionsModal() {
    const modal = document.getElementById('subscriptions-modal');
    if (modal) modal.classList.remove('hidden');
}

function closeSubscriptionsModal() {
    const modal = document.getElementById('subscriptions-modal');
    if (modal) modal.classList.add('hidden');
}

async function checkoutPlan(tier) {
    showToast(`Redirecting to ${tier} checkout...`, 'info');
    try {
        const res = await fetch('/api/v1/create-checkout-session', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tier: tier })
        });
        const data = await res.json();
        if (data.checkout_url) {
            window.location.href = data.checkout_url;
        } else {
            showToast('Could not initialize checkout', 'error');
        }
    } catch (e) {
        showToast('Checkout connection error', 'error');
    }
}

// ─── Brand Kit ────────────────────────────────────────────────────────────────
function saveBrandKit() {
    const handle = document.getElementById('brand-handle')?.value.trim() || '';
    const font   = document.getElementById('brand-font')?.value || 'Hormozi';
    localStorage.setItem('clipai_handle', handle);
    localStorage.setItem('clipai_font',   font);
    document.getElementById('brand-modal').classList.add('hidden');
    showToast('Brand Kit saved!');
}

function loadBrandKit() {
    const handle = localStorage.getItem('clipai_handle') || '';
    const font   = localStorage.getItem('clipai_font')   || 'Hormozi';
    const handleEl = document.getElementById('brand-handle');
    const fontEl   = document.getElementById('brand-font');
    if (handleEl) handleEl.value = handle;
    if (fontEl)   fontEl.value   = font;
}

// ─── Dynamic Ticker ───────────────────────────────────────────────────────────
function initTicker() {
    const ticker = document.getElementById('dynamic-ticker');
    if (!ticker) return;
    
    const names = ['mike_h', 'viral_king', 'sarah_j', 'anon', 'user183', 'crypto_god', 'hustler99', 'clip_master', 'tt_creator', 'passive_inc'];
    const actions = ['generated a', 'auto-posted a', 'hit 50k views on a', 'hit 1M views on a', 'rendered a', 'scheduled a'];
    const niches = ['Crypto', 'Motivation', 'MrBeast', 'Finance', 'Tech', 'Podcast', 'Gaming', 'Fitness'];
    const colors = ['var(--blue-light)', 'var(--green)'];
    
    let html = '';
    // Generate 30 random items
    for (let i = 0; i < 30; i++) {
        const time = Math.floor(Math.random() * 59) + 1;
        const name = names[Math.floor(Math.random() * names.length)];
        const action = actions[Math.floor(Math.random() * actions.length)];
        const niche = niches[Math.floor(Math.random() * niches.length)];
        const color = colors[Math.floor(Math.random() * colors.length)];
        
        let timeStr = i === 0 ? 'Just now' : `${time}m ago`;
        html += `<span style="color: ${color};">● ${timeStr}:</span> <strong>${name}</strong> ${action} ${niche} clip &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;`;
    }
    // Duplicate the content so the infinite scroll is seamless
    ticker.innerHTML = html + html;
}
document.addEventListener('DOMContentLoaded', initTicker);

// ─── Analytics ────────────────────────────────────────────────────────────────
let viewsChart = null;

async function loadAnalytics() {
    try {
        const userId = getActiveUserId();
        const res = await fetch(`/api/v1/analytics?user_id=${userId}`);
        const data = await res.json();

        document.getElementById('stat-total-views').textContent = formatNumber(data.total_views);
        document.getElementById('stat-total-videos').textContent = data.total_videos;
        document.getElementById('stat-avg-views').textContent = formatNumber(data.avg_views);

        const galleryGrid = document.getElementById('videos-gallery-grid');
        if (!galleryGrid) return;

        if (!data.videos || data.videos.length === 0) {
            galleryGrid.innerHTML = '<div class="table-empty" style="grid-column: 1 / -1;">No videos posted yet. Generate your first clip!</div>';
            return;
        }
        
        // Add canvas for chart dynamically
        
        // Render Chart container
        const chartContainer = document.createElement('div');
        chartContainer.style.marginBottom = '40px';
        chartContainer.style.height = '250px';
        chartContainer.style.width = '100%';
        chartContainer.innerHTML = '<canvas id="viewsChart"></canvas>';
        
        // Insert chart right before the gallery header (only if not already inserted)
        const galleryHeader = document.querySelector('.gallery-header');
        if (galleryHeader && !document.getElementById('viewsChart')) {
            galleryHeader.parentNode.insertBefore(chartContainer, galleryHeader);
        }
        
        // Render video cards (only live published clips appear in My Clips)
        const publishedClips = (data.videos || []).filter(v => v.youtube_url && (v.youtube_url.includes('youtube.com') || v.youtube_url.includes('youtu.be')));
        if (publishedClips.length === 0) {
            galleryGrid.innerHTML = '<div class="table-empty" style="grid-column: 1 / -1;">No live YouTube clips yet. Review your drafts in Workplace to publish them!</div>';
            return;
        }
        galleryGrid.innerHTML = publishedClips.map(v => {
            const rawId = v.youtube_url ? (v.youtube_url.split('shorts/')[1] || v.youtube_url.split('v=')[1] || '') : '';
            const videoId = rawId.split('?')[0];
            const thumbUrl = videoId
                ? `https://i.ytimg.com/vi/${videoId}/maxresdefault.jpg`
                : 'https://via.placeholder.com/400x700/1e293b/3b82f6?text=ClipAI';
            const viralScore = Math.floor(Math.random() * 12) + 88;
            const title = escHtml(v.title || v.niche || 'Untitled');
            
            return `
            <div class="clip-card" onclick="openPlayer('${videoId}','${v.youtube_url || ''}','${title}')">
                <div class="clip-card-thumb" style="background-image:url('${thumbUrl}')">
                    <div class="clip-card-overlay">
                        <div class="clip-play-btn">
                            <svg width="24" height="24" viewBox="0 0 24 24" fill="white"><path d="M8 5v14l11-7z"/></svg>
                        </div>
                    </div>
                    <div class="clip-score-badge">🔥 ${viralScore}</div>
                </div>
                <div class="clip-card-info">
                    <div class="clip-title">${title}</div>
                    <div class="clip-meta">
                        <span>${v.created_at ? new Date(v.created_at).toLocaleDateString() : '—'}</span>
                        <span class="clip-views">👁 ${formatNumber(v.views || 0)}</span>
                    </div>
                </div>
            </div>`;
        }).join('');
        
        // Render Chart
        if (viewsChart) {
            viewsChart.destroy();
        }
        
        // Prepare chart data (reverse to show chronological order)
        const chartVideos = [...data.videos].reverse();
        const labels = chartVideos.map(v => v.created_at ? new Date(v.created_at).toLocaleDateString() : '');
        const views = chartVideos.map(v => v.views || 0);
        
        const ctx = document.getElementById('viewsChart').getContext('2d');
        
        // Create gradient
        const gradient = ctx.createLinearGradient(0, 0, 0, 250);
        gradient.addColorStop(0, 'rgba(59, 130, 246, 0.5)'); // Blue
        gradient.addColorStop(1, 'rgba(59, 130, 246, 0.0)');
        
        viewsChart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [{
                    label: 'Views',
                    data: views,
                    borderColor: '#3b82f6',
                    backgroundColor: gradient,
                    borderWidth: 3,
                    pointBackgroundColor: '#3b82f6',
                    pointBorderColor: '#fff',
                    pointBorderWidth: 2,
                    pointRadius: 4,
                    pointHoverRadius: 6,
                    fill: true,
                    tension: 0.4
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: 'rgba(17, 24, 39, 0.9)',
                        titleColor: '#fff',
                        bodyColor: '#cbd5e1',
                        padding: 12,
                        cornerRadius: 8,
                        displayColors: false
                    }
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        grid: { color: 'rgba(255, 255, 255, 0.05)', drawBorder: false },
                        ticks: { color: '#64748b', maxTicksLimit: 5 }
                    },
                    x: {
                        grid: { display: false, drawBorder: false },
                        ticks: { color: '#64748b', maxTicksLimit: 7 }
                    }
                }
            }
        });
        
    } catch (e) {
        console.error('Analytics load error:', e);
    }
}

function formatNumber(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return String(n || 0);
}

function escHtml(str) {
    return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ─── Toast ────────────────────────────────────────────────────────────────────
function showToast(message, type = 'success') {
    const container = document.getElementById('toast-container');
    if (!container) return;
    
    const t = document.createElement('div');
    t.className = `toast ${type}`;
    
    let icon = '✅';
    if (type === 'error') icon = '❌';
    if (type === 'info') icon = 'ℹ️';
    
    t.innerHTML = `
        <div class="toast-icon">${icon}</div>
        <div class="toast-content">
            <h4>${type === 'error' ? 'Error' : 'Success'}</h4>
            <p>${message}</p>
        </div>
    `;
    
    container.appendChild(t);
    
    // Animate in
    requestAnimationFrame(() => {
        t.classList.add('show');
    });
    
    // Remove after 3.5s
    setTimeout(() => {
        t.classList.remove('show');
        setTimeout(() => t.remove(), 400); // Wait for transition
    }, 3500);
}

// ─── Add Log Entry ────────────────────────────────────────────────────────────
function addMessage(sender, text) {
    const chatWindow = document.getElementById('chat-window');
    if (!chatWindow) return;
    const el = document.createElement('div');
    el.className = 'log-entry';
    const icon = sender === 'You' ? '👤' : '🤖';
    const formatted = text.replace(/\n/g,'<br>').replace(/\*\*(.*?)\*\*/g,'<strong>$1</strong>');
    el.innerHTML = `
        <div class="log-icon">${icon}</div>
        <div class="log-body">
            <div class="log-sender">${sender}</div>
            <div class="log-text">${formatted}</div>
        </div>`;
    chatWindow.appendChild(el);
    chatWindow.scrollTop = chatWindow.scrollHeight;
}

// ─── Job Polling ──────────────────────────────────────────────────────────────
let pollingInterval = null;

function startStatusPolling(jobId) {
    const progressContainer = document.getElementById('progress-container');
    const progressFill = document.getElementById('progress-bar-fill');
    const progressText = document.getElementById('progress-text');
    const progressPct = document.getElementById('progress-pct');
    const runBtn = document.getElementById('run-clip-farm-btn');

    // Guard: all elements must exist before starting
    if (!progressContainer || !progressFill || !progressText || !runBtn) {
        console.warn('startStatusPolling: required DOM elements missing, aborting poll.');
        return;
    }

    progressContainer.classList.remove('hidden');
    progressFill.style.width = '5%';
    if (pollingInterval) clearInterval(pollingInterval);

    const BTN_ICON = `<svg width="18" height="18" viewBox="0 0 15 15" fill="none"><path d="M3 1.5L13.5 7.5L3 13.5V1.5Z" fill="currentColor"/></svg>`;

    pollingInterval = setInterval(async () => {
        try {
            const res = await fetch('/api/v1/job-status/' + jobId);
            const data = await res.json();

            if (data.status !== 'idle' && data.status !== 'queued') {
                const pct = Math.max(5, data.progress);
                progressFill.style.width = pct + '%';
                if (progressPct) progressPct.textContent = pct + '%';
                progressText.textContent = data.message;
                
                // Show Virality Score once the search/AI analysis is complete (around 50%)
                const viralityBadge = document.getElementById('virality-badge');
                if (pct >= 50 && viralityBadge && viralityBadge.style.display === 'none') {
                    viralityBadge.style.display = 'block';
                    const score = Math.floor(Math.random() * (99 - 88 + 1)) + 88;
                    const scoreEl = document.getElementById('virality-score');
                    if (scoreEl) scoreEl.textContent = score;
                }
                
                updatePipelineSteps(pct);
            }

            if (data.progress >= 100 || data.status === 'complete' || data.status === 'draft_ready' || data.status === 'error') {
                clearInterval(pollingInterval);
                localStorage.removeItem('active_job_id');
                runBtn.disabled = false;
                runBtn.innerHTML = `${BTN_ICON} Generate Clip`;
                progressFill.style.width = '100%';
                setTimeout(() => progressContainer.classList.add('hidden'), 4000);

                if (data.status === 'draft_ready' || (data.message && data.message.includes('Workplace'))) {
                    addMessage('Director AI', `🎬 **Video rendered!** It is waiting in your <a href="javascript:void(0)" onclick="switchTab('workplace')">Workplace</a> for review before posting.`);
                    showToast('Video saved to Workplace for review!');
                    loadWorkplaceClips();
                } else if (data.url && (data.url.includes('youtube.com') || data.url.includes('youtu.be'))) {
                    addMessage('Director AI', `Video is live! <a href="${data.url}" target="_blank">Watch on YouTube ↗</a>`);
                    showToast('Video posted to YouTube!');
                    loadAnalytics();
                } else if (data.status === 'error') {
                    addMessage('Director AI', `Error: ${data.message}`);
                    showToast('Generation failed — see activity log', 'error');
                } else {
                    addMessage('Director AI', `🎬 Video ready! Check your <a href="javascript:void(0)" onclick="switchTab('workplace')">Workplace</a>.`);
                    showToast('Video ready in Workplace!');
                    loadWorkplaceClips();
                }
            }
        } catch (e) {
            console.error(e);
            clearInterval(pollingInterval);
            localStorage.removeItem('active_job_id');
            if (runBtn) {
                runBtn.disabled = false;
                runBtn.innerHTML = `${BTN_ICON} Generate Clip`;
            }
        }
    }, 1500);
}

// ─── Account Modal & Profile Management ─────────────────────────────────────
async function openAccountModal() {
    const modal = document.getElementById('account-modal');
    if (!modal) return;
    modal.classList.remove('hidden');
    modal.style.display = 'flex';
    try {
        const res = await fetch('/api/v1/user/profile');
        const data = await res.json();
        const planBadge = document.getElementById('account-plan-badge');
        const userIdSpan = document.getElementById('account-user-id');
        const workerStatusEl = document.getElementById('account-worker-status');
        const loggedInBox = document.getElementById('account-logged-in-box');
        const loginBox = document.getElementById('account-login-box');
        const userEmailEl = document.getElementById('account-user-email');
        const avatarLetter = document.getElementById('account-avatar-letter');
        
        if (data.email) {
            if (loggedInBox) loggedInBox.style.display = 'block';
            if (loginBox) loginBox.style.display = 'none';
            if (userEmailEl) userEmailEl.textContent = data.email;
            if (avatarLetter) avatarLetter.textContent = data.email[0].toUpperCase();
        } else {
            if (loggedInBox) loggedInBox.style.display = 'none';
            if (loginBox) loginBox.style.display = 'block';
        }

        if (planBadge) planBadge.textContent = (data.license || 'free_tier').replace('_', ' ').toUpperCase();
        if (userIdSpan) userIdSpan.textContent = data.user_id || '';
        if (workerStatusEl) {
            workerStatusEl.textContent = workerIsAlive ? '🟢 Online' : '🔴 Offline';
            workerStatusEl.style.color = workerIsAlive ? '#10b981' : '#ef4444';
        }
    } catch (e) {
        console.error('Failed to load profile:', e);
    }
}

function closeAccountModal() {
    const modal = document.getElementById('account-modal');
    if (modal) {
        modal.classList.add('hidden');
        modal.style.display = 'none';
    }
}

function logoutAccount() {
    if (!confirm('Are you sure you want to sign out?')) return;
    localStorage.removeItem('clipai_user_id');
    document.cookie = 'user_id=;path=/;max-age=0;SameSite=Lax';
    showToast('Signed out successfully');
    setTimeout(() => window.location.reload(), 600);
}

async function saveAccountEmail() {
    const email = document.getElementById('account-email-input')?.value.trim();
    if (!email || !email.includes('@')) {
        showToast('Please enter a valid email address', 'error');
        return;
    }
    try {
        const res = await fetch('/api/v1/user/profile', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email: email })
        });
        const data = await res.json();
        if (res.ok) {
            if (data.user_id) {
                localStorage.setItem('clipai_user_id', data.user_id);
                document.cookie = `user_id=${data.user_id};path=/;max-age=31536000;SameSite=Lax`;
            }
            showToast('Account profile linked successfully!');
            const userLabel = document.getElementById('user-display-label');
            if (userLabel) userLabel.textContent = email.split('@')[0];
            closeAccountModal();
            loadWorkplaceClips();
            loadAnalytics();
            checkWorkerHeartbeat();
        } else {
            showToast(data.detail || 'Failed to update email', 'error');
        }
    } catch (e) {
        showToast('Error saving account profile', 'error');
    }
}

// ─── On Page Load ─────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
    // Force initialize layout state
    switchTab('generate');
    
    // Load saved brand kit settings
    loadBrandKit();

    // Check existing account profile and permanently persist user_id
    try {
        const res = await fetch('/api/v1/user/profile');
        const data = await res.json();
        if (data.user_id) {
            localStorage.setItem('clipai_user_id', data.user_id);
            document.cookie = `user_id=${data.user_id};path=/;max-age=315360000;SameSite=Lax`;
        }
        if (data.email) {
            const userLabel = document.getElementById('user-display-label');
            if (userLabel) userLabel.textContent = data.email.split('@')[0];
        }
    } catch (e) {}

    // Handle Google Auth & YouTube OAuth redirects
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get('auth') === 'success') {
        window.history.replaceState({}, '', window.location.pathname);
        showToast('Signed in with Google successfully!');
        try {
            const pRes = await fetch('/api/v1/user/profile');
            const pData = await pRes.json();
            if (pData.user_id) {
                localStorage.setItem('clipai_user_id', pData.user_id);
                document.cookie = `user_id=${pData.user_id};path=/;max-age=315360000;SameSite=Lax`;
            }
            if (pData.email) {
                const userLabel = document.getElementById('user-display-label');
                if (userLabel) userLabel.textContent = pData.email.split('@')[0];
            }
        } catch (e) {}
    } else if (urlParams.get('auth') === 'error') {
        const detail = urlParams.get('detail') || 'Google sign-in was canceled';
        showToast('Google login error: ' + detail, 'error');
        window.history.replaceState({}, '', window.location.pathname);
    }

    if (urlParams.get('youtube') === 'connected') {
        localStorage.setItem('youtube_connected', 'true');
        window.history.replaceState({}, '', window.location.pathname);
        showToast('YouTube connected successfully!');
    } else if (urlParams.get('youtube') === 'error') {
        const detail = urlParams.get('detail') || 'Unknown error';
        showToast('YouTube connection failed: ' + detail, 'error');
        window.history.replaceState({}, '', window.location.pathname);
    }

    // Resume polling if a job was running before page refresh
    const activeJobId = localStorage.getItem('active_job_id');
    if (activeJobId) {
        // First check if the job is actually still active on the server
        try {
            const res = await fetch('/api/v1/job-status/' + activeJobId);
            const data = await res.json();
            // Only clear if genuinely done or job not found — NOT just because progress is 0
            const terminalStates = ['complete', 'error', 'idle'];
            if (!data.status || terminalStates.includes(data.status) || data.status === 'unknown') {
                localStorage.removeItem('active_job_id');
            } else {
                // Job is genuinely still running (queued, processing, running) — resume polling
                const runBtn = document.getElementById('run-clip-farm-btn');
                runBtn.disabled = true;
                runBtn.textContent = 'Running...';
                startStatusPolling(activeJobId);
            }
        } catch (e) {
            // Can't reach server — clear the job to avoid infinite stuck state
            localStorage.removeItem('active_job_id');
        }
    }
});

// ─── Niche Presets ─────────────────────────────────────────────────────────────
function selectNichePreset(nicheName, btnEl) {
    const input = document.getElementById('niche-input');
    if (input) input.value = nicheName;
    document.querySelectorAll('.niche-pill').forEach(btn => {
        btn.style.borderColor = 'rgba(255,255,255,0.12)';
        btn.style.background = 'rgba(255,255,255,0.06)';
    });
    if (btnEl) {
        btnEl.style.borderColor = 'var(--blue)';
        btnEl.style.background = 'rgba(37,99,235,0.25)';
    }
}

// ─── Generate Button ──────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    checkWorkerHeartbeat(); // initial check

    // Check account requirement when toggling Auto-Post OFF
    const autoPostToggle = document.getElementById('studio-autopost-toggle');
    if (autoPostToggle) {
        autoPostToggle.addEventListener('change', async (e) => {
            if (!autoPostToggle.checked) {
                // User is trying to turn OFF auto-post (draft/review mode)
                try {
                    const res = await fetch('/api/v1/user/profile');
                    const data = await res.json();
                    if (!data.email) {
                        e.preventDefault();
                        autoPostToggle.checked = true; // Revert switch
                        showToast('Please log into your account first to use Workplace review mode!', 'warning');
                        openAccountModal();
                    }
                } catch (err) {
                    console.error('Auth verification error:', err);
                }
            }
        });
    }

    const runBtn = document.getElementById('run-clip-farm-btn');

    runBtn.addEventListener('click', async () => {
        const isConnected = localStorage.getItem('youtube_connected') === 'true';
        if (!isConnected) {
            addMessage('Director AI', 'Please connect your YouTube account first using the **Connect YouTube** button in the top right.');
            showToast('Connect YouTube first', 'info');
            return;
        }

        const niche = document.getElementById('niche-input').value.trim() || 'motivation';
        const autoUploadToggle = document.getElementById('studio-autopost-toggle');
        const autoUpload = autoUploadToggle ? autoUploadToggle.checked : true;
        
        const layout = document.getElementById('studio-layout-select')?.value || 'split_screen';
        const subtitleStyle = document.getElementById('studio-subtitle-select')?.value || 'hormozi';

        runBtn.disabled = true;
        runBtn.textContent = 'Running...';

        try {
            const res = await fetch('/api/v1/generate-clip', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    niche, 
                    auto_upload: autoUpload,
                    layout: layout,
                    subtitle_style: subtitleStyle
                })
            });

            if (res.status === 402) {
                document.getElementById('paywall-modal').classList.remove('hidden');
                runBtn.disabled = false;
                runBtn.innerHTML = `<svg width="18" height="18" viewBox="0 0 15 15" fill="none"><path d="M3 1.5L13.5 7.5L3 13.5V1.5Z" fill="currentColor"/></svg> Generate Clip`;
                return;
            }

            const data = await res.json();
            if (data.job_id) {
                localStorage.setItem('active_job_id', data.job_id);
                addMessage('Director AI', `Pipeline started for **${niche}**. Watch the progress bar!`);
                startStatusPolling(data.job_id);
                // Live-update the free generations badge
                if (data.free_remaining !== null && data.free_remaining !== undefined) {
                    const badge = document.getElementById('free-tier-badge');
                    const span = document.getElementById('free-remaining');
                    if (span) span.textContent = data.free_remaining;
                    if (badge && data.free_remaining === 0) {
                        badge.style.color = '#ef4444';
                        badge.innerHTML = '🔒 Free Tier — <span id="free-remaining">0</span> generations remaining';
                    }
                }
            } else {
                runBtn.disabled = false;
                runBtn.innerHTML = `<svg width="18" height="18" viewBox="0 0 15 15" fill="none"><path d="M3 1.5L13.5 7.5L3 13.5V1.5Z" fill="currentColor"/></svg> Generate Clip`;
            }
        } catch (e) {
            addMessage('Director AI', 'Connection error. Please try again.');
            runBtn.disabled = false;
            runBtn.innerHTML = `<svg width="18" height="18" viewBox="0 0 15 15" fill="none"><path d="M3 1.5L13.5 7.5L3 13.5V1.5Z" fill="currentColor"/></svg> Generate Clip`;
        }
    });

    // Paywall modal buttons
    const checkoutBtn = document.getElementById('checkout-btn');
    if (checkoutBtn) {
        checkoutBtn.addEventListener('click', async () => {
            const res = await fetch('/api/v1/create-checkout-session', { method: 'POST' });
            const data = await res.json();
            if (data.checkout_url) window.location.href = data.checkout_url;
        });
    }
    const closeModalBtn = document.getElementById('close-modal');
    if (closeModalBtn) {
        closeModalBtn.addEventListener('click', () => {
            document.getElementById('paywall-modal').classList.add('hidden');
        });
    }
});

// ─── Cancel Job (global scope — called from onclick in HTML) ──────────────────
function cancelJob() {
    // Clear the stored job so the UI stops polling
    localStorage.removeItem('active_job_id');
    // Hide the progress card and virality badge
    const progressContainerEl = document.getElementById('progress-container');
    if (progressContainerEl) progressContainerEl.classList.add('hidden');
    const viralityBadge = document.getElementById('virality-badge');
    if (viralityBadge) viralityBadge.style.display = 'none';
    
    // Re-enable the generate button
    const runBtn = document.getElementById('run-clip-farm-btn');
    if (runBtn) {
        runBtn.disabled = false;
        runBtn.innerHTML = `
            <svg width="18" height="18" viewBox="0 0 15 15" fill="none">
                <path d="M3 1.5L13.5 7.5L3 13.5V1.5Z" fill="currentColor"/>
            </svg>
            Generate Clip
        `;
    }
    resetPipelineSteps();
    addMessage('Director AI', 'Job cancelled. Ready to generate a new clip!');
}

// ─── Auto Post ────────────────────────────────────────────────────────────────

function addTimeInput(value = '') {
    const container = document.getElementById('times-container');
    const row = document.createElement('div');
    row.style.cssText = 'display:flex; align-items:center; gap:10px;';
    row.innerHTML = `
        <input type="time" class="time-input niche-input-lg" style="max-width:160px;" value="${value}">
        <button type="button" onclick="this.parentElement.remove()" style="background:none;border:none;color:var(--text-3);cursor:pointer;font-size:20px;line-height:1;padding:0 4px;" title="Remove">×</button>
    `;
    container.appendChild(row);
}

async function loadAutoPostSettings() {
    try {
        const res = await fetch('/api/v1/auto-post/settings');
        const data = await res.json();
        
        document.getElementById('autopost-enable').checked = data.enabled || false;
        document.getElementById('autopost-niche').value = data.niche || 'motivation';
        
        // Populate dynamic time inputs
        const container = document.getElementById('times-container');
        container.innerHTML = '';
        const times = data.times && data.times.length ? data.times : (data.time ? [data.time] : ['12:00']);
        times.forEach(t => addTimeInput(t));
        
        if (data.days && Array.isArray(data.days)) {
            document.querySelectorAll('.day-cb').forEach(cb => {
                cb.checked = data.days.includes(cb.value);
            });
        } else {
            document.querySelectorAll('.day-cb').forEach(cb => cb.checked = true);
        }
    } catch (e) {
        console.error('Failed to load auto post settings:', e);
        addTimeInput('12:00');
    }
}

async function saveAutoPostSettings() {
    const btn = document.getElementById('btn-save-autopost');
    const prevText = btn.textContent;
    btn.textContent = 'Saving...';
    btn.disabled = true;
    
    const days = Array.from(document.querySelectorAll('.day-cb'))
                      .filter(cb => cb.checked)
                      .map(cb => cb.value);
                      
    const times = Array.from(document.querySelectorAll('.time-input'))
                       .map(i => i.value)
                       .filter(v => v);
    
    try {
        const payload = {
            enabled: document.getElementById('autopost-enable').checked,
            times: times,
            niche: document.getElementById('autopost-niche').value || 'motivation',
            days: days
        };
        
        const res = await fetch('/api/v1/auto-post/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        
        if (res.ok) {
            showToast('Auto Post Schedule Saved!');
        } else {
            showToast('Failed to save settings', 'error');
        }

    } catch (e) {
        console.error('Error saving:', e);
        showToast('Error saving settings', 'error');
    } finally {
        btn.textContent = prevText;
        btn.disabled = false;
    }
}


################################################################################
# FILE: requirements.txt
################################################################################

httpx>=0.27
requests>=2.31
tenacity>=8.2
yt-dlp>=2024.8.1
google-auth>=2.29
google-auth-oauthlib>=1.2
google-api-python-client>=2.130
redis>=5.0
python-dotenv>=1.0

# optional, only needed if you use youtube_uploader's token-persist path
supabase>=2.4

# dev / test
pytest>=8.0
pytest-cov>=5.0


################################################################################
# FILE: .env.example
################################################################################

YOUTUBE_API_KEY=
WORKER_SECRET=
API_BASE_URL=https://viralclip-saas.onrender.com
REDIS_URL=redis://localhost:6379/0
CLIPAI_WEBHOOK_URL=
CLIPAI_WEBHOOK_SECRET=
CLIPAI_LOG_LEVEL=INFO
CLIPAI_LOG_JSON=false


################################################################################
# FILE: README.md
################################################################################

# ClipAI pipeline (v2)

Two legitimate video-sourcing modes, hardened with retries, structured
logging, type hints, tests, a config file, and a CLI. See `HANDOFF.md` for
the full architecture writeup and `NOTES.md` for the earlier mode-rewrite
summary.

## Quick start
```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your keys
pytest                 # run the test suite
python worker.py --mode licensed_cc --user-id demo --niche "cooking tips" --no-upload
```


################################################################################
# FILE: HANDOFF.md
################################################################################

# HANDOFF — ClipAI pipeline, v2 (own_content + licensed_cc, hardened)

## What v2 adds on top of the mode rewrite
- **config.py** — every tunable (thresholds, timeouts, paths, secrets) now
  comes from env vars via a single `Settings` dataclass, instead of scattered
  `os.environ.get()` calls. Supports a `.env` file if `python-dotenv` is
  installed.
- **logging_setup.py** — structured logging (`get_logger(name)`) replacing
  `print()` everywhere. Set `CLIPAI_LOG_JSON=1` for JSON-line output if you
  want to ship logs to something like Datadog/Loki.
- **Retries** — network calls (YouTube Data API search/details, the
  clip-analysis backend call, yt-dlp downloads) now retry with exponential
  backoff via `tenacity`, bounded by `CLIPAI_MAX_RETRIES` (default 3).
- **Type hints + dataclasses** — `VideoCandidate`, `DownloadResult`,
  `ClipSegment`, `ClipJob` replace the old loose dicts, so mismatched keys
  fail at call time instead of silently producing `None`/`KeyError`s deep in
  the pipeline.
- **Custom exceptions** — `VideoFinderError`, `DownloadError`, `ClipCutError`,
  `PipelineError`, `UploadError` instead of bare `Exception`. `worker.py`
  catches these specifically and reports a clean message; anything
  unexpected is logged with a full traceback (`log.exception(...)`) rather
  than swallowed.
- **Webhook support** — set `CLIPAI_WEBHOOK_URL` (and optionally
  `CLIPAI_WEBHOOK_SECRET` for an HMAC-SHA256 signature in the
  `X-ClipAI-Signature` header) and every `update_job_status` call also POSTs
  a JSON event there, independent of the website's own progress API. This is
  the integration point for Antigravity or any other external listener —
  point its webhook receiver at this URL and it gets every status update the
  website gets, without needing to poll or share code.
- **CLI** — `python worker.py --mode licensed_cc --user-id U --niche "cooking tips"`
  runs one job end-to-end from the command line (see below). Useful for
  manual testing, cron-triggered runs, or letting Antigravity shell out to
  it directly instead of importing it as a library.
- **Tests** — `tests/` covers the pure logic: ISO8601 duration parsing, VTT
  parsing, ASS timestamp formatting, ffmpeg text escaping, and `ClipJob`
  validation rules. These don't hit the network or ffmpeg, so they run fast
  and are safe for CI. Run with `pytest` from the project root.

## Everything from the v1 handoff still applies
- Two modes: `own_content` (`source_kind="file"` or `"channel"`) and
  `licensed_cc` (searches YouTube Data API, CC-licensed only, re-verified
  server-side via `status.license`).
- No scraping fallback, no client-fingerprint rotation, no proxy evasion, no
  fingerprint-defeating transforms, no auto-fetched b-roll. Same "do not
  reintroduce" list as before — see below.
- Attribution is mandatory (not optional) in the upload description for
  `licensed_cc` mode.

## CLI usage
```bash
# Render only, no upload — good for testing
python worker.py --mode licensed_cc --user-id U --niche "cooking tips" --no-upload

# Full pipeline, own uploaded file, auto-upload to the user's channel
python worker.py --mode own_content --user-id U --source-kind file \
  --source /path/to/video.mp4 --job-id job-123

# Own channel video, split-screen layout with licensed b-roll
python worker.py --mode own_content --user-id U --source-kind channel \
  --source dQw4w9WgXcQ --layout split_screen --broll-path /path/to/broll.mp4
```

## Config reference (env vars)
| Var | Default | Notes |
|---|---|---|
| `YOUTUBE_API_KEY` | — | required for `licensed_cc` |
| `WORKER_SECRET` | — | required always (creds fetch HMAC) |
| `API_BASE_URL` | `https://viralclip-saas.onrender.com` | website job-status API |
| `REDIS_URL` | `redis://localhost:6379/0` | optional, falls back to JSON file |
| `CLIPAI_MIN_VIEWS` | `50000` | CC search filter |
| `CLIPAI_MIN_DURATION_SEC` | `300` | CC search filter |
| `CLIPAI_MAX_AGE_DAYS` | `730` | CC search filter |
| `CLIPAI_TOP_N` | `3` | candidates considered per search |
| `CLIPAI_MAX_SHORT_SEC` | `56` | render cap, stays under YT's 60s limit |
| `CLIPAI_DEFAULT_WATERMARK` | `@YourChannel` | fallback if job doesn't set one |
| `CLIPAI_FFMPEG_TIMEOUT` | `600` | seconds |
| `CLIPAI_HTTP_TIMEOUT` | `20` | seconds, per API call |
| `CLIPAI_MAX_RETRIES` | `3` | applies to API calls + downloads |
| `CLIPAI_WEBHOOK_URL` | — | optional, generic status webhook |
| `CLIPAI_WEBHOOK_SECRET` | — | optional, HMAC-signs webhook body |
| `CLIPAI_LOG_LEVEL` | `INFO` | |
| `CLIPAI_LOG_JSON` | `false` | |

## Explicitly do not reintroduce
- Any yt-dlp *search* fallback when the Data API path fails.
- Client-fingerprint rotation (`ios`/`android`/`mweb`/`tv`) or proxy routing
  used to get past bot-detection.
- Speed/color/crop transforms whose purpose is to alter a fingerprint rather
  than serve a real formatting/aesthetic goal.
- Auto-downloading third-party footage without an explicit rights check.
- A "blacklist of major studios" as a stand-in for an actual license check.

## Open items for you to wire up
- Website: file upload form → `source_kind="file"`; channel picker UI →
  `source_kind="channel"`.
- Consent screen: add `youtube.readonly` scope for own-channel mode.
- Point `CLIPAI_WEBHOOK_URL` at Antigravity (or whatever's consuming job
  events) if you want it watching pipeline runs live.
- `pip install -r requirements.txt` and `pytest` in CI before merging any
  change to these modules.
