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

def get_best_h264_encoder() -> tuple[str, list[str]]:
    """
    Probes ffmpeg to find the best available hardware accelerated h264 encoder.
    Returns a tuple: (encoder_name, [extra_args])
    """
    try:
        res = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        out = res.stdout
        # NVIDIA NVENC (Ultra-fast dedicated GPU hardware encoder on RTX 2050)
        if "h264_nvenc" in out:
            print("[ClipCutter] Found NVIDIA GPU! Engaging RTX NVENC hardware acceleration.")
            return "h264_nvenc", ["-preset", "p1", "-tune", "ll", "-cq", "24", "-spatial-aq", "1"]
        # Intel QSV (QuickSync on 13th Gen Intel Core i5)
        elif "h264_qsv" in out:
            print("[ClipCutter] Found Intel GPU! Engaging QuickSync (QSV) hardware acceleration.")
            return "h264_qsv", ["-preset", "veryfast", "-q", "23"]
        # AMD AMF
        elif "h264_amf" in out:
            print("[ClipCutter] Found AMD GPU! Engaging AMF hardware acceleration.")
            return "h264_amf", ["-quality", "speed", "-rc", "cqp", "-qp_i", "23"]
    except Exception as e:
        print(f"[ClipCutter] Hardware probe warning: {e}")
        
    print("[ClipCutter] Using multi-threaded CPU processing across all cores (libx264 ultrafast).")
    return "libx264", ["-preset", "ultrafast", "-crf", "22", "-threads", "0"]

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

