import os
import sys
import time
import requests
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Immediately hide console window on Windows if spawned with one
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32")
        user32 = ctypes.WinDLL("user32")
        hWnd = kernel32.GetConsoleWindow()
        if hWnd:
            user32.ShowWindow(hWnd, 0) # SW_HIDE
    except Exception:
        pass

# Ensure pystray and pillow are available
try:
    import pystray
    from PIL import Image, ImageDraw
    import winreg as reg
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "pystray", "Pillow"], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    import pystray
    from PIL import Image, ImageDraw
    import winreg as reg

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

def get_user_id():
    local_storage_path = Path.home() / ".clipai" / "user_id.txt"
    
    # 1. If passed as arg via URI handler from the website (e.g. clipai://start?user_id=user_12345)
    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            if arg.startswith("clipai://"):
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(arg)
                qs = parse_qs(parsed.query)
                if "user_id" in qs:
                    uid = qs["user_id"][0]
                    # Save it so future manual double-clicks work
                    local_storage_path.parent.mkdir(parents=True, exist_ok=True)
                    local_storage_path.write_text(uid)
                    return uid

    # 2. Check local storage file
    if local_storage_path.exists():
        stored_id = local_storage_path.read_text().strip()
        if stored_id and "@" not in stored_id: # Ignore old broken emails
            return stored_id

    # 3. If no ID found, force them to use the website
    print("\n" + "="*60)
    print("Welcome to ClipAI Desktop Worker!")
    print("="*60)
    print("ERROR: No linked account found.")
    print("Please go to your web dashboard and click 'Start Worker'.")
    print("This will securely link your browser session to this app.")
    print("="*60)
    time.sleep(10)
    os._exit(1)

USER_ID = get_user_id()
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
            subprocess.run([str(yt_dlp_exe), "-U"], check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        print("yt-dlp is up to date!")
    except Exception as e:
        print(f"Failed to setup yt-dlp: {e}")

def sync_live_pipeline_scripts():
    """Dynamically fetches the latest production fixes from the cloud server and updates local modules in real-time."""
    try:
        res = requests.get(f"{API_BASE_URL}/api/v1/worker/scripts", timeout=10)
        if res.status_code == 200:
            data = res.json()
            scripts = data.get("scripts", {})
            for name, code in scripts.items():
                target_path = Path(__file__).resolve().parent / name
                try:
                    target_path.write_text(code, encoding="utf-8")
                except Exception:
                    pass
            print(f"[Worker] Live hot-sync complete: {len(scripts)} pipeline scripts updated to latest version.")
    except Exception as e:
        print(f"[Worker] Live hot-sync warning (offline/cached): {e}")

def run_worker_loop():
    global is_running
    print(f"Starting ClipAI Companion Worker for user: {USER_ID}")
    
    # Always pull latest fixes live before starting loop
    sync_live_pipeline_scripts()

    try:
        import importlib
        import worker
        import hot_pipeline
        importlib.reload(worker)
        importlib.reload(hot_pipeline)
        from worker import run_clip_pipeline
        # Speculatively warm cache for top niches in the background with zero user wait
        for default_niche in ["motivation", "finance", "gaming"]:
            hot_pipeline.trigger_replenish(default_niche, is_free_tier=False)
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
                    # Use the user_id from the job payload so each customer's job is tracked correctly
                    job_user_id = job.get("user_id", USER_ID)
                    is_free_tier = job.get("is_free_tier", False)
                    auto_upload = job.get("auto_upload", True)
                    layout = job.get("layout", "split_screen")
                    subtitle_style = job.get("subtitle_style", "hormozi")
                    print(f"\n[!] Picked up new job: {job_id} (Niche: {niche}, User: {job_user_id}, Layout: {layout}, Subtitles: {subtitle_style}, Free Tier: {is_free_tier}, Auto Upload: {auto_upload})")
                    
                    try:
                        # Run the heavy pipeline synchronously
                        run_clip_pipeline(niche, job_user_id, job_id, is_free_tier, auto_upload=auto_upload, layout=layout, subtitle_style=subtitle_style)
                    except Exception as pipeline_err:
                        print(f"Pipeline error: {pipeline_err}")
                        requests.post(f"{API_BASE_URL}/api/v1/worker/complete", json={
                            "job_id": job_id, "status": "error", "message": str(pipeline_err)
                        }, params={"user_id": job_user_id})
        except requests.exceptions.RequestException:
            pass
        except Exception as e:
            print(f"Unexpected polling error: {e}")
        time.sleep(3)

def set_autostart(enable=True):
    key = reg.HKEY_CURRENT_USER
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        registry_key = reg.OpenKey(key, key_path, 0, reg.KEY_ALL_ACCESS)
        if enable:
            # We add quotes around sys.executable to ensure paths with spaces work safely
            cmd = f'"{sys.executable}"'
            reg.SetValueEx(registry_key, "ClipAI_Worker", 0, reg.REG_SZ, cmd)
        else:
            try:
                reg.DeleteValue(registry_key, "ClipAI_Worker")
            except FileNotFoundError:
                pass
        reg.CloseKey(registry_key)
    except Exception as e:
        print(f"Failed to set startup: {e}")

def check_autostart():
    key = reg.HKEY_CURRENT_USER
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        registry_key = reg.OpenKey(key, key_path, 0, reg.KEY_READ)
        value, _ = reg.QueryValueEx(registry_key, "ClipAI_Worker")
        reg.CloseKey(registry_key)
        return True
    except FileNotFoundError:
        return False

def create_image():
    # Generate a sleek crimson icon for the system tray
    image = Image.new('RGB', (64, 64), color=(220, 38, 38))
    d = ImageDraw.Draw(image)
    d.rectangle([16, 16, 48, 48], fill="white")
    return image

def setup_tray():
    autostart_enabled = check_autostart()

    def toggle_autostart(icon, item):
        nonlocal autostart_enabled
        autostart_enabled = not autostart_enabled
        set_autostart(autostart_enabled)
        # Update menu
        icon.update_menu()

    def quit_action(icon, item):
        global is_running
        is_running = False
        icon.stop()
        os._exit(0)
        
    menu = pystray.Menu(
        pystray.MenuItem("Worker Active (🟢)", lambda: None, enabled=False),
        pystray.MenuItem("Run on Startup", toggle_autostart, checked=lambda item: autostart_enabled),
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
    
    if "--cli" in sys.argv:
        print("Running in CLI mode. Press Ctrl+C to exit.")
        while True:
            time.sleep(1)
    else:
        # Run system tray
        setup_tray()
