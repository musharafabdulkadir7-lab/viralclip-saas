"""
clip_cutter.py
Cuts a clip, crops it to 9:16 for Shorts, burns in captions + watermark.

No speed/fingerprint-evasion transforms. Split-screen b-roll must be
supplied explicitly by the caller (owned/licensed) — never auto-fetched.
"""
from __future__ import annotations

import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from config import settings
from logging_setup import get_logger

log = get_logger("clip_cutter")


class ClipCutError(Exception):
    """Raised when ffmpeg fails or times out."""


def _get_ffmpeg() -> str:
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "bin", "ffmpeg.exe")
    if sys.platform == "win32":
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return "ffmpeg"
    return "/usr/bin/ffmpeg"


FFMPEG = _get_ffmpeg()
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def get_best_h264_encoder() -> tuple[str, list[str]]:
    """Probes ffmpeg/hardware and picks the fastest available H.264 encoder."""
    cpu_cores = os.cpu_count() or 4
    try:
        res = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True,
                              errors="replace", creationflags=_NOWIN)
        out = res.stdout

        if "h264_nvenc" in out:
            test = subprocess.run(
                [FFMPEG, "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1", "-c:v", "h264_nvenc", "-f", "null", "-"],
                capture_output=True, creationflags=_NOWIN)
            if test.returncode == 0:
                log.info("Using NVIDIA NVENC hardware encoding (%d threads).", cpu_cores)
                return "h264_nvenc", ["-preset", "p1", "-tune", "ull", "-zerolatency", "1", "-2pass", "0",
                                       "-cq", "23", "-spatial-aq", "1", "-threads", str(cpu_cores)]
        if "h264_videotoolbox" in out:
            log.info("Using Apple VideoToolbox hardware encoding.")
            return "h264_videotoolbox", ["-realtime", "1", "-q:v", "65"]
        if "h264_qsv" in out:
            log.info("Using Intel QuickSync hardware encoding.")
            return "h264_qsv", ["-preset", "veryfast", "-q", "23", "-threads", str(cpu_cores)]
        if "h264_amf" in out:
            log.info("Using AMD AMF hardware encoding.")
            return "h264_amf", ["-quality", "speed", "-rc", "cqp", "-qp_i", "23"]
    except Exception as e:
        log.warning("Hardware encoder probe failed, falling back to CPU: %s", e)

    log.info("Using CPU (libx264) encoding across %d threads.", cpu_cores)
    return "libx264", ["-preset", "ultrafast", "-crf", "22", "-threads", str(cpu_cores), "-slice-max-size", "0"]


def parse_time(ts_str: str) -> float:
    parts = ts_str.strip().split(":")
    h, m, s = ("00", *parts) if len(parts) == 2 else parts
    sec, ms = s.split(".") if "." in s else (s, "000")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000.0


def format_ass_time(sec: float) -> str:
    sec = max(sec, 0)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def generate_ass_subtitle(vtt_path: str, start_sec: int, duration: int, output_ass: str,
                           subtitle_style: str = "bold_captions") -> bool:
    try:
        content = Path(vtt_path).read_text(encoding="utf-8")
    except Exception as e:
        log.error("Failed to read VTT for subtitles: %s", e)
        return False

    is_clean = subtitle_style == "clean_minimal"
    font_name = "Arial" if is_clean else "Impact"
    font_size = "75" if is_clean else "95"
    margin_v = "450" if is_clean else "550"
    outline_w = "3" if is_clean else "6"

    ass_header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Primary,{font_name},{font_size},&H0000FFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline_w},3,2,10,10,{margin_v},1
Style: PrimaryWhite,{font_name},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline_w},3,2,10,10,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    blocks = re.findall(
        r"(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3})\n((?:.|\n)*?)(?=\n\n|\Z)", content)
    end_sec = start_sec + duration
    use_alt = True

    for start_ts, end_ts, text in blocks:
        t_start, t_end = parse_time(start_ts), parse_time(end_ts)
        if t_end < start_sec or t_start > end_sec:
            continue
        t_start -= start_sec
        t_end -= start_sec
        text = text.strip().replace("\n", " ")
        if not text:
            continue
        style = "Primary" if use_alt else "PrimaryWhite"
        use_alt = not use_alt
        events.append(f"Dialogue: 0,{format_ass_time(t_start)},{format_ass_time(t_end)},{style},,0,0,0,,{text}")

    if not events:
        return False
    try:
        Path(output_ass).write_text(ass_header + "\n".join(events), encoding="utf-8")
        return True
    except Exception as e:
        log.error("Failed to write ASS subtitle file: %s", e)
        return False


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:").replace(",", "\\,")


