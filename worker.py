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

# Import video processing modules — fail softly if not installed
MODULES_AVAILABLE = True
try:
    import video_finder
    import video_downloader
    import clip_finder
    import clip_cutter
    import youtube_uploader
except ImportError as e:
    print(f"Warning: Video processing modules not found: {e}")
    MODULES_AVAILABLE = False

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

def update_job_status(job_id: str, status: str, progress: int, message: str, url: str = "", title: str = "", niche: str = "", user_id: str = ""):
    """Updates job progress back to the cloud via HTTP API."""
    # Read fresh each time so .exe env vars are always respected
    API_BASE_URL = os.environ.get("API_BASE_URL", "https://viralclip-saas.onrender.com")
    print(f"[{progress}%] {status}: {message}")
    try:
        import requests
        if status in ["complete", "error"]:
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
    """Fetches YouTube credentials via the backend API — no Supabase needed on customer PC."""
    API_BASE_URL = os.environ.get("API_BASE_URL", "https://viralclip-saas.onrender.com")
    try:
        import requests
        res = requests.get(f"{API_BASE_URL}/api/v1/user/youtube-creds",
            params={"user_id": user_id}, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get("refresh_token"):
                return data
    except Exception as e:
        print(f"Failed to fetch YouTube creds: {e}")
    return None

def run_clip_pipeline(niche: str, user_id: str, job_id: str):
    """The heavy video processing pipeline. Runs in a background thread."""

    if not MODULES_AVAILABLE:
        update_job_status(job_id, "error", 0,
            "Video agent modules not found. Please re-download ClipAI Worker.",
            user_id=user_id)
        return

    try:
        update_job_status(job_id, "running", 10, "Searching for viral videos...", user_id=user_id)
        candidates = video_finder.find_viral_videos(niche=niche)
        if not candidates:
            update_job_status(job_id, "error", 0, "No viral video found matching criteria.", user_id=user_id)
            return

        video = None
        dl = {}
        for i, candidate in enumerate(candidates):
            update_job_status(job_id, "running", 20 + i*5,
                f"Downloading: {candidate['title'][:45]}...", user_id=user_id)
            dl = video_downloader.download_video_and_subs(candidate["url"], candidate["id"])
            if dl.get("video_path"):
                video = candidate
                break

        if not video or not dl.get("video_path"):
            err = dl.get("error", "Unknown download error")
            update_job_status(job_id, "error", 0, f"Download failed: {err}", user_id=user_id)
            return

        update_job_status(job_id, "running", 50, "AI is finding the best segment...", user_id=user_id)
        if dl.get("sub_path"):
            clip_info = clip_finder.find_best_segment(dl["sub_path"], niche=niche)
        else:
            clip_info = {"start_sec": 120, "end_sec": 240,
                         "caption": niche.upper(), "reason": "No subs", "num_parts": 2}

        update_job_status(job_id, "running", 70,
            f"Cutting {clip_info.get('num_parts', 2)} parts & adding captions...", user_id=user_id)
        clip_paths = clip_cutter.cut_multipart_clips(
            video_path=dl["video_path"],
            start_sec=clip_info["start_sec"],
            end_sec=clip_info["end_sec"],
            caption=clip_info.get("caption", niche.upper()),
            num_parts=clip_info.get("num_parts", 2),
            watermark=f"@{niche.replace(' ', '').capitalize()}Viral"
        )

        if not clip_paths:
            update_job_status(job_id, "error", 0, "Clip cutting failed.", user_id=user_id)
            return

        update_job_status(job_id, "running", 85, "Uploading to YouTube...", user_id=user_id)
        caption = clip_info.get("caption", niche.title())
        title = f"#Shorts {caption} (Part 1) #{niche.replace(' ', '')}"
        desc = f"{caption} - Part 1\n\n#Shorts #{niche.replace(' ', '')} #viral"

        # Fetch credentials from backend API (no Supabase needed on customer PC)
        creds_dict = fetch_youtube_creds(user_id)

        if not creds_dict:
            update_job_status(job_id, "error", 85,
                "YouTube account not connected. Please connect your account on the website first.",
                user_id=user_id)
            return

        upload_res = youtube_uploader.upload_video_to_youtube(
            clip_paths[0], title=title, description=desc,
            tags=["Shorts", niche, "viral", "part1"],
            creds_dict=creds_dict
        )

        if upload_res.get("status") == "success":
            video_finder.mark_video_used(video["id"], video["title"])
            youtube_url = upload_res.get("url", "")
            update_job_status(job_id, "complete", 100, "Done! Video is live.",
                youtube_url, title, niche, user_id=user_id)
        else:
            update_job_status(job_id, "error", 100,
                f"Upload failed: {upload_res.get('error')}", user_id=user_id)

    except Exception as e:
        update_job_status(job_id, "error", 0, f"Critical Pipeline Error: {str(e)}", user_id=user_id)
        raise e

