"""
Ported from v2 (video_finder.py) — the sourcing logic itself was already
solid: official YouTube Data API only, no scraping fallback, server-side
re-verification of the CC license. Only the imports changed for the new
package layout, and `top_n_candidates` default raised (3 -> 5 via config)
so multi-clip jobs have enough distinct source videos to draw from.
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

from .config import settings
from .logging_setup import get_logger

log = get_logger("video_finder")


class VideoFinderError(Exception):
    pass


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


USED_VIDEOS_REDIS_KEY = "viralclip:used_videos"
_redis_used_client = None


def _get_used_redis():
    global _redis_used_client
    if _redis_used_client is None:
        try:
            import redis as _rl
            c = _rl.Redis.from_url("redis://localhost:6379/0", decode_responses=True, socket_connect_timeout=2)
            c.ping()
            _redis_used_client = c
        except Exception as e:
            log.debug("Redis unavailable for used-video tracking: %s", e)
            _redis_used_client = False
    return _redis_used_client if _redis_used_client else None


def load_used_videos() -> dict:
    r = _get_used_redis()
    if r:
        try:
            return {vid: {} for vid in r.smembers(USED_VIDEOS_REDIS_KEY)}
        except Exception as e:
            log.warning("Redis read failed: %s", e)
    return {}


def mark_video_used(video_id: str, title: str = "") -> None:
    r = _get_used_redis()
    if r:
        try:
            r.sadd(USED_VIDEOS_REDIS_KEY, video_id)
            return
        except Exception as e:
            log.warning("Redis write failed: %s", e)


def _iso8601_to_seconds(duration: str) -> int:
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


def find_licensed_cc_videos(niche: str, max_results: int = 20) -> list[VideoCandidate]:
    if not settings.youtube_api_key:
        raise VideoFinderError("YOUTUBE_API_KEY is required for licensed_cc mode.")

    used = load_used_videos()
    cutoff = (datetime.now() - timedelta(days=settings.max_age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    search_params = {
        "part": "id,snippet", "q": niche, "type": "video", "order": "viewCount",
        "videoDuration": "medium", "publishedAfter": cutoff, "maxResults": max_results,
        "videoLicense": "creativeCommon", "key": settings.youtube_api_key,
    }
    try:
        data = _http_get("https://www.googleapis.com/youtube/v3/search", search_params)
    except Exception as e:
        raise VideoFinderError(f"YouTube API search request failed: {e}") from e
    if "error" in data:
        raise VideoFinderError(f"YouTube API error: {data['error'].get('message', data['error'])}")

    video_ids = [item["id"]["videoId"] for item in data.get("items", []) if item.get("id", {}).get("videoId")]
    if not video_ids:
        raise VideoFinderError(f"No CC-licensed videos found for '{niche}'.")

    detail_data = _http_get("https://www.googleapis.com/youtube/v3/videos", {
        "part": "contentDetails,statistics,snippet,status", "id": ",".join(video_ids), "key": settings.youtube_api_key,
    })

    candidates: list[VideoCandidate] = []
    for item in detail_data.get("items", []):
        vid_id = item["id"]
        if vid_id in used:
            continue
        if item.get("status", {}).get("license", "") != "creativeCommon":
            continue  # re-verify server-side, don't trust the search filter alone

        duration_sec = _iso8601_to_seconds(item.get("contentDetails", {}).get("duration", "PT0S"))
        view_count = int(item.get("statistics", {}).get("viewCount", 0))
        if duration_sec < settings.min_duration_sec or view_count < settings.min_views:
            continue

        title = item.get("snippet", {}).get("title", "")[:60]
        channel_name = item.get("snippet", {}).get("channelTitle", "Unknown")
        candidates.append(VideoCandidate(
            id=vid_id, title=title, url=f"https://www.youtube.com/watch?v={vid_id}",
            duration=duration_sec, view_count=view_count, channel=channel_name, license="creativeCommon",
            attribution=f"Original by {channel_name} (CC BY) https://youtu.be/{vid_id}",
        ))

    candidates.sort(key=lambda c: c.view_count, reverse=True)
    top = candidates[: settings.top_n_candidates]
    if not top:
        raise VideoFinderError(f"No qualifying CC-licensed videos found for '{niche}' after filtering.")
    return top


def register_uploaded_file(file_path: str, title: str = "Uploaded video") -> VideoCandidate:
    p = Path(file_path)
    if not p.exists():
        raise VideoFinderError(f"Uploaded file not found: {file_path}")
    return VideoCandidate(id=p.stem, title=title[:60], local_path=str(p))