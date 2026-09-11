# automation.py — ClipAI v2 Unified Pipeline Architecture
# Clean Legal Modes: own_content (Mode A) & licensed_cc (Mode B)


# ============================================================
# MODULE: config.py
# ============================================================

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

# ============================================================
# MODULE: logging_setup.py
# ============================================================

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

# ============================================================
# MODULE: video_finder.py
# ============================================================

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

# ============================================================
# MODULE: video_downloader.py
# ============================================================

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

# ============================================================
# MODULE: clip_finder.py
# ============================================================

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

# ============================================================
# MODULE: clip_cutter.py
# ============================================================

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

# ============================================================
# MODULE: youtube_uploader.py
# ============================================================

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

# ============================================================
# MODULE: hot_pipeline.py
# ============================================================

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

# ============================================================
# MODULE: worker.py
# ============================================================

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
