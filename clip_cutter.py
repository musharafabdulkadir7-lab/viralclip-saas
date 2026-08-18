"""
clip_cutter.py
Uses ffmpeg to cut a clip from a video at specific timestamps,
crop it to 9:16 vertical format for YouTube Shorts,
apply visual transformations to avoid Content ID flags,
and burn captions + watermark onto the video.

Anti-"Unoriginal Content" measures applied:
  1. 1.05x speed change - shifts audio/video fingerprint
  2. Color grade (contrast + saturation boost) - visual transformation
  3. Slight zoom crop - changes framing
  4. Caption overlay - adds unique text element
  5. Channel watermark - branding differentiates from source
"""
import subprocess
import os
import sys
import time

from pathlib import Path
OUTPUT_DIR = os.path.join(str(Path.home() / ".clipai"), "generated_videos")

# Dynamically locate ffmpeg: system on Linux, bundled on Windows
def _get_ffmpeg() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, "bin", "ffmpeg.exe")
    if sys.platform == "win32":
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return "ffmpeg"
    else:
        return "/usr/bin/ffmpeg"

FFMPEG = _get_ffmpeg()

# Channel watermark text — buyers should change this to their channel name
WATERMARK_TEXT = "@FinanceClips"


def cut_and_format_clip(
    video_path: str,
    start_sec: int,
    end_sec: int,
    caption: str,
    output_filename: str = None,
    watermark: str = None,
) -> str:
    """
    Cuts a clip, crops to 9:16, applies speed+color transformation,
    burns a caption and watermark. Returns the output path.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if not output_filename:
        output_filename = f"clip_{int(time.time())}.mp4"

    output_path = os.path.join(OUTPUT_DIR, output_filename)

    # YouTube Shorts must be under 60 seconds — cap at 56s to be safe
    # (1.05x speed means actual duration = duration/1.05, so 56s -> ~53s)
    duration = min(end_sec - start_sec, 56)

    # Hard-limit caption to 26 chars so it always fits at 32px on 1080px wide video
    if len(caption) > 26:
        caption = caption[:23] + "..."

    # Escape caption text for ffmpeg drawtext filter
    def esc(text):
        return text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:").replace(",", "\\,")

    safe_caption = esc(caption)
    safe_watermark = esc(watermark or WATERMARK_TEXT)

    # ─── Video Filter Chain ──────────────────────────────────────────────
    # setpts=PTS/1.05   → 1.05x speed (changes video timing/fingerprint)
    # crop=ih*9/16:ih   → center crop to 9:16
    # scale=1080:1920   → upscale to Shorts resolution
    # eq=...            → color grade: toned down to look natural but slightly tweaked
    # unsharp=...       → adds a subtle crispness/quality improvement
    # drawtext #1       → main caption at bottom center
    # drawtext #2       → small watermark in top-left corner
    vf_filter = (
        "setpts=PTS/1.05,"
        "crop=ih*9/16:ih,"
        "scale=1080:1920,"
        "eq=contrast=1.03:brightness=0.01:saturation=1.06,"
        "unsharp=5:5:1.0:5:5:0.0,"
        f"drawtext=text='{safe_caption}':"
        "fontsize=36:"
        "fontcolor=white:"
        "borderw=2:"
        "bordercolor=black:"
        "x=(w-text_w)/2:"
        "y=h-text_h-180:"
        "font=Arial Bold:"
        "box=1:"
        "boxcolor=black@0.60:"
        "boxborderw=16:"
        "fix_bounds=1,"
        f"drawtext=text='{safe_watermark}':"
        "fontsize=26:"
        "fontcolor=white@0.75:"
        "borderw=1:"
        "bordercolor=black@0.5:"
        "x=24:"
        "y=50:"
        "font=Arial:"
        "fix_bounds=1"
    )

    # ─── Audio Filter ─────────────────────────────────────────────────────
    # atempo=1.05 → 1.05x audio speed (shifts audio fingerprint to avoid Content ID)
    af_filter = "atempo=1.05"

    cmd = [
        FFMPEG,
        "-y",
        "-ss", str(start_sec),
        "-i", video_path,
        "-t", str(duration),
        "-vf", vf_filter,
        "-af", af_filter,
        "-c:v", "libx264",
        "-preset", "medium",  # Better compression quality than 'fast'
        "-crf", "18",         # Visually lossless quality (default was 23)
        "-c:a", "aac",
        "-b:a", "192k",       # Better audio bitrate
        "-movflags", "+faststart",
        output_path,
    ]

    print(f"[ClipCutter] Processing {start_sec}s-{end_sec}s ({duration}s) | '{caption}'")
    print(f"[ClipCutter] Applying: 1.05x speed, color grade, caption, watermark")

    try:
        result = subprocess.run(cmd, timeout=300, check=True, capture_output=True)
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"[ClipCutter] Done: {output_path} ({size_mb:.1f} MB)")
        return output_path
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="replace")[:600]
        print(f"[ClipCutter] ffmpeg error:\n{err}")
        return ""
    except subprocess.TimeoutExpired:
        print("[ClipCutter] ffmpeg timed out (300s limit).")
        return ""


def cut_multipart_clips(
    video_path: str,
    start_sec: int,
    end_sec: int,
    caption: str,
    num_parts: int,
    watermark: str = None,
) -> list[str]:
    """
    Cuts a longer segment into multiple parts (Part 1, Part 2, etc.)
    Returns a list of paths to the generated clips.
    """
    clip_paths = []
    total_duration = end_sec - start_sec
    part_duration = total_duration / num_parts

    for i in range(num_parts):
        part_start = start_sec + int(i * part_duration)
        part_end = start_sec + int((i + 1) * part_duration)
        
        # Ensure we don't exceed the 56s cap per part
        if part_end - part_start > 56:
            part_end = part_start + 56

        part_caption = f"{caption} (Part {i+1})"
        
        print(f"\n[ClipCutter] --- Generating Part {i+1}/{num_parts} ---")
        path = cut_and_format_clip(
            video_path=video_path,
            start_sec=part_start,
            end_sec=part_end,
            caption=part_caption,
            watermark=watermark
        )
        if path:
            clip_paths.append(path)

    return clip_paths
