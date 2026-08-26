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


import re

def parse_time(ts_str):
    parts = ts_str.strip().split(':')
    if len(parts) == 3:
        h, m, s = parts
    else:
        h = '00'
        m, s = parts
    sec, ms = s.split('.') if '.' in s else (s, '000')
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000.0

def format_ass_time(sec):
    if sec < 0: sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"

def generate_ass_subtitle(vtt_path: str, start_sec: int, duration: int, output_ass: str):
    try:
        with open(vtt_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        print(f"Error reading VTT: {e}")
        return False
        
    ass_header = '''[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hormozi,Arial,85,&H0000FFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,3,2,10,10,750,1
Style: HormoziWhite,Arial,85,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,3,2,10,10,750,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    events = []
    blocks = re.findall(r'(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3})\n((?:.|\n)*?)(?=\n\n|\Z)', content)
    end_sec = start_sec + duration
    use_yellow = True
    
    power_words = {
        "money": ("{\\c&H00FF00&}", "💸"),
        "crazy": ("{\\c&H0000FF&}", "🤯"),
        "secret": ("{\\c&H00FFFF&}", "🤫"),
        "viral": ("{\\c&H0000FF&}", "🚀"),
        "win": ("{\\c&H00FF00&}", "🏆"),
        "stop": ("{\\c&H0000FF&}", "🛑")
    }

    for start_ts, end_ts, text in blocks:
        t_start = parse_time(start_ts)
        t_end = parse_time(end_ts)
        if t_end < start_sec or t_start > end_sec:
            continue
        t_start -= start_sec
        t_end -= start_sec
        text = text.strip().replace('\n', ' ')
        if not text:
            continue
            
        # Highlight power words
        lower_text = text.lower()
        for word, (color_code, emoji) in power_words.items():
            if word in lower_text:
                # Replace exact word ignoring case with colored version + emoji (re imported at top)
                text = re.sub(rf'\b({word})\b', rf'{color_code}\1{{\\r}}{emoji}', text, flags=re.IGNORECASE)

        style = "Hormozi" if use_yellow else "HormoziWhite"
        use_yellow = not use_yellow
        events.append(f"Dialogue: 0,{format_ass_time(t_start)},{format_ass_time(t_end)},{style},,0,0,0,,{text}")

    if not events: return False
    try:
        # Changed font to Impact for bolder, higher quality Hormozi look, size to 95 to prevent overflow, MarginV to 550
        ass_header = ass_header.replace("Arial,85", "Impact,95").replace("750,1", "550,1")
        with open(output_ass, 'w', encoding='utf-8') as f:
            f.write(ass_header + '\n'.join(events))
        return True
    except:
        return False

def cut_and_format_clip(
    video_path: str,
    start_sec: int,
    end_sec: int,
    caption: str,
    output_filename: str = None,
    watermark: str = None,
    sub_path: str = None,
) -> str:
    """
    Cuts a clip, formats it to 9:16 using a cinematic blurred background,
    applies speed+color transformation, and burns a caption, watermark, and subtitles.
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
    
    # Process ASS Subtitles
    ass_filter = ""
    if sub_path and os.path.exists(sub_path):
        ass_path = os.path.join(OUTPUT_DIR, f"subs_{int(time.time())}.ass")
        if generate_ass_subtitle(sub_path, start_sec, duration, ass_path):
            # Escape path for ffmpeg filter
            safe_ass = ass_path.replace("\\", "/").replace(":", "\\:")
            ass_filter = f",subtitles={safe_ass}"

    # ─── Filter Complex (Ultra-Fast Cinematic Blur Background) ───────────
    # Replaced CPU-heavy boxblur with scale down/scale up trick (100x faster)
    filter_complex = (
        "[0:v]setpts=PTS/1.1,split=2[bg][fg]; "
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,scale=108:192,scale=1080:1920:flags=bilinear,eq=brightness=-0.15[bg_blurred]; "
        # Scale foreground, then apply a slow continuous 15% zoom over 60 seconds (pattern interrupt)
        "[fg]scale=1080:1920:force_original_aspect_ratio=decrease,zoompan=z='min(zoom+0.0015,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1080x1920[fg_zoomed]; "
        "[bg_blurred][fg_zoomed]overlay=(W-w)/2:(H-h)/2[merged]; "
        "[merged]eq=contrast=1.02:saturation=1.04,"
        f"drawtext=text='{safe_caption}':fontsize=38:fontcolor=white:borderw=2:bordercolor=black:x=(w-text_w)/2:y=h-text_h-350:font=Arial Bold:box=1:boxcolor=black@0.55:boxborderw=14:fix_bounds=1,"
        f"drawtext=text='{safe_watermark}':fontsize=26:fontcolor=white@0.70:borderw=1:bordercolor=black@0.5:x=40:y=80:font=Arial:fix_bounds=1"
        f"{ass_filter}[v_out]"
    )

    # ─── Audio Filter (10% Speedup for Pacing/Silence Reduction) ───────────
    af_filter = "atempo=1.1"

    cmd = [
        FFMPEG,
        "-y",
        "-ss", str(start_sec),
        "-t", str(duration),
        "-i", video_path,
        "-filter_complex", filter_complex,
        "-map", "[v_out]",
        "-map", "0:a",
        "-af", af_filter,
        "-c:v", "libx264",
        "-preset", "veryfast",   # was 'medium' — 4x faster encode
        "-crf", "23",            # was 18 — standard quality, half the file size
        "-c:a", "aac",
        "-b:a", "128k",          # was 192k — plenty for Shorts audio
        "-threads", "0",         # use all available CPU cores
        "-movflags", "+faststart",
        output_path,
    ]

    print(f"[ClipCutter] Processing {start_sec}s-{end_sec}s ({duration}s) | '{caption}'")
    print(f"[ClipCutter] Preset: veryfast | CRF: 23 | Threads: auto")

    try:
        result = subprocess.run(cmd, timeout=600, check=True, capture_output=True)
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"[ClipCutter] Done: {output_path} ({size_mb:.1f} MB)")
        return output_path
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="replace")[:800]
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
    sub_path: str = None,
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

        if num_parts > 1:
            part_caption = f"{caption} (Part {i+1})"
        else:
            part_caption = caption
        
        print(f"\n[ClipCutter] --- Generating Part {i+1}/{num_parts} ---")
        path = cut_and_format_clip(
            video_path=video_path,
            start_sec=part_start,
            end_sec=part_end,
            caption=part_caption,
            output_filename=f"clip_{int(time.time())}_part{i+1}.mp4",
            watermark=watermark,
            sub_path=sub_path,
        )
        if path:
            clip_paths.append(path)

    return clip_paths
