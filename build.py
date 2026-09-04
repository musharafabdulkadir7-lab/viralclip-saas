import os
import sys
import subprocess
import requests
import shutil
from pathlib import Path

# Paths
BUILD_DIR = Path("build_output")
BIN_DIR = BUILD_DIR / "bin"

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
                
    # 2. Download ffmpeg.exe via imageio_ffmpeg
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

    # Collect all the AI pipeline scripts in this directory to bundle explicitly
    agent_scripts = [
        "worker.py",
        "hot_pipeline.py",
        "video_finder.py",
        "video_downloader.py",
        "clip_finder.py",
        "clip_cutter.py",
        "youtube_uploader.py",
    ]

    # Build --add-data args for each agent script (bundle alongside binary)
    add_data_args = []
    for script in agent_scripts:
        if Path(script).exists():
            add_data_args += ["--add-data", f"{script}{os.pathsep}."]

    # Also add hidden imports so PyInstaller doesn't miss dynamically-imported modules
    hidden_imports = [
        "--hidden-import", "video_finder",
        "--hidden-import", "video_downloader",
        "--hidden-import", "clip_finder",
        "--hidden-import", "clip_cutter",
        "--hidden-import", "youtube_uploader",
        "--hidden-import", "worker",
        "--hidden-import", "pystray",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageDraw",
    ]

    args = [
        sys.executable, "-m", "PyInstaller",
        "--name", "ClipAI_Worker",
        "--onefile",
        "--add-data", f"{BIN_DIR}{os.pathsep}bin",
    ] + add_data_args + hidden_imports + ["client_worker.py"]
    
    subprocess.run(args, check=True)
    print("Build complete! Check the 'dist' folder for ClipAI_Worker.exe")

if __name__ == "__main__":
    download_binaries()
    run_pyinstaller()
