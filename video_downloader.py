"""
video_downloader.py
Downloads a YouTube video and its auto-generated subtitles using yt-dlp Python API.
Uses system ffmpeg on Linux (Render) and falls back to imageio_ffmpeg on Windows.
"""
import yt_dlp
import os
import glob
import sys

DOWNLOAD_DIR = "downloaded_videos"


def _get_ffmpeg_dir() -> str:
    """Auto-detect ffmpeg location: system on Linux, bundled on Windows."""
    if sys.platform == "win32":
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
            return os.path.dirname(exe)
        except Exception:
            return ""
    else:
        # On Linux (Render), ffmpeg is installed system-wide via apt
        return "/usr/bin"


def _write_cookies_file() -> str:
    """Write YOUTUBE_COOKIES env var to a temp file for yt-dlp to use."""
    cookies_content = os.environ.get("YOUTUBE_COOKIES", "")
    if not cookies_content:
        return ""
    cookies_path = "/tmp/youtube_cookies.txt"
    with open(cookies_path, "w") as f:
        f.write(cookies_content)
    print("[Downloader] Using YouTube cookies from environment.")
    return cookies_path


def download_video_and_subs(url: str, video_id: str) -> dict:
    """
    Downloads the video at 720p and its auto-generated subtitles.
    Returns paths to the video file and subtitle file.
    """
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    output_template = os.path.join(DOWNLOAD_DIR, f"{video_id}.%(ext)s")
    ffmpeg_dir = _get_ffmpeg_dir()
    cookies_file = _write_cookies_file()

    print(f"[Downloader] Downloading video: {url}")
    print(f"[Downloader] Using ffmpeg from: {ffmpeg_dir or 'system PATH'}")

    ydl_opts = {
        # 480p is plenty for a short clip and downloads ~2x faster than 720p
        "format": "bestvideo[height<=480]+bestaudio/best[height<=480]/bestvideo+bestaudio/best",
        "outtmpl": output_template,
        "writeautomaticsub": True,
        "subtitleslangs": ["en"],
        "subtitlesformat": "vtt",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "merge_output_format": "mp4",
        # Speed: use 8 parallel fragment downloads
        "concurrent_fragment_downloads": 8,
        # Only download first 12 minutes — we only need a short clip
        "download_ranges": lambda info, *args: [{"start_time": 0, "end_time": 720}],
        "force_keyframes_at_cuts": True,
    }

    if ffmpeg_dir:
        ydl_opts["ffmpeg_location"] = ffmpeg_dir
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
