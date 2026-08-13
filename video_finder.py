"""
video_finder.py
Searches YouTube for genuinely trending long-form videos using yt-dlp.
Filters: 500K+ views, uploaded within last 2 years, not previously used.
Returns multiple candidates so the pipeline can fall back if one fails to download.
"""
import yt_dlp
import json
import os
import random
from datetime import datetime, timedelta

USED_VIDEOS_FILE = "used_videos.json"
MIN_VIEWS = 500_000     # Must have at least 500K views
MIN_DURATION = 600      # At least 10 minutes
MAX_AGE_DAYS = 730      # Within the last 2 years
TOP_N = 3               # Return top N candidates so pipeline can retry on 403

# Varied search phrases for content diversity
SEARCH_SUFFIXES = [
    "motivational speech",
    "life advice",
    "how to get rich",
    "financial freedom speech",
    "success mindset",
    "money habits",
    "millionaire mindset talk",
    "build wealth advice",
    "never give up speech",
    "self improvement talk",
    "powerful speech",
    "inspiring talk",
    "entrepreneurship advice",
    "wealth building",
]


def _safe(text: str) -> str:
    """Remove characters that can't print on Windows console (cp1252)."""
    return text.encode("ascii", errors="replace").decode("ascii")


def log(msg: str):
    print(_safe(msg))


def load_used_videos() -> dict:
    if os.path.exists(USED_VIDEOS_FILE):
        try:
            with open(USED_VIDEOS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def mark_video_used(video_id: str, title: str):
    """Saves a video ID so it is never reused."""
    used = load_used_videos()
    used[video_id] = {"title": title, "used_at": datetime.now().isoformat()}
    with open(USED_VIDEOS_FILE, "w") as f:
        json.dump(used, f, indent=2)
    log(f"[VideoFinder] Marked as used: {video_id} -- '{title[:50]}'")


def find_viral_video(niche: str = "finance", max_results: int = 15) -> dict:
    """
    Searches YouTube for a trending long-form video.
    Returns the best unused candidate (500K+ views, <2 years old, 10+ min).
    Falls back to relaxed criteria if strict filter finds nothing.
    """
    candidates = _search(niche, max_results)
    if candidates:
        return candidates[0]
    return {}


def find_viral_videos(niche: str = "finance", max_results: int = 15) -> list:
    """Returns top N candidates so the pipeline can retry if one fails to download."""
    return _search(niche, max_results)


def _search(niche: str, max_results: int) -> list:
    used = load_used_videos()
    log(f"[VideoFinder] {len(used)} videos already used, will skip them.")

    suffix = random.choice(SEARCH_SUFFIXES)
    search_query = f"ytsearch{max_results}:{niche} {suffix}"
    log(f"[VideoFinder] Searching: '{niche} {suffix}'...")

    cutoff = (datetime.now() - timedelta(days=MAX_AGE_DAYS)).strftime("%Y%m%d")

    ydl_opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}

    candidates = []
    last_error = None

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            entries = info.get("entries", []) if info else []

            for entry in entries:
                if not entry:
                    continue

                video_id   = entry.get("id") or ""
                duration   = entry.get("duration") or 0
                view_count = entry.get("view_count") or 0
                upload_date = entry.get("upload_date") or ""
                title = _safe(entry.get("title", ""))[:60]

                if video_id in used:
                    log(f"  [SKIP] Already used: {title}")
                    continue
                if duration < MIN_DURATION:
                    log(f"  [SKIP] Too short ({duration//60}min): {title}")
                    continue
                if view_count < MIN_VIEWS:
                    log(f"  [SKIP] Too few views ({view_count:,}): {title}")
                    continue
                if upload_date and upload_date < cutoff:
                    log(f"  [SKIP] Too old ({upload_date}): {title}")
                    continue

                url = entry.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}"
                candidates.append({
                    "url": url,
                    "title": title,
                    "duration": duration,
                    "view_count": view_count,
                    "upload_date": upload_date,
                    "id": video_id,
                })

    except Exception as e:
        last_error = str(e)
        log(f"[VideoFinder] Search error: {e}")

    # Relaxed fallback if strict filter returns nothing
    if not candidates:
        log("[VideoFinder] No trending videos found -- relaxing filters...")
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(search_query, download=False)
                entries = info.get("entries", []) if info else []
                for entry in entries:
                    if not entry:
                        continue
                    vid_id = entry.get("id") or ""
                    if vid_id in used:
                        continue
                    dur = entry.get("duration") or 0
                    views = entry.get("view_count") or 0
                    if dur >= 300 and views >= 100_000:
                        url = entry.get("webpage_url") or f"https://www.youtube.com/watch?v={vid_id}"
                        candidates.append({
                            "url": url,
                            "title": _safe(entry.get("title", ""))[:60],
                            "duration": dur,
                            "view_count": views,
                            "upload_date": entry.get("upload_date", ""),
                            "id": vid_id,
                        })
        except Exception as e:
            last_error = str(e)
            log(f"[VideoFinder] Fallback error: {e}")

    if not candidates:
        log("[VideoFinder] No suitable videos found.")
        if last_error:
            raise Exception(f"YouTube search failed: {last_error}")
        return []

    # Sort by views descending — highest viewed = most trending
    candidates.sort(key=lambda x: x["view_count"], reverse=True)

    top = candidates[:TOP_N]
    for i, c in enumerate(top):
        log(f"[VideoFinder] Candidate {i+1}: '{c['title']}' ({c['view_count']:,} views | {c['duration']//60}min)")

    return top
