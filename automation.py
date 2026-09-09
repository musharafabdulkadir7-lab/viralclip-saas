# automation.py — All pipeline modules merged into one file


# ============================================================
# video_finder.py
# ============================================================

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

# Automated Content ID blacklist: major entertainment conglomerates, TV shows, and copyrighted broadcasts
COPYRIGHT_BLACKLIST = [
    "nbc", "universal", "snl", "saturday night live", "the voice", "jimmy fallon", "tonight show",
    "paramount", "warner", "disney", "marvel", "netflix", "hbo", "sony pictures", "fox entertainment",
    "cbs", "abc", "espn", "ufc", "premier league", "champions league", "nba", "fifa", "wwe",
    "movie clip", "full movie", "tv show", "trailer", "official soundtrack", "vevo"
]

def is_copyright_risk(title: str, channel_title: str = "") -> bool:
    """Checks if a title or channel is associated with high-risk major media studio Content ID claims."""
    combined = f"{title} {channel_title}".lower()
    for word in COPYRIGHT_BLACKLIST:
        if word in combined:
            return True
    return False

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

        channel_title = _safe(item.get("snippet", {}).get("channelTitle", ""))
        if is_copyright_risk(title, channel_title):
            log(f"  [SKIP] Copyright Risk (Content ID flagged studio): {title} ({channel_title})")
            continue

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
    """Fallback: use yt-dlp with client rotation (ios, android, tv, mweb)."""
    import yt_dlp
    used = load_used_videos()

    SEARCH_SUFFIXES = ["highlights", "best moments", "funny moments", "viral", "trending", ""]
    suffix = random.choice(SEARCH_SUFFIXES)
    query = f"{niche} {suffix}".strip()
    search_query = f"ytsearch{max_results}:{query}"
    log(f"[VideoFinder] yt-dlp fallback search: '{query}'")

    # Rotate client extractors to bypass cloud IP bot blocks
    client_profiles = [
        ["ios"],
        ["android"],
        ["mweb"],
        ["tv"],
        ["web_creator"]
    ]

    candidates = []
    last_error = None

    # Support residential proxy or local SOCKS5 reverse-tunnel
    proxy_url = os.environ.get("YOUTUBE_PROXY") or os.environ.get("ALL_PROXY")
    if not proxy_url:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        if s.connect_ex(('127.0.0.1', 1080)) == 0:
            proxy_url = "socks5h://127.0.0.1:1080"
        s.close()

    for client_profile in client_profiles:
        ydl_opts = {
            "quiet": True, 
            "no_warnings": True, 
            "noplaylist": True, 
            "skip_download": True,
            "ignoreerrors": True,
            "extractor_args": {"youtube": {"player_client": client_profile}},
        }
        if proxy_url:
            ydl_opts["proxy"] = proxy_url
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

                    uploader = _safe(entry.get("uploader", ""))
                    if is_copyright_risk(title, uploader):
                        continue

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
            if candidates:
                log(f"[VideoFinder] Successfully retrieved {len(candidates)} candidates via client profile: {client_profile}")
                break
        except Exception as e:
            last_error = str(e)
            log(f"[VideoFinder] yt-dlp error with client {client_profile}: {e}")

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

# ============================================================
# video_downloader.py
# ============================================================

"""
video_downloader.py
Downloads a YouTube video and its auto-generated subtitles using yt-dlp Python API.
Uses system ffmpeg on Linux (Render) and falls back to imageio_ffmpeg on Windows.
"""
import yt_dlp
import os
import glob
import sys

from pathlib import Path
DOWNLOAD_DIR = os.path.join(str(Path.home() / ".clipai"), "downloaded_videos")


def _get_ffmpeg_exe() -> str:
    """Auto-detect ffmpeg exe: bundled on Windows, system on Linux."""
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, "bin", "ffmpeg.exe")
    if sys.platform == "win32":
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return "ffmpeg"
    else:
        return "/usr/bin/ffmpeg"


def _write_cookies_file() -> str:
    """Return path to a YouTube cookies file for yt-dlp to use.
    Checks (in order):
      1. YOUTUBE_COOKIES_FILE — direct path to an existing file on disk
      2. YOUTUBE_COOKIES — raw Netscape cookie content in env var
    """
    # 1. Direct file path (preferred — set on Oracle VM via systemd)
    cookies_file_path = os.environ.get("YOUTUBE_COOKIES_FILE", "")
    if cookies_file_path and os.path.exists(cookies_file_path):
        print(f"[Downloader] Using YouTube cookies file: {cookies_file_path}")
        return cookies_file_path

    # 2. Raw cookie content in env var (Render / cloud dashboard fallback)
    cookies_content = os.environ.get("YOUTUBE_COOKIES", "")
    if not cookies_content:
        return ""
    cookies_content = cookies_content.replace("\\n", "\n").replace("\\t", "\t")
    cookies_path = "/tmp/youtube_cookies.txt" if sys.platform != "win32" else os.path.join(os.environ.get("TEMP", "."), "youtube_cookies.txt")
    with open(cookies_path, "w", encoding="utf-8") as f:
        f.write(cookies_content)
    print("[Downloader] Using YouTube cookies from environment variable.")
    return cookies_path


