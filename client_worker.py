import os
import sys
import time
import requests
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Ensure pystray and pillow are available
try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "pystray", "Pillow"], check=True)
    import pystray
    from PIL import Image, ImageDraw

# Setup cross-platform paths
HOME_DIR = Path.home() / ".clipai"
BIN_DIR = HOME_DIR / "bin"
os.makedirs(BIN_DIR, exist_ok=True)

# Update system path so subprocesses can find bin files easily
os.environ["PATH"] += os.pathsep + str(BIN_DIR)

API_BASE_URL = os.environ.get("API_BASE_URL", "https://viralclip-saas.onrender.com")
# Force inject it so imported modules like worker.py use it
os.environ["API_BASE_URL"] = API_BASE_URL

# For testing locally, uncomment the line below:
# API_BASE_URL = "http://localhost:8000"
# os.environ["API_BASE_URL"] = API_BASE_URL

USER_ID = os.environ.get("CLIPAI_USER_ID", "demo_user_123")
is_running = True

def register_uri_scheme():
    """Register the clipai:// protocol handler in Windows Registry."""
    if sys.platform != "win32":
        print("URI Registration only supported on Windows currently.")
        return
        
    import winreg
    try:
        # HKEY_CURRENT_USER\Software\Classes\clipai
        key_path = r"Software\Classes\clipai"
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
        winreg.SetValue(key, "", winreg.REG_SZ, "URL:ClipAI Protocol")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        
        # Path to this executable
        exe_path = sys.executable if getattr(sys, 'frozen', False) else f'"{sys.executable}" "{os.path.abspath(__file__)}"'
        
        # shell\open\command
        cmd_key = winreg.CreateKey(key, r"shell\open\command")
        winreg.SetValue(cmd_key, "", winreg.REG_SZ, f'{exe_path} "%1"')
        
        winreg.CloseKey(cmd_key)
        winreg.CloseKey(key)
        print("Successfully registered clipai:// protocol handler.")
    except Exception as e:
        print(f"Failed to register URI scheme: {e}")

def update_yt_dlp():
    """Download or auto-update standalone yt-dlp binary."""
    print("Checking for yt-dlp updates...")
    yt_dlp_exe = BIN_DIR / ("yt-dlp.exe" if sys.platform == "win32" else "yt-dlp")
    try:
        if not yt_dlp_exe.exists():
            print("yt-dlp not found. Downloading latest standalone binary...")
            url = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" if sys.platform == "win32" else "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos"
            response = requests.get(url, stream=True)
            with open(yt_dlp_exe, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            if sys.platform != "win32":
                os.chmod(yt_dlp_exe, 0o755)
        else:
            subprocess.run([str(yt_dlp_exe), "-U"], check=True, capture_output=True)
        print("yt-dlp is up to date!")
    except Exception as e:
        print(f"Failed to setup yt-dlp: {e}")

def run_worker_loop():
    global is_running
    print(f"Starting ClipAI Companion Worker for user: {USER_ID}")
    
    try:
        from worker import run_clip_pipeline
    except ImportError as e:
        print(f"Failed to load pipeline modules: {e}")
        return

    while is_running:
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
                        run_clip_pipeline(niche, USER_ID, job_id)
                    except Exception as pipeline_err:
                        print(f"Pipeline error: {pipeline_err}")
                        requests.post(f"{API_BASE_URL}/api/v1/worker/complete", json={
                            "job_id": job_id, "status": "error", "message": str(pipeline_err)
                        }, params={"user_id": USER_ID})
        except requests.exceptions.RequestException:
            pass
        except Exception as e:
            print(f"Unexpected polling error: {e}")
        time.sleep(3)

def create_image():
    # Generate a simple blue icon for the system tray
    image = Image.new('RGB', (64, 64), color=(59, 130, 246))
    d = ImageDraw.Draw(image)
    d.rectangle([16, 16, 48, 48], fill="white")
    return image

def setup_tray():
    def quit_action(icon, item):
        global is_running
        is_running = False
        icon.stop()
        os._exit(0)
        
    menu = pystray.Menu(
        pystray.MenuItem("Worker Active (🟢)", lambda: None, enabled=False),
        pystray.MenuItem("Quit", quit_action)
    )
    icon = pystray.Icon("ClipAI", create_image(), "ClipAI Worker", menu)
    icon.run()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == "--register":
            register_uri_scheme()
            sys.exit(0)
        elif arg.startswith("clipai://"):
            # e.g. clipai://start?user_id=123
            parsed = urlparse(arg)
            qs = parse_qs(parsed.query)
            if "user_id" in qs:
                USER_ID = qs["user_id"][0]
    else:
        # If the user just double-clicks the app, always ensure the URI scheme is registered
        register_uri_scheme()
                
    update_yt_dlp()
    
    # Run the worker loop in a background thread so the system tray can block the main thread
    worker_thread = threading.Thread(target=run_worker_loop, daemon=True)
    worker_thread.start()
    
    # Run system tray
    setup_tray()