def generate_ass_subtitle(vtt_path: str, start_sec: int, duration: int, output_ass: str, subtitle_style: str = "hormozi"):
    try:
        with open(vtt_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        print(f"Error reading VTT: {e}")
        return False
        
    is_clean = (subtitle_style == "clean_minimal")
    font_name = "Arial" if is_clean else "Impact"
    font_size = "75" if is_clean else "95"
    margin_v = "450" if is_clean else "550"
    outline_w = "3" if is_clean else "6"

    ass_header = f'''[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hormozi,{font_name},{font_size},&H0000FFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline_w},3,2,10,10,{margin_v},1
Style: HormoziWhite,{font_name},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline_w},3,2,10,10,{margin_v},1

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
            
        # Highlight power words if in Hormozi mode
        if not is_clean:
            lower_text = text.lower()
            for word, (color_code, emoji) in power_words.items():
                if word in lower_text:
                    text = re.sub(rf'\b({word})\b', rf'{color_code}\1{{\\r}}{emoji}', text, flags=re.IGNORECASE)

        style = "Hormozi" if use_yellow else "HormoziWhite"
        use_yellow = not use_yellow
        events.append(f"Dialogue: 0,{format_ass_time(t_start)},{format_ass_time(t_end)},{style},,0,0,0,,{text}")

    if not events: return False
    try:
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
    broll_path: str = None,
    subtitle_style: str = "hormozi",
) -> str:
    """
    Cuts a clip, formats it to 9:16 using either a cinematic blurred background OR a split-screen 
    B-Roll mode if broll_path is provided. Applies speed+color transformation, and burns subtitles.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if not output_filename:
        output_filename = f"clip_{int(time.time())}.mp4"

    output_path = os.path.join(OUTPUT_DIR, output_filename)

    # YouTube Shorts must be under 60 seconds — cap at 56s to be safe
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
        if generate_ass_subtitle(sub_path, start_sec, duration, ass_path, subtitle_style=subtitle_style):
            # Escape path for ffmpeg filter
            safe_ass = ass_path.replace("\\", "/").replace(":", "\\:")
            ass_filter = f",subtitles={safe_ass}"

    # ─── Filter Complex ───────────
    # Auto-detect GPU hardware encoder (runs fast, cached per-clip)
    encoder, encoder_args = get_best_h264_encoder()

    if broll_path and os.path.exists(broll_path):
        # Split-Screen Mode (Top: Original, Bottom: B-Roll)
        import random
        broll_start = random.randint(0, 60)
        
        filter_complex = (
            "[0:v]setpts=PTS/1.1,"
            "scale=1080:960:force_original_aspect_ratio=increase:flags=fast_bilinear,crop=1080:960[top]; "
            "[1:v]scale=1080:960:force_original_aspect_ratio=increase:flags=fast_bilinear,crop=1080:960[bottom]; "
            "[top][bottom]vstack=inputs=2[merged]; "
            "[merged]eq=contrast=1.04:saturation=1.06:gamma=0.98,vignette=PI/5,"
            f"drawtext=text='{safe_caption}':fontsize=38:fontcolor=white:borderw=2:bordercolor=black:x=(w-text_w)/2:y=(h/2)-text_h-20:font=Arial Bold:box=1:boxcolor=black@0.55:boxborderw=14:fix_bounds=1,"
            f"drawtext=text='{safe_watermark}':fontsize=26:fontcolor=white@0.70:borderw=1:bordercolor=black@0.5:x=40:y=80:font=Arial:fix_bounds=1"
            f"{ass_filter}[v_out]"
        )
        
        # Professional acoustic transform (shifts audio fingerprint while preserving speech naturalness)
        audio_filter = "asetrate=44100*1.03,aresample=44100,atempo=1.07,equalizer=f=1000:t=q:w=1:g=1.5"

        cmd = [
            FFMPEG, "-y",
            "-threads", "0",
            "-ss", str(start_sec), "-t", str(duration), "-i", video_path,
            "-stream_loop", "-1", "-ss", str(broll_start), "-t", str(duration), "-i", broll_path,
            "-filter_complex", filter_complex,
            "-map", "[v_out]",
            "-map", "0:a",
            "-af", audio_filter,
            "-r", "30",
            "-pix_fmt", "yuv420p",
            "-c:v", encoder, *encoder_args,
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            output_path,
        ]
    else:
        # Standard Cinematic Blur Mode
        filter_complex = (
            "[0:v]setpts=PTS/1.1,split=2[bg][fg]; "
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase:flags=fast_bilinear,crop=1080:1920,scale=108:192:flags=fast_bilinear,scale=1080:1920:flags=fast_bilinear,eq=brightness=-0.15[bg_blurred]; "
            "[fg]scale=1080:1920:force_original_aspect_ratio=decrease:flags=fast_bilinear,zoompan=z='min(zoom+0.0015,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1080x1920:fps=30[fg_zoomed]; "
            "[bg_blurred][fg_zoomed]overlay=(W-w)/2:(H-h)/2[merged]; "
            "[merged]eq=contrast=1.04:saturation=1.06:gamma=0.98,vignette=PI/5,"
            f"drawtext=text='{safe_caption}':fontsize=38:fontcolor=white:borderw=2:bordercolor=black:x=(w-text_w)/2:y=h-text_h-350:font=Arial Bold:box=1:boxcolor=black@0.55:boxborderw=14:fix_bounds=1,"
            f"drawtext=text='{safe_watermark}':fontsize=26:fontcolor=white@0.70:borderw=1:bordercolor=black@0.5:x=40:y=80:font=Arial:fix_bounds=1"
            f"{ass_filter}[v_out]"
        )
        
        audio_filter = "asetrate=44100*1.03,aresample=44100,atempo=1.07,equalizer=f=1000:t=q:w=1:g=1.5"

        cmd = [
            FFMPEG, "-y",
            "-threads", "0",
            "-ss", str(start_sec), "-t", str(duration), "-i", video_path,
            "-filter_complex", filter_complex,
            "-map", "[v_out]",
            "-map", "0:a",
            "-af", audio_filter,
            "-r", "30",
            "-pix_fmt", "yuv420p",
            "-c:v", encoder, *encoder_args,
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            output_path,
        ]

    print(f"[ClipCutter] Processing {start_sec}s-{end_sec}s ({duration}s) | '{caption}'")
    print(f"[ClipCutter] Encoder: {encoder} | Mode: {'split-screen' if broll_path else 'cinematic blur'}")

    try:
        result = subprocess.run(cmd, timeout=600, check=True, capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"[ClipCutter] Done: {output_path} ({size_mb:.1f} MB)")
        return output_path
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="replace")[:800]
        print(f"[ClipCutter] ffmpeg error:\n{err}")
        return ""
    except subprocess.TimeoutExpired:
        print("[ClipCutter] ffmpeg timed out (600s limit).")
        return ""


def cut_clip(
    video_path: str,
    start_sec: int,
    end_sec: int,
    caption: str,
    watermark: str = None,
    sub_path: str = None,
    broll_path: str = None,
    subtitle_style: str = "hormozi",
) -> str:
    """
    Cuts a single, high-impact standalone viral Short (30-55s).
    Just like Opus Clip / Munch, prioritizes one complete punchy clip.
    """
    out_name = f"clip_{int(time.time())}.mp4"
    # Cap duration at 55 seconds for standard Shorts
    actual_end = min(start_sec + 55, end_sec)
    if actual_end <= start_sec:
        actual_end = start_sec + 45

    return cut_and_format_clip(
        video_path=video_path,
        start_sec=start_sec,
        end_sec=actual_end,
        caption=caption,
        output_filename=out_name,
        watermark=watermark,
        sub_path=sub_path,
        broll_path=broll_path,
        subtitle_style=subtitle_style,
    )

def cut_multipart_clips(*args, **kwargs) -> list[str]:
    """Single-video compatibility: always produces 1 clean standalone Short."""
    kwargs.pop("num_parts", None)
    path = cut_clip(*args, **kwargs)
    return [path] if path else []