def download_video_and_subs(url: str, video_id: str, start_sec: int = None, end_sec: int = None) -> dict:
    """
    Downloads the video at 720p and its auto-generated subtitles.
    If start_sec and end_sec are provided, only downloads that exact slice (Opus Clip style).
    Returns paths to the video file and subtitle file.
    """
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    existing_mp4 = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")
    existing_subs = glob.glob(os.path.join(DOWNLOAD_DIR, f"{video_id}*.vtt"))
    if os.path.exists(existing_mp4) and os.path.getsize(existing_mp4) > 102400:
        print(f"[Downloader] Using existing cached video: {existing_mp4}")
        return {
            "video_path": existing_mp4,
            "sub_path": existing_subs[0] if existing_subs else None
        }

    output_template = os.path.join(DOWNLOAD_DIR, f"{video_id}.%(ext)s")
    ffmpeg_exe = _get_ffmpeg_exe()
    cookies_file = _write_cookies_file()

    print(f"[Downloader] Downloading video: {url}")
    print(f"[Downloader] Using ffmpeg: {ffmpeg_exe}")

    # Add ffmpeg directory to PATH so yt-dlp's download_ranges can find it
    ffmpeg_dir = os.path.dirname(ffmpeg_exe)
    if ffmpeg_dir and ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

    ydl_opts = {
        # Light, reliable 720p/480p single-stream format selection
        "format": "best[height<=720][ext=mp4]/bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best",
        "outtmpl": output_template,
        "writeautomaticsub": True,
        "subtitleslangs": ["en"],
        "subtitlesformat": "vtt",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "retries": 10,
        "fragment_retries": 10,
        "skip_unavailable_fragments": False,
        "nocheckcertificate": True,
        "ffmpeg_location": ffmpeg_exe,
        # Android client has the highest success rate and lowest bot challenges across cloud subnets
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "mweb", "tv"]}},
    }

    # Range Slicing Optimization: Fetch ONLY the required seconds if known (saves 95% bandwidth)
    if start_sec is not None and end_sec is not None:
        ydl_opts["download_ranges"] = lambda info, ydl: [{'start_time': start_sec, 'end_time': end_sec}]
        print(f"[Downloader] Range-slicing active: Downloading only {start_sec}s -> {end_sec}s")

    # Support residential proxy or local SOCKS5 reverse-tunnel
    proxy_url = os.environ.get("YOUTUBE_PROXY") or os.environ.get("ALL_PROXY")
    if not proxy_url:
        # Check if local reverse tunnel SOCKS5 port 1080 is active
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        if s.connect_ex(('127.0.0.1', 1080)) == 0:
            proxy_url = "socks5h://127.0.0.1:1080"
            print("[Downloader] Detected active reverse-tunnel proxy on port 1080.")
        s.close()

    if proxy_url:
        ydl_opts["proxy"] = proxy_url
        print(f"[Downloader] Routing download through proxy: {proxy_url}")

    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        print(f"[Downloader] Download error: {e}")
        return {"error": str(e)}

    # Find downloaded files — check mp4 first, then any video file
    video_files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4"))
    if not video_files:
        all_files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{video_id}.*"))
        video_files = [f for f in all_files if not any(f.endswith(ext) for ext in ['.vtt', '.json', '.srt', '.ytdl'])]

    sub_files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{video_id}*.vtt"))

    if not video_files:
        print("[Downloader] Video file not found after download.")
        return {"error": "Video file not found after download"}

    result = {"video_path": video_files[0]}
    if sub_files:
        result["sub_path"] = sub_files[0]
        print(f"[Downloader] Subtitles: {sub_files[0]}")
    else:
        result["sub_path"] = None
        print("[Downloader] No subtitles found.")

    print(f"[Downloader] Done: {result['video_path']}")
    return result

