import os
import sys
import time
import requests
import subprocess
from pathlib import Path

# Setup cross-platform paths
HOME_DIR = Path.home() / ".clipai"
BIN_DIR = HOME_DIR / "bin"
os.makedirs(BIN_DIR, exist_ok=True)

# Update system path so subprocesses can find bin files easily
os.environ["PATH"] += os.pathsep + str(BIN_DIR)

API_BASE_URL = os.environ.get("API_BASE_URL", "https://viralclip-saas.onrender.com")
# For testing locally, uncomment the line below:
# API_BASE_URL = "http://localhost:8000"

USER_ID = os.environ.get("CLIPAI_USER_ID", "demo_user_123")

def update_yt_dlp():
    """Auto-update yt-dlp on boot to prevent YouTube bot blocking."""
    print("Checking for yt-dlp updates...")
    try:
        # If installed via pip in the local python environment
        subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"], check=True, capture_output=True)
        print("yt-dlp is up to date!")
    except Exception as e:
        print(f"Failed to update yt-dlp: {e}")

def run_worker_loop():
    print(f"Starting ClipAI Companion Worker for user: {USER_ID}")
    print(f"Polling {API_BASE_URL} for jobs...")
    
    # Import the pipeline here so it doesn't fail fast if dependencies are missing before update
    try:
        from worker import run_clip_pipeline
    except ImportError as e:
        print(f"Failed to load pipeline modules: {e}")
        return

    while True:
        try:
            res = requests.get(f"{API_BASE_URL}/api/v1/worker/poll", params={"user_id": USER_ID}, timeout=10)
            if res.status_code == 200:
                data = res.json()
                job = data.get("job")
                
                if job:
                    job_id = job["job_id"]
                    niche = job["niche"]
                    print(f"\n[!] Picked up new job: {job_id} (Niche: {niche})")
                    
                    try:
                        # Run the heavy pipeline synchronously
                        # The update_job_status function inside worker.py will ping the cloud API
                        run_clip_pipeline(niche, USER_ID, job_id)
                    except Exception as pipeline_err:
                        print(f"Pipeline error: {pipeline_err}")
                        requests.post(f"{API_BASE_URL}/api/v1/worker/complete", json={
                            "job_id": job_id,
                            "status": "error",
                            "message": str(pipeline_err)
                        }, params={"user_id": USER_ID})
                    
                    print("Job complete. Resuming polling...")
        except requests.exceptions.RequestException:
            # Silent fail for network timeouts
            pass
        except Exception as e:
            print(f"Unexpected polling error: {e}")
            
        time.sleep(3) # Poll every 3 seconds

if __name__ == "__main__":
    update_yt_dlp()
    run_worker_loop()