def cut_and_format_clip(
    video_path: str,
    start_sec: int,
    end_sec: int,
    caption: str,
    output_filename: Optional[str] = None,
    watermark: Optional[str] = None,
    sub_path: Optional[str] = None,
    broll_path: Optional[str] = None,  # must be caller-owned/licensed b-roll
    subtitle_style: str = "bold_captions",
) -> str:
    """Cuts a clip and formats it to 9:16. Raises ClipCutError on failure."""
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    output_filename = output_filename or f"clip_{int(time.time())}.mp4"
    output_path = str(settings.output_dir / output_filename)

    duration = min(end_sec - start_sec, settings.max_short_duration_sec)
    if len(caption) > 26:
        caption = caption[:23] + "..."

    safe_caption = _esc(caption)
    safe_watermark = _esc(watermark or settings.default_watermark)

    ass_filter = ""
    if sub_path and os.path.exists(sub_path):
        ass_path = str(settings.output_dir / f"subs_{int(time.time())}.ass")
        if generate_ass_subtitle(sub_path, start_sec, duration, ass_path, subtitle_style=subtitle_style):
            safe_ass = ass_path.replace("\\", "/").replace(":", "\\:")
            ass_filter = f",subtitles={safe_ass}"

    encoder, encoder_args = get_best_h264_encoder()
    cpu_threads = str(os.cpu_count() or 4)

    if broll_path and os.path.exists(broll_path):
        broll_start = random.randint(0, 60)
        filter_complex = (
            "[0:v]scale=1080:960:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:960[top]; "
            "[1:v]scale=1080:960:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:960[bottom]; "
            "[top][bottom]vstack=inputs=2[merged]; "
            "[merged]scale=1080:1920:flags=lanczos,"
            f"drawtext=text='{safe_caption}':fontsize=38:fontcolor=white:borderw=2:bordercolor=black:"
            "x=(w-text_w)/2:y=(h/2)-text_h-20:font=Arial Bold:box=1:boxcolor=black@0.55:boxborderw=14:fix_bounds=1,"
            f"drawtext=text='{safe_watermark}':fontsize=26:fontcolor=white@0.70:borderw=1:bordercolor=black@0.5:"
            f"x=40:y=80:font=Arial:fix_bounds=1{ass_filter}[v_out]"
        )
        cmd = [
            FFMPEG, "-y", "-threads", cpu_threads,
            "-ss", str(start_sec), "-t", str(duration), "-i", video_path,
            "-stream_loop", "-1", "-ss", str(broll_start), "-t", str(duration), "-i", broll_path,
            "-filter_complex", filter_complex, "-map", "[v_out]", "-map", "0:a",
            "-r", "30", "-pix_fmt", "yuv420p", "-c:v", encoder, *encoder_args,
            "-c:a", "aac", "-b:a", "192k", "-map_metadata", "-1", "-movflags", "+faststart", output_path,
        ]
        mode = "split-screen"
    else:
        filter_complex = (
            "[0:v]split=2[bg][fg]; "
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,"
            "boxblur=luma_radius=28:luma_power=2:chroma_radius=14:chroma_power=2,"
            "eq=brightness=-0.12[bg_blurred]; "
            "[fg]scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos[fg_scaled]; "
            "[bg_blurred][fg_scaled]overlay=(W-w)/2:(H-h)/2[merged]; "
            "[merged]scale=1080:1920:flags=lanczos,"
            f"drawtext=text='{safe_caption}':fontsize=38:fontcolor=white:borderw=2:bordercolor=black:"
            "x=(w-text_w)/2:y=h-text_h-350:font=Arial Bold:box=1:boxcolor=black@0.55:boxborderw=14:fix_bounds=1,"
            f"drawtext=text='{safe_watermark}':fontsize=26:fontcolor=white@0.70:borderw=1:bordercolor=black@0.5:"
            f"x=40:y=80:font=Arial:fix_bounds=1{ass_filter}[v_out]"
        )
        cmd = [
            FFMPEG, "-y", "-threads", cpu_threads,
            "-ss", str(start_sec), "-t", str(duration), "-i", video_path,
            "-filter_complex", filter_complex, "-map", "[v_out]", "-map", "0:a",
            "-r", "30", "-pix_fmt", "yuv420p", "-c:v", encoder, *encoder_args,
            "-c:a", "aac", "-b:a", "192k", "-map_metadata", "-1", "-movflags", "+faststart", output_path,
        ]
        mode = "cinematic blur"

    log.info("Processing %ss-%ss (%ss) | %r | encoder=%s mode=%s", start_sec, end_sec, duration, caption, encoder, mode)

    try:
        subprocess.run(cmd, timeout=settings.ffmpeg_timeout_sec, check=True, capture_output=True, creationflags=_NOWIN)
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="replace")[:800]
        raise ClipCutError(f"ffmpeg failed: {err}") from e
    except subprocess.TimeoutExpired as e:
        raise ClipCutError(f"ffmpeg timed out after {settings.ffmpeg_timeout_sec}s") from e

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    log.info("Done: %s (%.1f MB)", output_path, size_mb)
    return output_path


def cut_clip(
    video_path: str,
    start_sec: int,
    end_sec: int,
    caption: str,
    watermark: Optional[str] = None,
    sub_path: Optional[str] = None,
    broll_path: Optional[str] = None,
    subtitle_style: str = "bold_captions",
) -> str:
    out_name = f"clip_{int(time.time())}.mp4"
    actual_end = min(start_sec + 55, end_sec)
    if actual_end <= start_sec:
        actual_end = start_sec + 45
    return cut_and_format_clip(
        video_path=video_path, start_sec=start_sec, end_sec=actual_end, caption=caption,
        output_filename=out_name, watermark=watermark, sub_path=sub_path,
        broll_path=broll_path, subtitle_style=subtitle_style,
    )