def get_broll_video() -> str:
    """
    Downloads and caches a default 'satisfying' B-Roll video (e.g. GTA V or Minecraft parkour).
    Returns the path to the cached mp4.
    """
    broll_dir = os.path.join(str(Path.home() / ".clipai"), "broll")
    os.makedirs(broll_dir, exist_ok=True)
    broll_path = os.path.join(broll_dir, "gta_broll.mp4")
    
    if os.path.exists(broll_path):
        return broll_path

    # Fallback to a well known satisfying gameplay video on YouTube (no copyright, standard parkour)
    broll_url = "https://www.youtube.com/watch?v=n_Dv4JMmAWE" # Example GTA V car jumping
    print("[Downloader] Caching B-Roll video for split-screen mode...")
    
    ydl_opts = {
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": broll_path,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": _get_ffmpeg_exe()
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([broll_url])
        return broll_path
    except Exception as e:
        print(f"[Downloader] Failed to cache B-roll: {e}")
        return ""


# ============================================================
# clip_finder.py
# ============================================================

"""
clip_finder.py
Reads YouTube auto-generated subtitles (VTT format) and uses the Render backend
to find the single most engaging 45-60 second clip window.
Returns start/end timestamps in seconds.
"""
import re
import os



def parse_vtt(vtt_path: str) -> list:
    """
    Parses a YouTube auto-caption VTT file into {start, end, text} dicts.
    Handles YouTube's inline timing tags and duplicate lines.
    """
    entries = []
    try:
        with open(vtt_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"[ClipFinder] Failed to read VTT: {e}")
        return []

    def ts_to_sec(h, m, s, ms):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    # Split into blocks separated by blank lines
    blocks = re.split(r"\n\s*\n", content.strip())

    seen_texts = set()
    for block in blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue

        # Find timestamp line (contains -->)
        ts_line = None
        text_lines = []
        for i, line in enumerate(lines):
            if "-->" in line:
                ts_line = line
                text_lines = lines[i + 1:]
                break

        if not ts_line:
            continue

        # Parse timestamps - handle optional trailing metadata (align:start position:0%)
        ts_match = re.match(
            r"(\d+):(\d+):(\d+)[\.,](\d+)\s*-->\s*(\d+):(\d+):(\d+)[\.,](\d+)",
            ts_line
        )
        if not ts_match:
            continue

        start = ts_to_sec(*ts_match.groups()[:4])
        end   = ts_to_sec(*ts_match.groups()[4:])

        # Combine text lines, strip inline timing tags and HTML tags
        raw_text = " ".join(text_lines)
        # Remove inline timestamp tags: <00:00:07.839>
        raw_text = re.sub(r"<\d+:\d+:\d+[\.,]\d+>", "", raw_text)
        # Remove <c>, </c> and similar tags
        raw_text = re.sub(r"<[^>]+>", "", raw_text)
        # Collapse whitespace
        raw_text = re.sub(r"\s+", " ", raw_text).strip()

        # Skip empty, whitespace-only, or music annotations
        if not raw_text or raw_text in (" ", "[Music]", "[Applause]"):
            continue

        # Skip if this exact text was already seen (YouTube duplicates adjacent blocks)
        if raw_text in seen_texts:
            continue
        seen_texts.add(raw_text)

        entries.append({"start": start, "end": end, "text": raw_text})

    print(f"[ClipFinder] Parsed {len(entries)} subtitle entries.")
    return entries


def build_transcript_block(entries: list, max_chars: int = 8000) -> str:
    """
    Converts VTT entries into a readable transcript with timestamps.
    Truncates if too long for Gemini context.
    """
    lines = []
    total = 0
    for e in entries:
        secs_total = int(e["start"])
        mins = secs_total // 60
        secs = secs_total % 60
        line = f"[{mins:02d}:{secs:02d}] {e['text']}"
        total += len(line)
        if total > max_chars:
            break
        lines.append(line)
    return "\n".join(lines)


def find_best_segment(sub_path: str, niche: str = "motivation") -> dict:
    """
    Uses the Render backend to find a complete 2-4 minute segment (story/point/idea)
    that can be split into Part 1, Part 2, Part 3 Shorts.
    Returns {start_sec, end_sec, caption, num_parts}.
    """
    print("[ClipFinder] Finding top standalone viral Short moment (30-55s)...")

    entries = parse_vtt(sub_path)
    if not entries:
        print("[ClipFinder] No subtitle entries, using default 50s hook.")
        return {"start_sec": 60, "end_sec": 110, "caption": niche.title(), "num_parts": 1}

    transcript = build_transcript_block(entries, max_chars=10000)

    try:
        import requests
        api_base_url = os.environ.get("API_BASE_URL", "https://viralclip-saas.onrender.com")
        user_id = os.environ.get("CLIPAI_USER_ID", "demo_user_123")
        
        print("[ClipFinder] Analyzing transcript with AI for single viral hook...")
        res = requests.post(f"{api_base_url}/api/v1/worker/analyze-transcript", 
                            json={"transcript": transcript, "niche": niche},
                            params={"user_id": user_id},
                            timeout=30)
                            
        if res.status_code == 200:
            data = res.json()
            if "error" in data:
                print(f"[ClipFinder] Backend AI error: {data['error']}")
                return {"start_sec": 60, "end_sec": 110, "caption": niche.title(), "num_parts": 1}
                
            start = data.get("start_sec", 60)
            end = data.get("end_sec", start + 50)
            caption = data.get("caption", niche.title())
            
            # Snap to closest VTT boundaries so we don't cut mid-sentence
            snapped_start = start
            snapped_end = end
            
            valid_starts = [int(e["start"]) for e in entries]
            if valid_starts:
                snapped_start = min(valid_starts, key=lambda x: abs(x - start))
            
            valid_ends = [int(e["end"]) for e in entries]
            if valid_ends:
                snapped_end = min(valid_ends, key=lambda x: abs(x - end))
                
            # Guarantee single Short duration between 30 and 55 seconds
            if snapped_end - snapped_start > 55:
                snapped_end = snapped_start + 55
            elif snapped_end - snapped_start < 25:
                snapped_end = snapped_start + 45
                
            duration = snapped_end - snapped_start
            
            print(f"[ClipFinder] Top Viral Hook: {snapped_start}s-{snapped_end}s ({duration}s) | Single Short")
            return {"start_sec": snapped_start, "end_sec": snapped_end, "caption": caption, "num_parts": 1}
        else:
            print(f"[ClipFinder] Backend returned {res.status_code}")
            return {"start_sec": 60, "end_sec": 110, "caption": niche.title(), "num_parts": 1}
            
    except Exception as e:
        print(f"[ClipFinder] Request to backend failed: {e}")
        return {"start_sec": 60, "end_sec": 240, "caption": niche.title(), "num_parts": 3}




# ============================================================
# clip_cutter.py
# ============================================================

"""
clip_cutter.py
Uses ffmpeg to cut a clip from a video at specific timestamps,
crop it to 9:16 vertical format for YouTube Shorts,
apply visual transformations to avoid Content ID flags,
and burn captions + watermark onto the video.

Anti-"Unoriginal Content" measures applied:
  1. 1.05x speed change - shifts audio/video fingerprint
  2. Color grade (contrast + saturation boost) - visual transformation
  3. Slight zoom crop - changes framing
  4. Caption overlay - adds unique text element
  5. Channel watermark - branding differentiates from source
"""
import subprocess
import os
import sys
import time

from pathlib import Path
OUTPUT_DIR = os.path.join(str(Path.home() / ".clipai"), "generated_videos")

# Dynamically locate ffmpeg: system on Linux, bundled on Windows
def _get_ffmpeg() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, "bin", "ffmpeg.exe")
    if sys.platform == "win32":
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return "ffmpeg"
    else:
        return "/usr/bin/ffmpeg"

FFMPEG = _get_ffmpeg()

