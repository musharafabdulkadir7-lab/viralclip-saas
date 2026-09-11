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
