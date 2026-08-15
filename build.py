import os
import sys
import subprocess
import requests
import shutil
from pathlib import Path

# Paths
BUILD_DIR = Path("build_output")
BIN_DIR = BUILD_DIR / "bin"
DIST_DIR = Path("dist")

def download_binaries():
    """Download ffmpeg and yt-dlp standalone binaries."""
    print("Preparing binaries for packaging...")
    os.makedirs(BIN_DIR, exist_ok=True)
    
    # 1. Download yt-dlp.exe
    yt_dlp_path = BIN_DIR / "yt-dlp.exe"
    if not yt_dlp_path.exists():
        print("Downloading yt-dlp.exe...")
        res = requests.get("https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe", stream=True)
        with open(yt_dlp_path, "wb") as f:
            for chunk in res.iter_content(8192):
                f.write(chunk)
                
    # 2. Download ffmpeg.exe (using imageio_ffmpeg wrapper to grab it locally if possible)
    ffmpeg_path = BIN_DIR / "ffmpeg.exe"
    if not ffmpeg_path.exists():
        print("Locating ffmpeg.exe...")
        try:
            import imageio_ffmpeg
            bundled_ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            shutil.copy(bundled_ffmpeg, ffmpeg_path)
            print("Copied ffmpeg from imageio_ffmpeg")
        except Exception as e:
            print(f"Error getting ffmpeg: {e}")
            print("Please place ffmpeg.exe in build_output/bin/ manually.")

def run_pyinstaller():
    """Run PyInstaller to create the executable."""
    print("Running PyInstaller...")
    
    # We use subprocess to run PyInstaller
    # --onefile creates a single exe
    # --add-data bundles our bin folder
    # --noconsole hides the ugly black terminal box (optional, but let's keep it for debug for now)
    
    args = [
        sys.executable, "-m", "PyInstaller",
        "--name", "ClipAI_Worker",
        "--onefile",
        "--add-data", f"{BIN_DIR}{os.pathsep}bin",
        "--paths", str(Path("..") / "youtube_ai_agent"),
        "client_worker.py"
    ]
    
    subprocess.run(args, check=True)
    print("Build complete! Check the 'dist' folder for ClipAI_Worker.exe")

if __name__ == "__main__":
    download_binaries()
    run_pyinstaller()