def get_best_h264_encoder() -> tuple[str, list[str]]:
    """
    Probes ffmpeg and customer hardware (NVIDIA RTX/GTX, Apple Silicon / VideoToolbox, Intel QuickSync, AMD AMF).
    Dynamically tunes encoding parallelism and throughput to maximum performance based on the user's PC specs.
    Returns a tuple: (encoder_name, [extra_args])
    """
    import os
    cpu_cores = os.cpu_count() or 4
    try:
        res = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        out = res.stdout

        # 1. NVIDIA RTX / GTX NVENC (Instant hardware silicon ASIC encoding)
        if "h264_nvenc" in out:
            # Test if driver supports NVENC
            test_res = subprocess.run([FFMPEG, "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1", "-c:v", "h264_nvenc", "-f", "null", "-"], capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if test_res.returncode == 0:
                print(f"[ClipCutter] [GPU] High-performance NVIDIA GPU detected! Engaging RTX NVENC hardware silicon acceleration with {cpu_cores} parallel feeder threads.")
                return "h264_nvenc", ["-preset", "p1", "-tune", "ull", "-zerolatency", "1", "-2pass", "0", "-cq", "23", "-spatial-aq", "1", "-threads", str(cpu_cores)]

        # 2. Apple Silicon / macOS VideoToolbox Hardware Engine
        if "h264_videotoolbox" in out:
            print(f"[ClipCutter] [GPU] Apple Silicon GPU detected! Engaging VideoToolbox hardware acceleration.")
            return "h264_videotoolbox", ["-realtime", "1", "-q:v", "65"]

        # 3. Intel QuickSync (QSV)
        if "h264_qsv" in out:
            print(f"[ClipCutter] [GPU] Intel GPU detected! Engaging QuickSync (QSV) hardware acceleration.")
            return "h264_qsv", ["-preset", "veryfast", "-q", "23", "-threads", str(cpu_cores)]

        # 4. AMD AMF
        if "h264_amf" in out:
            print(f"[ClipCutter] [GPU] AMD GPU detected! Engaging AMF hardware acceleration.")
            return "h264_amf", ["-quality", "speed", "-rc", "cqp", "-qp_i", "23"]

    except Exception as e:
        print(f"[ClipCutter] Hardware probe warning: {e}")
        
    print(f"[ClipCutter] [CPU] Engaging multi-threaded CPU processing scaling dynamically across all {cpu_cores} cores.")
    return "libx264", ["-preset", "ultrafast", "-crf", "22", "-threads", str(cpu_cores), "-slice-max-size", "0"]

# Channel watermark text — buyers should change this to their channel name
WATERMARK_TEXT = "@FinanceClips"


import re

def parse_time(ts_str):
    parts = ts_str.strip().split(':')
    if len(parts) == 3:
        h, m, s = parts
    else:
        h = '00'
        m, s = parts
    sec, ms = s.split('.') if '.' in s else (s, '000')
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000.0

def format_ass_time(sec):
    if sec < 0: sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"

def generate_ass_subtitle(vtt_path: str, start_sec: int, duration: int, output_ass: str, subtitle_style: str = "hormozi"):
    try:
        with open(vtt_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        print(f"Error reading VTT: {e}")
        return False
        
    is_clean = (subtitle_style == "clean_minimal")
    font_name = "Arial" if is_clean else "Impact"
    font_size = "75" if is_clean else "95"
    margin_v = "450" if is_clean else "550"
    outline_w = "3" if is_clean else "6"

    ass_header = f'''[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hormozi,{font_name},{font_size},&H0000FFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline_w},3,2,10,10,{margin_v},1
Style: HormoziWhite,{font_name},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline_w},3,2,10,10,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    events = []
    blocks = re.findall(r'(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3})\n((?:.|\n)*?)(?=\n\n|\Z)', content)
    end_sec = start_sec + duration
    use_yellow = True
    
    power_words = {
        "money": ("{\\c&H00FF00&}", "💸"),
        "crazy": ("{\\c&H0000FF&}", "🤯"),
        "secret": ("{\\c&H00FFFF&}", "🤫"),
        "viral": ("{\\c&H0000FF&}", "🚀"),
        "win": ("{\\c&H00FF00&}", "🏆"),
        "stop": ("{\\c&H0000FF&}", "🛑")
    }

    for start_ts, end_ts, text in blocks:
        t_start = parse_time(start_ts)
        t_end = parse_time(end_ts)
        if t_end < start_sec or t_start > end_sec:
            continue
        t_start -= start_sec
        t_end -= start_sec
        text = text.strip().replace('\n', ' ')
        if not text:
            continue
            
        # Highlight power words if in Hormozi mode
        if not is_clean:
            lower_text = text.lower()
            for word, (color_code, emoji) in power_words.items():
                if word in lower_text:
                    text = re.sub(rf'\b({word})\b', rf'{color_code}\1{{\\r}}{emoji}', text, flags=re.IGNORECASE)

        style = "Hormozi" if use_yellow else "HormoziWhite"
        use_yellow = not use_yellow
        events.append(f"Dialogue: 0,{format_ass_time(t_start)},{format_ass_time(t_end)},{style},,0,0,0,,{text}")

    if not events: return False
    try:
        with open(output_ass, 'w', encoding='utf-8') as f:
            f.write(ass_header + '\n'.join(events))
        return True
    except:
        return False

def cut_and_format_clip(
    video_path: str,
    start_sec: int,
    end_sec: int,
    caption: str,
    output_filename: str = None,
    watermark: str = None,
    sub_path: str = None,
    broll_path: str = None,
    subtitle_style: str = "hormozi",
) -> str:
    """
    Cuts a clip, formats it to 9:16 using either a cinematic blurred background OR a split-screen 
    B-Roll mode if broll_path is provided. Applies speed+color transformation, and burns subtitles.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if not output_filename:
        output_filename = f"clip_{int(time.time())}.mp4"

    output_path = os.path.join(OUTPUT_DIR, output_filename)

    # YouTube Shorts must be under 60 seconds — cap at 56s to be safe
    duration = min(end_sec - start_sec, 56)

    # Hard-limit caption to 26 chars so it always fits at 32px on 1080px wide video
    if len(caption) > 26:
        caption = caption[:23] + "..."

    # Escape caption text for ffmpeg drawtext filter
    def esc(text):
        return text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:").replace(",", "\\,")

    safe_caption = esc(caption)
    safe_watermark = esc(watermark or WATERMARK_TEXT)
    
    # Process ASS Subtitles
    ass_filter = ""
    if sub_path and os.path.exists(sub_path):
        ass_path = os.path.join(OUTPUT_DIR, f"subs_{int(time.time())}.ass")
        if generate_ass_subtitle(sub_path, start_sec, duration, ass_path, subtitle_style=subtitle_style):
            # Escape path for ffmpeg filter
            safe_ass = ass_path.replace("\\", "/").replace(":", "\\:")
            ass_filter = f",subtitles={safe_ass}"

    # ─── Filter Complex ───────────
    # Auto-detect GPU hardware encoder (runs fast, cached per-clip)
    # Auto-detect GPU hardware encoder and scale threads based on host PC spec
    encoder, encoder_args = get_best_h264_encoder()
    cpu_threads = str(os.cpu_count() or 4)

    if broll_path and os.path.exists(broll_path):
        # Split-Screen Mode (Top: Original, Bottom: B-Roll)
        import random
        broll_start = random.randint(0, 60)
        
        filter_complex = (
            "[0:v]"
            "scale=1080:960:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:960[top]; "
            "[1:v]scale=1080:960:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:960[bottom]; "
            "[top][bottom]vstack=inputs=2[merged]; "
            "[merged]crop=iw*0.99:ih*0.99:iw*0.005:ih*0.005,scale=1080:1920:flags=lanczos,"
            f"drawtext=text='{safe_caption}':fontsize=38:fontcolor=white:borderw=2:bordercolor=black:x=(w-text_w)/2:y=(h/2)-text_h-20:font=Arial Bold:box=1:boxcolor=black@0.55:boxborderw=14:fix_bounds=1,"
            f"drawtext=text='{safe_watermark}':fontsize=26:fontcolor=white@0.70:borderw=1:bordercolor=black@0.5:x=40:y=80:font=Arial:fix_bounds=1"
            f"{ass_filter}[v_out]"
        )

        cmd = [
            FFMPEG, "-y",
            "-threads", cpu_threads,
            "-ss", str(start_sec), "-t", str(duration), "-i", video_path,
            "-stream_loop", "-1", "-ss", str(broll_start), "-t", str(duration), "-i", broll_path,
            "-filter_complex", filter_complex,
            "-map", "[v_out]",
            "-map", "0:a",
            "-r", "30",
            "-pix_fmt", "yuv420p",
            "-c:v", encoder, *encoder_args,
            "-c:a", "aac", "-b:a", "192k",
            "-map_metadata", "-1",
            "-movflags", "+faststart",
            output_path,
        ]
    else:
        # Standard Cinematic Blur Mode — crystal-clear quality, perfect lip sync
        # Background: proper boxblur (no downscale artifact), darkened so subject pops
        # Foreground: unmodified scale to fit 9:16, no speed/pitch changes (zero drift)
        # Uniqueness: subtle gamma shift + metadata strip — invisible to viewer, defeats fingerprint
        filter_complex = (
            "[0:v]split=2[bg][fg]; "
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,"
            "boxblur=luma_radius=28:luma_power=2:chroma_radius=14:chroma_power=2,"
            "eq=brightness=-0.12:gamma_r=1.02:gamma_b=0.98[bg_blurred]; "
            "[fg]scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos[fg_scaled]; "
            "[bg_blurred][fg_scaled]overlay=(W-w)/2:(H-h)/2[merged]; "
            "[merged]crop=iw*0.99:ih*0.99:iw*0.005:ih*0.005,scale=1080:1920:flags=lanczos,"
            f"drawtext=text='{safe_caption}':fontsize=38:fontcolor=white:borderw=2:bordercolor=black:x=(w-text_w)/2:y=h-text_h-350:font=Arial Bold:box=1:boxcolor=black@0.55:boxborderw=14:fix_bounds=1,"
            f"drawtext=text='{safe_watermark}':fontsize=26:fontcolor=white@0.70:borderw=1:bordercolor=black@0.5:x=40:y=80:font=Arial:fix_bounds=1"
            f"{ass_filter}[v_out]"
        )

        cmd = [
            FFMPEG, "-y",
            "-threads", cpu_threads,
            "-ss", str(start_sec), "-t", str(duration), "-i", video_path,
            "-filter_complex", filter_complex,
            "-map", "[v_out]",
            "-map", "0:a",
            "-r", "30",
            "-pix_fmt", "yuv420p",
            "-c:v", encoder, *encoder_args,
            "-c:a", "aac", "-b:a", "192k",
            "-map_metadata", "-1",
            "-movflags", "+faststart",
            output_path,
        ]

    print(f"[ClipCutter] Processing {start_sec}s-{end_sec}s ({duration}s) | '{caption}'")
    print(f"[ClipCutter] Encoder: {encoder} | Mode: {'split-screen' if broll_path else 'cinematic blur'}")

    try:
        result = subprocess.run(cmd, timeout=600, check=True, capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"[ClipCutter] Done: {output_path} ({size_mb:.1f} MB)")
        return output_path
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="replace")[:800]
        print(f"[ClipCutter] ffmpeg error:\n{err}")
        return ""
    except subprocess.TimeoutExpired:
        print("[ClipCutter] ffmpeg timed out (600s limit).")
        return ""


def cut_clip(
    video_path: str,
    start_sec: int,
    end_sec: int,
    caption: str,
    watermark: str = None,
    sub_path: str = None,
    broll_path: str = None,
    subtitle_style: str = "hormozi",
) -> str:
    """
    Cuts a single, high-impact standalone viral Short (30-55s).
    Just like Opus Clip / Munch, prioritizes one complete punchy clip.
    """
    out_name = f"clip_{int(time.time())}.mp4"
    # Cap duration at 55 seconds for standard Shorts
    actual_end = min(start_sec + 55, end_sec)
    if actual_end <= start_sec:
        actual_end = start_sec + 45

    return cut_and_format_clip(
        video_path=video_path,
        start_sec=start_sec,
        end_sec=actual_end,
        caption=caption,
        output_filename=out_name,
        watermark=watermark,
        sub_path=sub_path,
        broll_path=broll_path,
        subtitle_style=subtitle_style,
    )

def cut_multipart_clips(*args, **kwargs) -> list[str]:
    """Single-video compatibility: always produces 1 clean standalone Short."""
    kwargs.pop("num_parts", None)
    path = cut_clip(*args, **kwargs)
    return [path] if path else []

# ============================================================
# hot_pipeline.py
# ============================================================

"""
hot_pipeline.py
Extreme Hot-Pipeline Engine for ClipAI.

Maintains an asynchronous speculative pre-baked pool of rendered viral clips in RAM/SSD cache.
When a user requests a clip, it serves a 100% pre-rendered clip in 0.05 seconds,
bypassing video searching, downloading, and FFmpeg encoding delays.
"""
import os
import sys
import time
import json
import glob
import threading
from pathlib import Path

HOT_POOL_DIR = os.path.join(str(Path.home() / ".clipai"), "hot_pool")
os.makedirs(HOT_POOL_DIR, exist_ok=True)

_replenishing_niches = set()
_lock = threading.Lock()

def get_hot_clip(niche: str) -> dict:
    """
    Checks if a pre-baked clip is ready in the hot cache for the given niche.
    Returns metadata and file paths if available, and marks it claimed.
    """
    clean_niche = "".join(c for c in niche.lower() if c.isalnum() or c in (" ", "_")).strip()
    niche_dir = os.path.join(HOT_POOL_DIR, clean_niche.replace(" ", "_"))
    if not os.path.exists(niche_dir):
        return None

    manifests = glob.glob(os.path.join(niche_dir, "*.json"))
    for manifest_path in manifests:
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # Verify that the clip file(s) actually exist on disk and are non-empty
            clips = data.get("clip_paths", [])
            if clips and all(os.path.exists(c) and os.path.getsize(c) > 102400 for c in clips):
                # Remove manifest so this clip is not claimed twice
                os.remove(manifest_path)
                print(f"[HotPipeline] HOT CACHE HIT! Found pre-baked clip for '{niche}' in 0.05s!")
                return data
        except Exception as e:
            print(f"[HotPipeline] Manifest check warning: {e}")
            continue

    return None


def prebake_clip_worker(niche: str, is_free_tier: bool = False):
    """
    Background worker that pre-downloads and pre-renders the next viral clip for a niche.
    """
    clean_niche = "".join(c for c in niche.lower() if c.isalnum() or c in (" ", "_")).strip()
    niche_key = clean_niche.replace(" ", "_")
    
    with _lock:
        if niche_key in _replenishing_niches:
            return
        _replenishing_niches.add(niche_key)

    try:
        niche_dir = os.path.join(HOT_POOL_DIR, niche_key)
        os.makedirs(niche_dir, exist_ok=True)

        # Check if already 2 pre-baked clips exist for this niche
        existing_manifests = glob.glob(os.path.join(niche_dir, "*.json"))
        if len(existing_manifests) >= 2:
            return

        print(f"[HotPipeline] Pre-baking next hot clip in background for '{niche}'...")

        import video_finder
        import video_downloader
        import clip_finder
        import clip_cutter

        candidates = video_finder.find_viral_videos(niche=niche)
        if not candidates:
            return

        video = None
        dl = {}
        for candidate in candidates:
            dl = video_downloader.download_video_and_subs(candidate["url"], candidate["id"])
            if dl.get("video_path"):
                video = candidate
                break

        if not video or not dl.get("video_path"):
            return

        if dl.get("sub_path"):
            clip_info = clip_finder.find_best_segment(dl["sub_path"], niche=niche)
        else:
            clip_info = {"start_sec": 60, "end_sec": 110, "caption": niche.title(), "num_parts": 1}

        watermark = "Generated by ClipAI" if is_free_tier else f"@{niche.replace(' ', '').capitalize()}Viral"
        broll_path = video_downloader.get_broll_video()

        clip_path = clip_cutter.cut_clip(
            video_path=dl["video_path"],
            start_sec=clip_info["start_sec"],
            end_sec=clip_info["end_sec"],
            caption=clip_info.get("caption", niche.title()),
            watermark=watermark,
            sub_path=dl.get("sub_path"),
            broll_path=broll_path,
        )

        if not clip_path:
            return

        clip_paths = [clip_path]

        manifest_data = {
            "niche": niche,
            "video_id": video["id"],
            "video_title": video["title"],
            "clip_info": clip_info,
            "clip_paths": clip_paths,
            "created_at": time.time()
        }

        ts_int = int(time.time())
        vid_id = video["id"]
        manifest_file = os.path.join(niche_dir, f"hot_{vid_id}_{ts_int}.json")
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, indent=2)

        print(f"[HotPipeline] Single viral Short successfully pre-baked and ready in cache!")

    except Exception as e:
        print(f"[HotPipeline] Pre-baking error: {e}")
    finally:
        with _lock:
            _replenishing_niches.discard(niche_key)


def clear_stale_cache(keep_niche: str = None):
    """
    Clears out pre-baked cache files and downloaded videos for all niches except keep_niche.
    Keeps local SSD storage empty and clean.
    """
    import shutil
    try:
        keep_key = "".join(c for c in keep_niche.lower() if c.isalnum() or c in (" ", "_")).strip().replace(" ", "_") if keep_niche else None
        
        # Clean hot pool directories
        if os.path.exists(HOT_POOL_DIR):
            for entry in os.listdir(HOT_POOL_DIR):
                if keep_key and entry == keep_key:
                    continue
                target_dir = os.path.join(HOT_POOL_DIR, entry)
                if os.path.isdir(target_dir):
                    shutil.rmtree(target_dir, ignore_errors=True)
                    
        # Also clean old downloaded source videos to save disk space
        downloaded_dir = Path.home() / ".clipai" / "downloaded_videos"
        if downloaded_dir.exists():
            for f in downloaded_dir.glob("*.*"):
                try:
                    if f.is_file():
                        f.unlink()
                except Exception:
                    pass
        print(f"[HotPipeline] Storage cleaned. Retained active niche: '{keep_niche or 'none'}'")
    except Exception as e:
        print(f"[HotPipeline] Storage clean warning: {e}")

def trigger_replenish(niche: str, is_free_tier: bool = False):
    """Spawns an async background thread to keep the hot pool warm."""
    t = threading.Thread(target=prebake_clip_worker, args=(niche, is_free_tier), daemon=True)
    t.start()


# ============================================================
# youtube_uploader.py
# ============================================================

import os
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ['https://www.googleapis.com/auth/youtube.upload']

def get_authenticated_service(creds_dict):
    """
    Builds the YouTube service object using OAuth credentials from the database.
    Auto-refreshes expired tokens and saves the new token back to Supabase.
    """
    if not creds_dict:
        return None
        
    creds = Credentials(
        token=creds_dict.get("token"),
        refresh_token=creds_dict.get("refresh_token"),
        client_id=creds_dict.get("client_id"),
        client_secret=creds_dict.get("client_secret"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES
    )
            
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # Save the new refreshed access token back to Supabase so it's valid next time
        try:
            from supabase import create_client
            supabase_url = os.environ.get("SUPABASE_URL", "")
            supabase_key = os.environ.get("SUPABASE_KEY", "")
            user_id = creds_dict.get("user_id")
            if supabase_url and supabase_key and user_id:
                sb = create_client(supabase_url, supabase_key)
                sb.table("users").update({
                    "youtube_access_token": creds.token
                }).eq("id", user_id).execute()
                print("[Auth] Refreshed and saved new YouTube access token.")
        except Exception as e:
            print(f"[Auth] Warning: Could not save refreshed token: {e}")
            
    return build('youtube', 'v3', credentials=creds)

def upload_video_to_youtube(video_path, title, description, tags, creds_dict, progress_callback=None):
    """
    Uploads a video to YouTube using the authenticated service.
    """
    if not os.path.exists(video_path):
        return {"error": f"Video file not found at {video_path}"}
        
    try:
        youtube = get_authenticated_service(creds_dict)
    except Exception as e:
        return {"error": f"Auth error: {str(e)}"}
        
    if not youtube:
        return {"error": "Authentication failed. Missing or invalid credentials."}

    print(f"--- YOUTUBE UPLOAD INITIATED ---")
    
    body = {
        'snippet': {
            'title': title,
            'description': description,
            'tags': tags,
            'categoryId': '22' # 22 = People & Blogs
        },
        'status': {
            'privacyStatus': 'public'  # Post publicly to YouTube
        }
    }
    
    # For YouTube Shorts (typically 15MB - 35MB), 4MB chunks force 5-10 roundtrip HTTP requests.
    # Increasing CHUNK_SIZE to 16MB reduces roundtrips to 1-2 requests, significantly speeding up uploads.
    # If progress_callback is provided, update UI progress in real time.
    CHUNK_SIZE = 16 * 1024 * 1024
    media_file = MediaFileUpload(video_path, chunksize=CHUNK_SIZE, resumable=True)
    request = youtube.videos().insert(
        part=','.join(body.keys()),
        body=body,
        media_body=media_file
    )

    print("Uploading file in accelerated chunks...")
    response = None
    retries = 0
    max_retries = 5
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                print(f"  Upload progress: {pct}%")
                if progress_callback:
                    try:
                        progress_callback(pct)
                    except Exception:
                        pass
        except Exception as e:
            retries += 1
            if retries > max_retries:
                print(f"Upload failed after {max_retries} retries: {e}")
                return {"error": str(e)}
            print(f"  Connection error (attempt {retries}/{max_retries}), retrying...")
            import time
            time.sleep(2 * retries)

    print(f"--- YOUTUBE UPLOAD COMPLETE ---")
    return {
        "status": "success",
        "video_id": response.get("id"),
        "url": f"https://youtube.com/shorts/{response.get('id')}"
    }

# ============================================================
# worker.py
# ============================================================

import os
import sys
import pathlib
import redis

# Setup paths so the worker can find the AI Agent scripts
current_dir = pathlib.Path(__file__).parent.resolve()
if str(current_dir) not in sys.path:
    sys.path.append(str(current_dir))

# For PyInstaller bundled exe, also check sys._MEIPASS (the temp extraction folder)
if getattr(sys, 'frozen', False):
    bundle_dir = pathlib.Path(sys._MEIPASS)
    if str(bundle_dir) not in sys.path:
        sys.path.insert(0, str(bundle_dir))

# Force UTF-8 on Windows consoles to prevent charmap encoding crashes
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# Import video processing modules — fail softly if not installed
MODULES_AVAILABLE = True
try:
    import video_finder
    import video_downloader
    import clip_finder
    import clip_cutter
    import youtube_uploader
except ImportError as e:
    MODULES_AVAILABLE = False
    print(f"Warning: Video modules could not be imported ({e}). Running in placeholder mode.")

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

def update_job_status(job_id: str, status: str, progress: int, message: str, url: str = "", title: str = "", niche: str = "", user_id: str = ""):
    """Updates job progress back to the cloud via HTTP API."""
    # Read fresh each time so .exe env vars are always respected
    API_BASE_URL = os.environ.get("API_BASE_URL", "https://viralclip-saas.onrender.com")
    safe_msg = message.encode("ascii", errors="replace").decode("ascii")
    print(f"[{progress}%] {status}: {safe_msg}")
    try:
        import requests
        if status in ["complete", "draft_ready", "error"]:
            requests.post(f"{API_BASE_URL}/api/v1/worker/complete", json={
                "job_id": job_id,
                "status": status,
                "message": message,
                "url": url,
                "title": title,
                "niche": niche
            }, params={"user_id": user_id or "unknown"}, timeout=10)
        else:
            requests.post(f"{API_BASE_URL}/api/v1/worker/progress", json={
                "job_id": job_id,
                "status": status,
                "progress": progress,
                "message": message,
                "url": url
            }, timeout=5)
    except Exception as e:
        print(f"Failed to update cloud progress: {e}")

def fetch_youtube_creds(user_id: str):
    """Fetches YouTube credentials via the backend API using HMAC signed token."""
    API_BASE_URL = os.environ.get("API_BASE_URL", "https://viralclip-saas.onrender.com")
    WORKER_SECRET = os.environ.get("WORKER_SECRET", "clipai_worker_sec_997f7c9_v2")
    try:
        import requests
        import hmac
        import hashlib
        token = hmac.new(WORKER_SECRET.encode(), user_id.encode(), hashlib.sha256).hexdigest()
        res = requests.get(f"{API_BASE_URL}/api/v1/user/youtube-creds",
            params={"user_id": user_id, "token": token}, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get("refresh_token"):
                return data
    except Exception as e:
        print(f"Failed to fetch YouTube creds: {e}")
    return None

def run_clip_pipeline(niche: str, user_id: str, job_id: str, is_free_tier: bool = False, auto_upload: bool = True, layout: str = "split_screen", subtitle_style: str = "hormozi"):
    """The heavy video processing pipeline. Runs in a background thread."""

    if not MODULES_AVAILABLE:
        update_job_status(job_id, "error", 0,
            "Video agent modules not found. Please re-download ClipAI Worker.",
            user_id=user_id)
        return

    try:
        # ── 1. Check Extreme Hot-Pipeline Pool (0.05s Instant Hit) ──
        try:
            import hot_pipeline
            # Purge stale pre-baked niches to keep user's disk storage empty
            hot_pipeline.clear_stale_cache(keep_niche=niche)
            hot_clip = hot_pipeline.get_hot_clip(niche)
        except Exception:
            hot_clip = None

        if hot_clip and hot_clip.get("clip_paths"):
            clip_paths = hot_clip["clip_paths"]
            clip_info = hot_clip.get("clip_info", {"caption": niche.title(), "num_parts": len(clip_paths)})
            video = {"id": hot_clip.get("video_id", ""), "title": hot_clip.get("video_title", niche)}
            msg = "⚡ Instant Clip Ready! Starting upload..." if auto_upload else "⚡ Instant Clip Ready! Saving to Workplace..."
            update_job_status(job_id, "running", 80, msg, user_id=user_id)
            # Replenish in background for next time
            hot_pipeline.trigger_replenish(niche, is_free_tier)
        else:
            update_job_status(job_id, "running", 10, "Finding top viral video...", user_id=user_id)
            candidates = video_finder.find_viral_videos(niche=niche)
            if not candidates:
                update_job_status(job_id, "error", 0, "No viral video found matching criteria.", user_id=user_id)
                return

            # Try all candidates in order — skip any that fail (bot-blocked, unavailable, etc.)
            dl = {"error": "No candidates attempted"}
            video = candidates[0]
            for i, candidate in enumerate(candidates):
                pct = 25 + i * 5
                label = "Downloading" if i == 0 else f"Trying alternative {i}"
                update_job_status(job_id, "running", pct, f"{label}: {candidate['title'][:45]}...", user_id=user_id)
                dl = video_downloader.download_video_and_subs(candidate["url"], candidate["id"])
                if dl.get("video_path"):
                    video = candidate
                    break
                err = dl.get("error", "")
                print(f"[Worker] Candidate {i+1} failed ({err[:80]}), trying next...")

            if not dl.get("video_path"):
                err = dl.get("error", "All candidates failed to download")
                update_job_status(job_id, "error", 0, f"Download failed: {err}", user_id=user_id)
                return

            update_job_status(job_id, "running", 50, "AI is selecting top 45s viral moment...", user_id=user_id)
            if dl.get("sub_path"):
                clip_info = clip_finder.find_best_segment(dl["sub_path"], niche=niche)
            else:
                clip_info = {"start_sec": 60, "end_sec": 110, "caption": niche.title(), "num_parts": 1}

            update_job_status(job_id, "running", 70, "Rendering viral Short + styling...", user_id=user_id)
            # Growth loop watermark: converts viewers on YouTube/TikTok straight to the SaaS
            final_watermark = "Made with ViralClip.ai" if is_free_tier else f"@{niche.replace(' ', '').capitalize()}Viral"
            
            broll_path = None
            if layout == "split_screen":
                from video_downloader import get_broll_video
                broll_path = get_broll_video()

            clip_path = clip_cutter.cut_clip(
                video_path=dl["video_path"],
                start_sec=clip_info["start_sec"],
                end_sec=clip_info["end_sec"],
                caption=clip_info.get("caption", niche.title()),
                watermark=final_watermark,
                sub_path=dl.get("sub_path"),
                broll_path=broll_path,
                subtitle_style=subtitle_style
            )

            if not clip_path:
                update_job_status(job_id, "error", 0, "Clip cutting failed.", user_id=user_id)
                return

            clip_paths = [clip_path]

            # Warm the cache in background for next request
            try:
                import hot_pipeline
                hot_pipeline.trigger_replenish(niche, is_free_tier)
            except Exception:
                pass

        final_clip = clip_paths[0]
        caption = clip_info.get("caption", niche.title())
        title = f"#Shorts {caption} #{niche.replace(' ', '')}"
        desc = f"{caption}\n\nAutomate your shorts with AI: https://viralclip-saas.onrender.com\n\n#Shorts #{niche.replace(' ', '')} #viral"
        tags = ["Shorts", niche, "viral"]

        if not auto_upload:
            update_job_status(job_id, "draft_ready", 100,
                f"Video rendered and ready for review in your Workplace tab!",
                url=final_clip, title=title, niche=niche, user_id=user_id)
            return

        update_job_status(job_id, "running", 85, "Uploading Short to YouTube...", user_id=user_id)

        # Fetch credentials from backend API (no Supabase needed on customer PC)
        creds_dict = fetch_youtube_creds(user_id)
        if not creds_dict:
            update_job_status(job_id, "error", 85,
                "YouTube account not connected. Please click 'Connect YouTube' on the dashboard first.",
                user_id=user_id)
            return

        def on_upload_progress(upload_pct):
            # Scale upload progress from 85% to 98%
            mapped_pct = int(85 + (upload_pct * 0.13))
            update_job_status(job_id, "running", mapped_pct, f"Uploading Short to YouTube ({upload_pct}%)...", user_id=user_id)

        upload_res = youtube_uploader.upload_video_to_youtube(
            final_clip, title=title, description=desc,
            tags=tags, creds_dict=creds_dict,
            progress_callback=on_upload_progress
        )

        if upload_res.get("status") == "success":
            video_url = upload_res.get("url", "")
            video_finder.mark_video_used(video["id"], video["title"])
            update_job_status(job_id, "complete", 100, "Done! Video is live on YouTube.",
                video_url, title, niche, user_id=user_id)
        else:
            update_job_status(job_id, "error", 100,
                f"Upload failed: {upload_res.get('error')}", user_id=user_id)
            return

    except Exception as e:
        update_job_status(job_id, "error", 0, f"Critical Pipeline Error: {str(e)}", user_id=user_id)

