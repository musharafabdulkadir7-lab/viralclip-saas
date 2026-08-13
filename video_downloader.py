"""
video_downloader.py
Downloads a YouTube video and its auto-generated subtitles using yt-dlp Python API.
"""
import yt_dlp
import os
import glob

DOWNLOAD_DIR = "downloaded_videos"

# Full path to the bundled ffmpeg binary
FFMPEG_EXE = r"C:\Users\musharaf.abdulkadir\AppData\Local\Python\pythoncore-3.14-64\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"
FFMPEG_DIR = os.path.dirname(FFMPEG_EXE)

def download_video_and_subs(url: str, video_id: str) -> dict:
    """
    Downloads the video at 720p and its auto-generated subtitles.
    Returns paths to the video file and subtitle file.
    """
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    output_template = os.path.join(DOWNLOAD_DIR, f"{video_id}.%(ext)s")

    print(f"[Downloader] Downloading video: {url}")

    ydl_opts = {
        # Use best single-file format (no merging needed) OR fall back to any mp4
        "format": "best[height<=720][ext=mp4]/best[ext=mp4]/best",
        "outtmpl": output_template,
        "writeautomaticsub": True,
        "subtitleslangs": ["en"],
        "subtitlesformat": "vtt",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ffmpeg_location": FFMPEG_DIR,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        print(f"[Downloader] Download error: {e}")
        return {}

    # Find downloaded files
    video_files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4"))
    sub_files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{video_id}*.vtt"))

    if not video_files:
        print("[Downloader] Video file not found after download.")
        return {}

    result = {"video_path": video_files[0]}
    if sub_files:
        result["sub_path"] = sub_files[0]
        print(f"[Downloader] Subtitles: {sub_files[0]}")
    else:
        result["sub_path"] = None
        print("[Downloader] No subtitles found.")

    print(f"[Downloader] Done: {result['video_path']}")
    return result
