# automation.py
# Two legal operating modes:
#   Mode A — user_owned: user supplies their own video file or picks from their own channel
#   Mode B — cc_search:  YouTube Data API v3 with videoLicense=creativeCommon filter

import os
import re
import sys
import glob
import json
import time
import hmac
import hashlib
import shutil
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

# Force UTF-8 output on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# SHARED CONFIG
# ─────────────────────────────────────────────────────────────────────────────

CLIPAI_DIR   = str(Path.home() / ".clipai")
DOWNLOAD_DIR = os.path.join(CLIPAI_DIR, "downloaded_videos")
OUTPUT_DIR   = os.path.join(CLIPAI_DIR, "generated_videos")
HOT_POOL_DIR = os.path.join(CLIPAI_DIR, "hot_pool")
USED_VIDEOS_FILE = os.path.join(CLIPAI_DIR, "used_videos.json")

for _d in (CLIPAI_DIR, DOWNLOAD_DIR, OUTPUT_DIR, HOT_POOL_DIR):
    os.makedirs(_d, exist_ok=True)

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
API_BASE_URL    = os.environ.get("API_BASE_URL", "https://viralclip-saas.onrender.com")
WORKER_SECRET   = os.environ.get("WORKER_SECRET", "clipai_worker_sec_997f7c9_v2")

MIN_VIEWS        = 50_000
MIN_DURATION_SEC = 300
MAX_AGE_DAYS     = 730
TOP_N            = 5


def _safe(text) -> str:
    """UTF-8 safe logging — preserves emojis and non-English titles."""
    val = str(text) if text is not None else ""
    try:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        val.encode(enc)
        return val
    except Exception:
        return val.encode("utf-8", errors="replace").decode("utf-8")


def log(msg: str):
    print(_safe(str(msg)))


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — VIDEO SOURCE
# ─────────────────────────────────────────────────────────────────────────────

# Redis deduplication (keeps cross-worker state on Render)
USED_VIDEOS_REDIS_KEY = "viralclip:used_videos"
_redis_used_client = None

def _get_used_redis():
    global _redis_used_client
    if _redis_used_client is None:
        try:
            import redis as _rl
            c = _rl.Redis.from_url(
                os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                decode_responses=True, socket_connect_timeout=2
            )
            c.ping()
            _redis_used_client = c
        except Exception:
            _redis_used_client = False
    return _redis_used_client if _redis_used_client else None


def load_used_videos() -> dict:
    r = _get_used_redis()
    if r:
        try:
            return {vid: {} for vid in r.smembers(USED_VIDEOS_REDIS_KEY)}
        except Exception:
            pass
    if os.path.exists(USED_VIDEOS_FILE):
        try:
            with open(USED_VIDEOS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def mark_video_used(video_id: str, title: str = ""):
    r = _get_used_redis()
    if r:
        try:
            r.sadd(USED_VIDEOS_REDIS_KEY, video_id)
            log(f"[VideoSource] Marked used (Redis): {video_id}")
            return
        except Exception:
            pass
    used = load_used_videos()
    used[video_id] = {"title": title, "used_at": datetime.now().isoformat()}
    with open(USED_VIDEOS_FILE, "w") as f:
        json.dump(used, f, indent=2)
    log(f"[VideoSource] Marked used (JSON): {video_id}")


def _iso8601_to_seconds(duration: str) -> int:
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration)
    if not match:
        return 0
    return int(match.group(1) or 0)*3600 + int(match.group(2) or 0)*60 + int(match.group(3) or 0)


def _is_transient_error(exc):
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


@retry(reraise=True, stop=stop_after_attempt(3),
       wait=wait_exponential(multiplier=1, min=2, max=10),
       retry=retry_if_exception(_is_transient_error))
def _http_get(url: str, params: dict) -> dict:
    res = httpx.get(url, params=params, timeout=15)
    res.raise_for_status()
    return res.json()


# ── MODE B: CC licensed search via YouTube Data API ──────────────────────────

