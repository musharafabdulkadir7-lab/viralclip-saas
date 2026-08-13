import os
import sys
import pathlib
import redis
from rq import Worker, Queue, Connection

# Setup paths so the worker can find the AI Agent scripts
current_dir = pathlib.Path(__file__).parent.resolve()
if str(current_dir) not in sys.path:
    sys.path.append(str(current_dir))

DEFAULT_AGENT_DIR = current_dir.parent / "youtube_ai_agent"
YOUTUBE_AGENT_DIR = os.environ.get("YOUTUBE_AGENT_DIR", str(DEFAULT_AGENT_DIR))

if YOUTUBE_AGENT_DIR not in sys.path:
    sys.path.append(YOUTUBE_AGENT_DIR)

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

def _get_redis():
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)

def update_job_status(job_id: str, status: str, progress: int, message: str, url: str = ""):
    """Helper to update Redis hash and strictly enforce 24h TTL"""
    try:
        r = _get_redis()
        key = f"job:{job_id}"
        r.hset(key, mapping={
            "status": status,
            "progress": progress,
            "message": message,
            "url": url
        })
        r.expire(key, 86400)
    except Exception as e:
        print(f"Redis update error: {e}")

def run_clip_pipeline(niche: str, user_id: str, job_id: str):
    """The heavy video processing pipeline. Runs in a background thread."""

    if not MODULES_AVAILABLE:
        update_job_status(job_id, "error", 0,
            "Video agent modules not found on server. Set YOUTUBE_AGENT_DIR env var.")
        return

    try:
        update_job_status(job_id, "running", 10, "Searching for viral videos...")
        candidates = video_finder.find_viral_videos(niche=niche)
        if not candidates:
            update_job_status(job_id, "error", 0, "No viral video found.")
            return

        video = None
        dl = {}
        for i, candidate in enumerate(candidates):
            update_job_status(job_id, "running", 20 + i*5,
                f"Downloading: {candidate['title'][:45]}...")
            dl = video_downloader.download_video_and_subs(candidate["url"], candidate["id"])
            if dl.get("video_path"):
                video = candidate
                break

        if not video or not dl.get("video_path"):
            update_job_status(job_id, "error", 0, "All candidates failed to download.")
            return

        update_job_status(job_id, "running", 50, "AI is finding the best segment...")
        if dl.get("sub_path"):
            clip_info = clip_finder.find_best_segment(dl["sub_path"], niche=niche)
        else:
            clip_info = {"start_sec": 120, "end_sec": 240,
                         "caption": niche.upper(), "reason": "No subs", "num_parts": 2}

        update_job_status(job_id, "running", 70,
            f"Cutting {clip_info.get('num_parts', 2)} parts & adding captions...")
        clip_paths = clip_cutter.cut_multipart_clips(
            video_path=dl["video_path"],
            start_sec=clip_info["start_sec"],
            end_sec=clip_info["end_sec"],
            caption=clip_info.get("caption", niche.upper()),
            num_parts=clip_info.get("num_parts", 2)
        )

        if not clip_paths:
            update_job_status(job_id, "error", 0, "Clip cutting failed.")
            return

        update_job_status(job_id, "running", 85, "Uploading to YouTube...")
        caption = clip_info.get("caption", niche.title())
        title = f"#Shorts {caption} (Part 1) #{niche.replace(' ', '')}"
        desc = f"{caption} - Part 1\n\n#Shorts #{niche.replace(' ', '')} #viral"

        # Fetch credentials from Supabase
        from supabase import create_client
        supabase_url = os.environ.get("SUPABASE_URL", "")
        supabase_key = os.environ.get("SUPABASE_KEY", "")
        creds_dict = None
        if supabase_url and supabase_key:
            sb = create_client(supabase_url, supabase_key)
            res = sb.table("users").select("youtube_access_token, youtube_refresh_token").eq("id", user_id).execute()
            if res.data and res.data[0].get("youtube_refresh_token"):
                creds_dict = {
                    "token": res.data[0].get("youtube_access_token"),
                    "refresh_token": res.data[0].get("youtube_refresh_token"),
                    "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
                    "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", "")
                }

        if not creds_dict:
            update_job_status(job_id, "error", 100, "YouTube account not connected. Please connect your account in Settings.")
            return

        upload_res = youtube_uploader.upload_video_to_youtube(
            clip_paths[0], title=title, description=desc,
            tags=["Shorts", niche, "viral", "part1"],
            creds_dict=creds_dict
        )

        if upload_res.get("status") == "success":
            video_finder.mark_video_used(video["id"], video["title"])
            update_job_status(job_id, "complete", 100, "Done! Video is live.", upload_res.get("url"))
        else:
            update_job_status(job_id, "error", 100,
                f"Upload failed: {upload_res.get('error')}")

    except Exception as e:
        update_job_status(job_id, "error", 0, f"Critical Pipeline Error: {str(e)}")
        raise e

if __name__ == "__main__":
    print("Starting RQ Worker for 'default' queue...")
    redis_conn = _get_redis()
    with Connection(redis_conn):
        worker = Worker(map(Queue, ["default"]))
        worker.work()
