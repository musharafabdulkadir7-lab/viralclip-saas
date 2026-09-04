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
            hot_clip = hot_pipeline.get_hot_clip(niche)
        except Exception:
            hot_clip = None

        if hot_clip and hot_clip.get("clip_paths"):
            clip_paths = hot_clip["clip_paths"]
            clip_info = hot_clip.get("clip_info", {"caption": niche.title(), "num_parts": len(clip_paths)})
            video = {"id": hot_clip.get("video_id", ""), "title": hot_clip.get("video_title", niche)}
            update_job_status(job_id, "running", 80, "⚡ Hot-Pipeline hit! Pre-baked in 0.05s, starting upload...", user_id=user_id)
            # Replenish in background for next time
            hot_pipeline.trigger_replenish(niche, is_free_tier)
        else:
            update_job_status(job_id, "running", 10, "Finding top viral video...", user_id=user_id)
            candidates = video_finder.find_viral_videos(niche=niche)
            if not candidates:
                update_job_status(job_id, "error", 0, "No viral video found matching criteria.", user_id=user_id)
                return

            # Prioritize the single top candidate
            video = candidates[0]
            update_job_status(job_id, "running", 25, f"Downloading: {video['title'][:45]}...", user_id=user_id)
            dl = video_downloader.download_video_and_subs(video["url"], video["id"])
            
            # Fallback to second candidate only if first failed download
            if not dl.get("video_path") and len(candidates) > 1:
                video = candidates[1]
                update_job_status(job_id, "running", 30, f"Downloading alternative: {video['title'][:45]}...", user_id=user_id)
                dl = video_downloader.download_video_and_subs(video["url"], video["id"])

            if not dl.get("video_path"):
                err = dl.get("error", "Download failed")
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