def find_cc_videos(niche: str, max_results: int = 20) -> list:
    """
    Mode B — find Creative Commons licensed videos via YouTube Data API v3.
    Only returns videos with videoLicense=creativeCommon. No scraping.
    """
    if not YOUTUBE_API_KEY:
        raise Exception("YOUTUBE_API_KEY not set. Cannot search for CC videos.")

    used    = load_used_videos()
    cutoff  = (datetime.now() - timedelta(days=MAX_AGE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    log(f"[VideoSource] CC search via YouTube API: '{niche}'")

    try:
        data = _http_get("https://www.googleapis.com/youtube/v3/search", {
            "part": "id,snippet", "q": niche, "type": "video",
            "order": "viewCount", "videoDuration": "medium",
            "publishedAfter": cutoff, "maxResults": max_results,
            "videoLicense": "creativeCommon",   # ← hard CC filter at API level
            "key": YOUTUBE_API_KEY,
        })
    except httpx.HTTPStatusError as e:
        raise Exception(f"YouTube API error {e.response.status_code}: quota exceeded or key invalid.")

    items     = data.get("items", [])
    video_ids = [i["id"]["videoId"] for i in items if i.get("id", {}).get("videoId")]
    if not video_ids:
        return []

    try:
        detail = _http_get("https://www.googleapis.com/youtube/v3/videos", {
            "part": "contentDetails,statistics,snippet",
            "id": ",".join(video_ids),
            "key": YOUTUBE_API_KEY,
        })
    except httpx.HTTPStatusError as e:
        raise Exception(f"YouTube API details error {e.response.status_code}")

    candidates = []
    for item in detail.get("items", []):
        vid_id       = item["id"]
        if vid_id in used:
            continue
        duration_sec = _iso8601_to_seconds(item.get("contentDetails", {}).get("duration", "PT0S"))
        view_count   = int(item.get("statistics", {}).get("viewCount", 0))
        title        = item.get("snippet", {}).get("title", "")[:80]
        channel      = item.get("snippet", {}).get("channelTitle", "Unknown")
        channel_id   = item.get("snippet", {}).get("channelId", "")

        if duration_sec < MIN_DURATION_SEC:
            continue
        if view_count < MIN_VIEWS:
            continue

        candidates.append({
            "id": vid_id,
            "url": f"https://www.youtube.com/watch?v={vid_id}",
            "title": title,
            "channel": channel,
            "channel_id": channel_id,
            "duration": duration_sec,
            "view_count": view_count,
            "license": "creativeCommon",
            "mode": "cc_search",
            # Attribution must appear in every upload description (CC license requirement)
            "attribution": (
                f"Original video: \"{title}\" by {channel}\n"
                f"Source: https://youtu.be/{vid_id}\n"
                f"Licensed under Creative Commons (CC BY)"
            ),
        })
        log(f"  [CC] {_safe(title[:50])} | {view_count:,} views | {duration_sec//60}min | by {_safe(channel)}")

    candidates.sort(key=lambda x: x["view_count"], reverse=True)
    return candidates[:TOP_N]


# ── MODE A: User-owned videos ─────────────────────────────────────────────────

def get_user_own_videos(creds_dict: dict, max_results: int = 20) -> list:
    """
    Mode A — fetch videos from the user's own YouTube channel via their OAuth token.
    No scraping. User owns full rights to these videos.
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
        scopes=["https://www.googleapis.com/auth/youtube.readonly",
                "https://www.googleapis.com/auth/youtube.upload"],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    youtube = build("youtube", "v3", credentials=creds)

    # Get user's channel ID
    channel_res = youtube.channels().list(part="id,snippet", mine=True).execute()
    channel_items = channel_res.get("items", [])
    if not channel_items:
        raise Exception("No YouTube channel found for this account.")
    channel_id   = channel_items[0]["id"]
    channel_name = channel_items[0]["snippet"]["title"]
    log(f"[VideoSource] Fetching user's own videos from channel: {_safe(channel_name)}")

    # List channel's uploads
    search_res = youtube.search().list(
        part="id,snippet", channelId=channel_id,
        type="video", order="date",
        videoDuration="medium", maxResults=max_results
    ).execute()

    video_ids = [i["id"]["videoId"] for i in search_res.get("items", []) if i.get("id", {}).get("videoId")]
    if not video_ids:
        return []

    # Get durations
    detail_res = youtube.videos().list(
        part="contentDetails,statistics,snippet",
        id=",".join(video_ids)
    ).execute()

    candidates = []
    for item in detail_res.get("items", []):
        vid_id       = item["id"]
        duration_sec = _iso8601_to_seconds(item.get("contentDetails", {}).get("duration", "PT0S"))
        title        = item.get("snippet", {}).get("title", "")[:80]
        view_count   = int(item.get("statistics", {}).get("viewCount", 0))

        if duration_sec < 60:   # skip videos under 1 minute
            continue

        candidates.append({
            "id": vid_id,
            "url": f"https://www.youtube.com/watch?v={vid_id}",
            "title": title,
            "channel": channel_name,
            "channel_id": channel_id,
            "duration": duration_sec,
            "view_count": view_count,
            "license": "user_owned",
            "mode": "user_owned",
            "attribution": "",  # user owns it — no attribution needed
        })
        log(f"  [Own] {_safe(title[:50])} | {duration_sec//60}min")

    return candidates


def find_viral_videos(niche: str = "finance", max_results: int = 15,
                      mode: str = "cc_search", creds_dict: dict = None) -> list:
    """
    Unified entry point. mode = 'cc_search' or 'user_owned'.
    Direct YouTube URLs are always accepted in both modes.
    """
    trimmed = niche.strip() if niche else ""

    # Direct URL passthrough (works in both modes — user pasting their own URL)
    if any(x in trimmed for x in ["youtube.com/watch", "youtu.be/", "youtube.com/shorts/"]):
        log(f"[VideoSource] Direct URL: {trimmed}")
        return [{
            "id": "direct",
            "url": trimmed,
            "title": "Custom Video",
            "channel": "Direct",
            "duration": 600,
            "view_count": 0,
            "license": "user_provided",
            "mode": "user_owned",
            "attribution": "",
        }]

    if mode == "user_owned":
        if not creds_dict:
            raise Exception("user_owned mode requires OAuth credentials (creds_dict).")
        return get_user_own_videos(creds_dict, max_results)
    else:
        return find_cc_videos(niche, max_results)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — VIDEO DOWNLOADER
# ─────────────────────────────────────────────────────────────────────────────

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


def _write_cookies_file() -> str:
    cookies_path = os.environ.get("YOUTUBE_COOKIES_FILE", "")
    if cookies_path and os.path.exists(cookies_path):
        return cookies_path
    raw = os.environ.get("YOUTUBE_COOKIES", "")
    if not raw:
        return ""
    raw = raw.replace("\\n", "\n").replace("\\t", "\t")
    out = ("/tmp/yt_cookies.txt" if sys.platform != "win32"
           else os.path.join(os.environ.get("TEMP", "."), "yt_cookies.txt"))
    with open(out, "w", encoding="utf-8") as f:
        f.write(raw)
    return out


def download_video_and_subs(url: str, video_id: str) -> dict:
    """
    Downloads the video using yt-dlp (no proxy rotation, no client spoofing).
    For user-owned videos on YouTube this works cleanly with OAuth cookies.
    For CC videos the standard web client is sufficient — no evasion needed.
    """
    import yt_dlp
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    existing_mp4  = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")
    existing_subs = glob.glob(os.path.join(DOWNLOAD_DIR, f"{video_id}*.vtt"))
    if os.path.exists(existing_mp4) and os.path.getsize(existing_mp4) > 102400:
        log(f"[Downloader] Cache hit: {existing_mp4}")
        return {"video_path": existing_mp4, "sub_path": existing_subs[0] if existing_subs else None}

    ffmpeg_exe   = _get_ffmpeg_exe()
    cookies_file = _write_cookies_file()

    ydl_opts = {
        "format": "best[height<=720][ext=mp4]/bestvideo[height<=720]+bestaudio/best",
        "outtmpl": os.path.join(DOWNLOAD_DIR, f"{video_id}.%(ext)s"),
        "writeautomaticsub": True, "subtitleslangs": ["en"],
        "subtitlesformat": "vtt",
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "merge_output_format": "mp4",
        "retries": 5, "fragment_retries": 5,
        "nocheckcertificate": True,
        "ffmpeg_location": ffmpeg_exe,
    }
    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file

    log(f"[Downloader] Downloading: {url}")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        log(f"[Downloader] Error: {e}")
        return {"error": str(e)}

    video_files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4"))
    if not video_files:
        all_f = glob.glob(os.path.join(DOWNLOAD_DIR, f"{video_id}.*"))
        video_files = [f for f in all_f if not any(f.endswith(x) for x in [".vtt", ".json", ".srt", ".ytdl"])]

    sub_files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{video_id}*.vtt"))
    if not video_files:
        return {"error": "Video file not found after download"}

    log(f"[Downloader] Done: {video_files[0]}")
    return {"video_path": video_files[0], "sub_path": sub_files[0] if sub_files else None}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — CLIP FINDER (AI segment selection)
# ─────────────────────────────────────────────────────────────────────────────

def parse_vtt(vtt_path: str) -> list:
    entries    = []
    seen_texts = set()
    try:
        content = open(vtt_path, "r", encoding="utf-8").read()
    except Exception as e:
        log(f"[ClipFinder] Failed to read VTT: {e}")
        return []

    def ts_to_sec(h, m, s, ms):
        return int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000

    for block in re.split(r"\n\s*\n", content.strip()):
        lines = block.strip().splitlines()
        ts_line, text_lines = None, []
        for i, line in enumerate(lines):
            if "-->" in line:
                ts_line    = line
                text_lines = lines[i+1:]
                break
        if not ts_line:
            continue
        m = re.match(r"(\d+):(\d+):(\d+)[\.,](\d+)\s*-->\s*(\d+):(\d+):(\d+)[\.,](\d+)", ts_line)
        if not m:
            continue
        start    = ts_to_sec(*m.groups()[:4])
        end      = ts_to_sec(*m.groups()[4:])
        raw_text = " ".join(text_lines)
        raw_text = re.sub(r"<[^>]+>", "", raw_text)
        raw_text = re.sub(r"\s+", " ", raw_text).strip()
        if not raw_text or raw_text in (" ", "[Music]", "[Applause]"):
            continue
        if raw_text in seen_texts:
            continue
        seen_texts.add(raw_text)
        entries.append({"start": start, "end": end, "text": raw_text})

    log(f"[ClipFinder] Parsed {len(entries)} subtitle entries.")
    return entries


def build_transcript_block(entries: list, max_chars: int = 8000) -> str:
    lines, total = [], 0
    for e in entries:
        secs = int(e["start"])
        line = f"[{secs//60:02d}:{secs%60:02d}] {e['text']}"
        total += len(line)
        if total > max_chars:
            break
        lines.append(line)
    return "\n".join(lines)


def find_best_segment(sub_path: str, niche: str = "motivation") -> dict:
    log("[ClipFinder] Finding best 45-60s viral moment...")
    entries = parse_vtt(sub_path)
    if not entries:
        return {"start_sec": 60, "end_sec": 110, "caption": niche.title(), "num_parts": 1}

    transcript = build_transcript_block(entries, max_chars=10000)
    try:
        import requests
        res = requests.post(
            f"{API_BASE_URL}/api/v1/worker/analyze-transcript",
            json={"transcript": transcript, "niche": niche},
            params={"user_id": os.environ.get("CLIPAI_USER_ID", "demo_user_123")},
            timeout=30
        )
        if res.status_code == 200:
            data      = res.json()
            start     = data.get("start_sec", 60)
            end       = data.get("end_sec", start + 50)
            caption   = data.get("caption", niche.title())

            valid_starts = [int(e["start"]) for e in entries]
            valid_ends   = [int(e["end"])   for e in entries]
            snapped_start = min(valid_starts, key=lambda x: abs(x - start)) if valid_starts else start
            snapped_end   = min(valid_ends,   key=lambda x: abs(x - end))   if valid_ends   else end

            if snapped_end - snapped_start > 60:
                snapped_end = snapped_start + 60
            elif snapped_end - snapped_start < 25:
                snapped_end = snapped_start + 45

            log(f"[ClipFinder] Segment: {snapped_start}s - {snapped_end}s ({snapped_end - snapped_start}s)")
            return {"start_sec": snapped_start, "end_sec": snapped_end, "caption": caption, "num_parts": 1}
    except Exception as e:
        log(f"[ClipFinder] AI request failed: {e}")
    return {"start_sec": 60, "end_sec": 110, "caption": niche.title(), "num_parts": 1}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — CLIP CUTTER (ffmpeg rendering, no fingerprint evasion)
# ─────────────────────────────────────────────────────────────────────────────

FFMPEG         = _get_ffmpeg_exe()
WATERMARK_TEXT = os.environ.get("WATERMARK_TEXT", "@YourChannel")


def get_best_h264_encoder() -> tuple:
    cpu_cores = os.cpu_count() or 4
    try:
        res = subprocess.run(
            [FFMPEG, "-hide_banner", "-encoders"],
            capture_output=True, text=True, errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        out = res.stdout
        if "h264_nvenc" in out:
            test = subprocess.run(
                [FFMPEG, "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1", "-c:v", "h264_nvenc", "-f", "null", "-"],
                capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            if test.returncode == 0:
                return "h264_nvenc", ["-preset", "p1", "-cq", "23", "-threads", str(cpu_cores)]
        if "h264_videotoolbox" in out:
            return "h264_videotoolbox", ["-realtime", "1", "-q:v", "65"]
        if "h264_qsv" in out:
            return "h264_qsv", ["-preset", "veryfast", "-q", "23"]
        if "h264_amf" in out:
            return "h264_amf", ["-quality", "speed", "-rc", "cqp", "-qp_i", "23"]
    except Exception as e:
        log(f"[ClipCutter] Hardware probe: {e}")
    return "libx264", ["-preset", "fast", "-crf", "22", "-threads", str(cpu_cores)]


def _parse_time(ts):
    parts = ts.strip().split(":")
    h, m, s = (parts if len(parts) == 3 else ["00"] + parts)
    sec, ms = s.split(".") if "." in s else (s, "000")
    return int(h)*3600 + int(m)*60 + int(sec) + int(ms)/1000.0


def _fmt_ass(sec):
    sec = max(0, sec)
    return f"{int(sec//3600)}:{int((sec%3600)//60):02d}:{sec%60:05.2f}"


def generate_ass_subtitle(vtt_path: str, start_sec: float, duration: float,
                           output_ass: str, subtitle_style: str = "hormozi") -> bool:
    try:
        content = open(vtt_path, "r", encoding="utf-8").read()
    except Exception:
        return False

    is_clean  = subtitle_style == "clean_minimal"
    font      = "Arial" if is_clean else "Impact"
    fsize     = "75" if is_clean else "95"
    margin_v  = "450" if is_clean else "550"
    outline   = "3" if is_clean else "6"

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{font},{fsize},&H0000FFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline},3,2,10,10,{margin_v},1
Style: Alt,{font},{fsize},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline},3,2,10,10,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events   = []
    end_sec  = start_sec + duration
    alt      = False

    for st, et, text in re.findall(
        r"(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3})\n((?:.|\n)*?)(?=\n\n|\Z)",
        content
    ):
        t_start = _parse_time(st) - start_sec
        t_end   = _parse_time(et) - start_sec
        if t_end < 0 or t_start > duration:
            continue
        text = re.sub(r"<[^>]+>", "", text).strip().replace("\n", " ")
        if not text:
            continue
        style = "Alt" if alt else "Main"
        alt   = not alt
        events.append(f"Dialogue: 0,{_fmt_ass(t_start)},{_fmt_ass(t_end)},{style},,0,0,0,,{text}")

    if not events:
        return False
    try:
        open(output_ass, "w", encoding="utf-8").write(header + "\n".join(events))
        return True
    except Exception:
        return False


def cut_clip(video_path: str, start_sec: int, end_sec: int, caption: str,
             watermark: str = None, sub_path: str = None,
             subtitle_style: str = "hormozi") -> str:
    """
    Cuts and formats a 9:16 Short. Cinematic blur background.
    No fingerprint evasion, no speed manipulation, no gamma tricks.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, f"clip_{int(time.time())}.mp4")
    duration = min(end_sec - start_sec, 60)
    if duration < 10:
        duration = 45

    if len(caption) > 26:
        caption = caption[:23] + "..."

    def esc(t):
        return t.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:").replace(",", "\\,")

    safe_cap = esc(caption)
    safe_wm  = esc(watermark or WATERMARK_TEXT)

    ass_filter = ""
    if sub_path and os.path.exists(sub_path):
        ass_path = os.path.join(OUTPUT_DIR, f"subs_{int(time.time())}.ass")
        if generate_ass_subtitle(sub_path, start_sec, duration, ass_path, subtitle_style):
            safe_ass   = ass_path.replace("\\", "/").replace(":", "\\:")
            ass_filter = f",subtitles={safe_ass}"

    encoder, enc_args = get_best_h264_encoder()
    cpu_threads       = str(os.cpu_count() or 4)

    # Clean 9:16 cinematic blur — no fingerprint manipulation
    filter_complex = (
        "[0:v]split=2[bg][fg]; "
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,"
        "crop=1080:1920,boxblur=luma_radius=20:luma_power=2[bg_blur]; "
        "[fg]scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos[fg_sc]; "
        "[bg_blur][fg_sc]overlay=(W-w)/2:(H-h)/2[merged]; "
        f"[merged]drawtext=text='{safe_cap}':fontsize=38:fontcolor=white:borderw=2:"
        f"bordercolor=black:x=(w-text_w)/2:y=h-text_h-350:font=Arial Bold:box=1:boxcolor=black@0.5:boxborderw=12:fix_bounds=1,"
        f"drawtext=text='{safe_wm}':fontsize=24:fontcolor=white@0.7:borderw=1:"
        f"bordercolor=black@0.4:x=40:y=80:font=Arial:fix_bounds=1"
        f"{ass_filter}[v_out]"
    )

    cmd = [
        FFMPEG, "-y", "-threads", cpu_threads,
        "-ss", str(start_sec), "-t", str(duration), "-i", video_path,
        "-filter_complex", filter_complex,
        "-map", "[v_out]", "-map", "0:a",
        "-r", "30", "-pix_fmt", "yuv420p",
        "-c:v", encoder, *enc_args,
        "-c:a", "aac", "-b:a", "192k",
        "-map_metadata", "-1",
        "-movflags", "+faststart",
        out_path,
    ]
    log(f"[ClipCutter] {start_sec}s-{end_sec}s ({duration}s) | encoder: {encoder}")
    try:
        subprocess.run(cmd, timeout=600, check=True, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        mb = os.path.getsize(out_path) / 1024 / 1024
        log(f"[ClipCutter] Done: {out_path} ({mb:.1f} MB)")
        return out_path
    except subprocess.CalledProcessError as e:
        log(f"[ClipCutter] ffmpeg error: {e.stderr.decode(errors='replace')[:600]}")
        return ""
    except subprocess.TimeoutExpired:
        log("[ClipCutter] ffmpeg timed out.")
        return ""


def cut_multipart_clips(*args, **kwargs) -> list:
    kwargs.pop("num_parts", None)
    p = cut_clip(*args, **kwargs)
    return [p] if p else []


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — HOT PIPELINE (pre-bake cache)
# ─────────────────────────────────────────────────────────────────────────────

_replenishing: set = set()
_hot_lock = threading.Lock()


def get_hot_clip(niche: str) -> dict:
    key      = niche.lower().replace(" ", "_")
    niche_dir = os.path.join(HOT_POOL_DIR, key)
    if not os.path.exists(niche_dir):
        return None
    for mf in glob.glob(os.path.join(niche_dir, "*.json")):
        try:
            data  = json.load(open(mf, encoding="utf-8"))
            clips = data.get("clip_paths", [])
            if clips and all(os.path.exists(c) and os.path.getsize(c) > 102400 for c in clips):
                os.remove(mf)
                log(f"[HotPipeline] Cache hit for '{niche}'")
                return data
        except Exception:
            pass
    return None


def prebake_clip_worker(niche: str, mode: str = "cc_search", creds_dict: dict = None):
    key = niche.lower().replace(" ", "_")
    with _hot_lock:
        if key in _replenishing:
            return
        _replenishing.add(key)
    try:
        niche_dir = os.path.join(HOT_POOL_DIR, key)
        os.makedirs(niche_dir, exist_ok=True)
        if len(glob.glob(os.path.join(niche_dir, "*.json"))) >= 2:
            return
        log(f"[HotPipeline] Pre-baking '{niche}' in background...")
        candidates = find_viral_videos(niche=niche, mode=mode, creds_dict=creds_dict)
        if not candidates:
            return
        video, dl = None, {}
        for c in candidates:
            dl = download_video_and_subs(c["url"], c["id"])
            if dl.get("video_path"):
                video = c
                break
        if not video:
            return
        clip_info = find_best_segment(dl["sub_path"], niche) if dl.get("sub_path") else                     {"start_sec": 60, "end_sec": 110, "caption": niche.title(), "num_parts": 1}
        clip_path = cut_clip(dl["video_path"], clip_info["start_sec"], clip_info["end_sec"],
                              clip_info.get("caption", niche.title()), sub_path=dl.get("sub_path"))
        if not clip_path:
            return
        mf = os.path.join(niche_dir, f"hot_{video['id']}_{int(time.time())}.json")
        json.dump({
            "niche": niche, "video_id": video["id"], "video_title": video["title"],
            "clip_info": clip_info, "clip_paths": [clip_path],
            "attribution": video.get("attribution", ""),
            "license": video.get("license", ""),
            "created_at": time.time()
        }, open(mf, "w", encoding="utf-8"), indent=2)
        log(f"[HotPipeline] Pre-baked clip ready.")
    except Exception as e:
        log(f"[HotPipeline] Error: {e}")
    finally:
        with _hot_lock:
            _replenishing.discard(key)


def clear_stale_cache(keep_niche: str = None):
    keep_key = keep_niche.lower().replace(" ", "_") if keep_niche else None
    if os.path.exists(HOT_POOL_DIR):
        for entry in os.listdir(HOT_POOL_DIR):
            if keep_key and entry == keep_key:
                continue
            shutil.rmtree(os.path.join(HOT_POOL_DIR, entry), ignore_errors=True)
    for f in Path(DOWNLOAD_DIR).glob("*.*"):
        try:
            f.unlink()
        except Exception:
            pass


def trigger_replenish(niche: str, mode: str = "cc_search", creds_dict: dict = None):
    t = threading.Thread(target=prebake_clip_worker, args=(niche, mode, creds_dict), daemon=True)
    t.start()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — YOUTUBE UPLOADER
# ─────────────────────────────────────────────────────────────────────────────

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def get_authenticated_service(creds_dict: dict):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    if not creds_dict:
        return None
    creds = Credentials(
        token=creds_dict.get("token"),
        refresh_token=creds_dict.get("refresh_token"),
        client_id=creds_dict.get("client_id"),
        client_secret=creds_dict.get("client_secret"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=YOUTUBE_SCOPES,
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        try:
            from supabase import create_client
            user_id = creds_dict.get("user_id")
            sb_url  = os.environ.get("SUPABASE_URL", "")
            sb_key  = os.environ.get("SUPABASE_KEY", "")
            if sb_url and sb_key and user_id:
                create_client(sb_url, sb_key).table("users").update(
                    {"youtube_access_token": creds.token}
                ).eq("id", user_id).execute()
        except Exception as e:
            log(f"[Auth] Token save warning: {e}")
    return build("youtube", "v3", credentials=creds)


def upload_video_to_youtube(video_path: str, title: str, description: str,
                             tags: list, creds_dict: dict,
                             progress_callback=None) -> dict:
    from googleapiclient.http import MediaFileUpload
    if not os.path.exists(video_path):
        return {"error": f"Video not found: {video_path}"}
    try:
        youtube = get_authenticated_service(creds_dict)
    except Exception as e:
        return {"error": f"Auth error: {e}"}
    if not youtube:
        return {"error": "Authentication failed."}

    body = {
        "snippet": {"title": title, "description": description, "tags": tags, "categoryId": "22"},
        "status":  {"privacyStatus": "public"},
    }
    media   = MediaFileUpload(video_path, chunksize=16*1024*1024, resumable=True)
    request = youtube.videos().insert(part=",".join(body.keys()), body=body, media_body=media)

    response, retries = None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status and progress_callback:
                try:
                    progress_callback(int(status.progress() * 100))
                except Exception:
                    pass
        except Exception as e:
            retries += 1
            if retries > 5:
                return {"error": str(e)}
            time.sleep(2 * retries)

    vid_id = response.get("id")
    return {"status": "success", "video_id": vid_id, "url": f"https://youtube.com/shorts/{vid_id}"}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — WORKER PIPELINE ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────

def update_job_status(job_id: str, status: str, progress: int, message: str,
                      url: str = "", title: str = "", niche: str = "", user_id: str = ""):
    log(f"[{progress}%] {status}: {_safe(message)}")
    try:
        import requests
        if status in ("complete", "draft_ready", "error"):
            requests.post(f"{API_BASE_URL}/api/v1/worker/complete", json={
                "job_id": job_id, "status": status, "message": message,
                "url": url, "title": title, "niche": niche,
            }, params={"user_id": user_id or "unknown"}, timeout=10)
        else:
            requests.post(f"{API_BASE_URL}/api/v1/worker/progress", json={
                "job_id": job_id, "status": status, "progress": progress,
                "message": message, "url": url,
            }, timeout=5)
    except Exception as e:
        log(f"[Worker] Progress update failed: {e}")


def fetch_youtube_creds(user_id: str) -> dict:
    try:
        import requests
        token = hmac.new(WORKER_SECRET.encode(), user_id.encode(), hashlib.sha256).hexdigest()
        res   = requests.get(f"{API_BASE_URL}/api/v1/user/youtube-creds",
                             params={"user_id": user_id, "token": token}, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get("refresh_token"):
                return data
    except Exception as e:
        log(f"[Worker] Fetch creds failed: {e}")
    return None


def run_clip_pipeline(niche: str, user_id: str, job_id: str,
                      is_free_tier: bool = False, auto_upload: bool = True,
                      subtitle_style: str = "hormozi",
                      mode: str = "cc_search"):
    """
    Main pipeline. mode = 'cc_search' (default) or 'user_owned'.
    """
    try:
        clear_stale_cache(keep_niche=niche)
        hot_clip = get_hot_clip(niche)

        creds_dict = fetch_youtube_creds(user_id)

        if hot_clip and hot_clip.get("clip_paths"):
            clip_paths = hot_clip["clip_paths"]
            clip_info  = hot_clip.get("clip_info", {"caption": niche.title()})
            video      = {"id": hot_clip.get("video_id", ""), "title": hot_clip.get("video_title", niche),
                          "attribution": hot_clip.get("attribution", ""), "url": ""}
            update_job_status(job_id, "running", 80, "Instant clip ready. Uploading...", user_id=user_id)
            trigger_replenish(niche, mode=mode, creds_dict=creds_dict)
        else:
            # ── 1. Find ──────────────────────────────────────────────────────
            update_job_status(job_id, "running", 10, "Finding video...", user_id=user_id)
            try:
                candidates = find_viral_videos(niche=niche, mode=mode, creds_dict=creds_dict)
            except Exception as e:
                update_job_status(job_id, "error", 0, f"Could not find video: {e}", user_id=user_id)
                return
            if not candidates:
                update_job_status(job_id, "error", 0, "No suitable video found.", user_id=user_id)
                return

            # ── 2. Download ───────────────────────────────────────────────────
            dl, video = {}, candidates[0]
            for i, candidate in enumerate(candidates):
                update_job_status(job_id, "running", 25 + i*5,
                                  f"Downloading: {candidate['title'][:40]}...", user_id=user_id)
                dl = download_video_and_subs(candidate["url"], candidate["id"])
                if dl.get("video_path"):
                    video = candidate
                    break
            if not dl.get("video_path"):
                update_job_status(job_id, "error", 0, f"Download failed: {dl.get('error', 'unknown')}", user_id=user_id)
                return

            # ── 3. Find segment ───────────────────────────────────────────────
            update_job_status(job_id, "running", 50, "AI selecting best 45-60s moment...", user_id=user_id)
            clip_info = find_best_segment(dl["sub_path"], niche) if dl.get("sub_path") else                         {"start_sec": 60, "end_sec": 110, "caption": niche.title(), "num_parts": 1}

            # ── 4. Cut clip ────────────────────────────────────────────────────
            update_job_status(job_id, "running", 70, "Rendering Short...", user_id=user_id)
            clip_path = cut_clip(
                video_path=dl["video_path"],
                start_sec=clip_info["start_sec"], end_sec=clip_info["end_sec"],
                caption=clip_info.get("caption", niche.title()),
                watermark="Made with ViralClip.ai" if is_free_tier else WATERMARK_TEXT,
                sub_path=dl.get("sub_path"), subtitle_style=subtitle_style,
            )
            if not clip_path:
                update_job_status(job_id, "error", 0, "Clip rendering failed.", user_id=user_id)
                return

            clip_paths = [clip_path]
            trigger_replenish(niche, mode=mode, creds_dict=creds_dict)

        # ── 5. Upload or draft ────────────────────────────────────────────────
        final_clip  = clip_paths[0]
        caption     = clip_info.get("caption", niche.title())
        title       = f"#Shorts {caption}"
        attribution = video.get("attribution", "")

        # Attribution always goes in the description (CC requirement for Mode B)
        desc = (
            f"{caption}\n\n"
            + (f"--- Original Source ---\n{attribution}\n\n" if attribution else "")
            + f"Automate your Shorts: https://viralclip-saas.onrender.com\n\n"
            + f"#Shorts #{niche.replace(' ', '')} #viral"
        )
        tags = ["Shorts", niche, "viral"]

        if not auto_upload:
            update_job_status(job_id, "draft_ready", 100,
                              "Video ready in your Workplace tab!",
                              url=final_clip, title=title, niche=niche, user_id=user_id)
            return

        update_job_status(job_id, "running", 85, "Uploading to YouTube...", user_id=user_id)
        if not creds_dict:
            update_job_status(job_id, "error", 85,
                              "YouTube account not connected. Click 'Connect YouTube' first.",
                              user_id=user_id)
            return

        upload_res = upload_video_to_youtube(
            final_clip, title=title, description=desc, tags=tags, creds_dict=creds_dict,
            progress_callback=lambda p: update_job_status(
                job_id, "running", int(85 + p * 0.13), f"Uploading... {p}%", user_id=user_id)
        )

        if upload_res.get("status") == "success":
            mark_video_used(video["id"], video.get("title", ""))
            update_job_status(job_id, "complete", 100, "Done! Video is live.",
                              upload_res.get("url", ""), title, niche, user_id=user_id)
        else:
            update_job_status(job_id, "error", 100,
                              f"Upload failed: {upload_res.get('error', 'unknown')}", user_id=user_id)

    except Exception as e:
        update_job_status(job_id, "error", 0, f"Pipeline error: {e}", user_id=user_id)
