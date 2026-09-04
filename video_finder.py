"""
video_finder.py
Searches YouTube Data API v3 for trending videos matching a niche/creator query.
Falls back to yt-dlp search if API key not available.
"""
import os
import json
import random
import httpx
from datetime import datetime, timedelta
from pathlib import Path

os.makedirs(str(Path.home() / ".clipai"), exist_ok=True)

USED_VIDEOS_FILE = os.path.join(str(Path.home() / ".clipai"), "used_videos.json")
MIN_VIEWS = 50_000
MIN_DURATION_SEC = 300   # 5 minutes
MAX_AGE_DAYS = 730
TOP_N = 3

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")


def _safe(text: str) -> str:
    return text.encode("ascii", errors="replace").decode("ascii")


def log(msg: str):
    print(_safe(str(msg)))


def load_used_videos() -> dict:
    if os.path.exists(USED_VIDEOS_FILE):
        try:
            with open(USED_VIDEOS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def mark_video_used(video_id: str, title: str):
    used = load_used_videos()
    used[video_id] = {"title": title, "used_at": datetime.now().isoformat()}
    with open(USED_VIDEOS_FILE, "w") as f:
        json.dump(used, f, indent=2)
    log(f"[VideoFinder] Marked as used: {video_id} -- '{_safe(title)[:50]}'")


def _iso8601_to_seconds(duration: str) -> int:
    """Convert YouTube ISO 8601 duration (PT4M13S) to seconds."""
    import re
    pattern = re.compile(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?')
    match = pattern.match(duration)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def _search_via_api(niche: str, max_results: int = 20) -> list:
    """Use YouTube Data API v3 — never gets blocked."""
    used = load_used_videos()
    log(f"[VideoFinder] Searching YouTube API for: '{niche}'")

    cutoff = (datetime.now() - timedelta(days=MAX_AGE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Step 1: Search for videos
    search_url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "id,snippet",
        "q": niche,
        "type": "video",
        "order": "viewCount",
        "videoDuration": "medium",  # 4–20 min videos
        "publishedAfter": cutoff,
        "maxResults": max_results,
        "key": YOUTUBE_API_KEY,
    }

    try:
        res = httpx.get(search_url, params=params, timeout=20)
        data = res.json()
    except Exception as e:
        raise Exception(f"YouTube API request failed: {e}")

    if "error" in data:
        raise Exception(f"YouTube API error: {data['error'].get('message', str(data['error']))}")

    items = data.get("items", [])
    if not items:
        return []

    video_ids = [item["id"]["videoId"] for item in items if item.get("id", {}).get("videoId")]
    if not video_ids:
        return []

    # Step 2: Get video details (duration, view count)
    details_url = "https://www.googleapis.com/youtube/v3/videos"
    detail_params = {
        "part": "contentDetails,statistics,snippet",
        "id": ",".join(video_ids),
        "key": YOUTUBE_API_KEY,
    }

    try:
        detail_res = httpx.get(details_url, params=detail_params, timeout=20)
        detail_data = detail_res.json()
    except Exception as e:
        raise Exception(f"YouTube API detail request failed: {e}")

    candidates = []
    for item in detail_data.get("items", []):
        vid_id = item["id"]
        if vid_id in used:
            log(f"  [SKIP] Already used: {vid_id}")
            continue

        duration_str = item.get("contentDetails", {}).get("duration", "PT0S")
        duration_sec = _iso8601_to_seconds(duration_str)
        view_count = int(item.get("statistics", {}).get("viewCount", 0))
        title = _safe(item.get("snippet", {}).get("title", ""))[:60]

        if duration_sec < MIN_DURATION_SEC:
            log(f"  [SKIP] Too short ({duration_sec//60}min): {title}")
            continue
        if view_count < MIN_VIEWS:
            log(f"  [SKIP] Too few views ({view_count:,}): {title}")
            continue

        candidates.append({
            "url": f"https://www.youtube.com/watch?v={vid_id}",
            "title": title,
            "duration": duration_sec,
            "view_count": view_count,
            "id": vid_id,
        })
        log(f"  [OK] {title} | {view_count:,} views | {duration_sec//60}min")

    return candidates


def _search_via_ytdlp(niche: str, max_results: int = 15) -> list:
    """Fallback: use yt-dlp (may be blocked on cloud IPs)."""
    import yt_dlp
    used = load_used_videos()

    SEARCH_SUFFIXES = ["highlights", "best moments", "funny moments", "viral", "trending", ""]
    suffix = random.choice(SEARCH_SUFFIXES)
    query = f"{niche} {suffix}".strip()
    search_query = f"ytsearch{max_results}:{query}"
    log(f"[VideoFinder] yt-dlp fallback search: '{query}'")

    ydl_opts = {
        "quiet": True, 
        "no_warnings": True, 
        "noplaylist": True, 
        "skip_download": True,
        "ignoreerrors": True  # Skip age-restricted or unavailable videos
    }
    candidates = []
    last_error = None

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            entries = info.get("entries", []) if info else []
            for entry in entries:
                if not entry:
                    continue
                vid_id = entry.get("id") or ""
                duration = entry.get("duration") or 0
                view_count = entry.get("view_count") or 0
                title = _safe(entry.get("title", ""))[:60]

                if vid_id in used:
                    continue
                if duration < MIN_DURATION_SEC or view_count < MIN_VIEWS:
                    continue

                candidates.append({
                    "url": entry.get("webpage_url") or f"https://www.youtube.com/watch?v={vid_id}",
                    "title": title,
                    "duration": duration,
                    "view_count": view_count,
                    "id": vid_id,
                })
    except Exception as e:
        last_error = str(e)
        log(f"[VideoFinder] yt-dlp error: {e}")

    if not candidates and last_error:
        raise Exception(f"yt-dlp search failed: {last_error}")

    return candidates


def find_viral_video(niche: str = "finance", max_results: int = 15) -> dict:
    candidates = find_viral_videos(niche, max_results)
    return candidates[0] if candidates else {}


def find_viral_videos(niche: str = "finance", max_results: int = 15) -> list:
    # ── 0. Handle Direct YouTube URL Input ───────────────────
    trimmed = niche.strip()
    if "youtube.com/watch" in trimmed or "youtu.be/" in trimmed or "youtube.com/shorts/" in trimmed:
        import yt_dlp
        log(f"[VideoFinder] Direct YouTube URL detected: {trimmed}")
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
                info = ydl.extract_info(trimmed, download=False)
                if info:
                    vid_id = info.get("id") or "direct_vid"
                    title = _safe(info.get("title", "Custom YouTube Video"))[:60]
                    duration = info.get("duration") or 300
                    view_count = info.get("view_count") or 100000
                    return [{
                        "url": trimmed,
                        "title": title,
                        "duration": duration,
                        "view_count": view_count,
                        "id": vid_id
                    }]
        except Exception as e:
            log(f"[VideoFinder] Failed to inspect direct URL: {e}")

    candidates = []

    # Prefer YouTube Data API (never gets blocked)
    if YOUTUBE_API_KEY:
        try:
            candidates = _search_via_api(niche, max_results)
        except Exception as e:
            log(f"[VideoFinder] API search failed, trying yt-dlp: {e}")

    # Fallback to yt-dlp
    if not candidates:
        candidates = _search_via_ytdlp(niche, max_results)

    if not candidates:
        raise Exception(f"No videos found for '{niche}'. Try a different search term.")

    # Sort by views descending
    candidates.sort(key=lambda x: x["view_count"], reverse=True)
    top = candidates[:TOP_N]
    for i, c in enumerate(top):
        log(f"[VideoFinder] Candidate {i+1}: '{c['title']}' ({c['view_count']:,} views | {c['duration']//60}min)")

    return top
