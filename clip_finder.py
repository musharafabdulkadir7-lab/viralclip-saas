"""
clip_finder.py
Reads auto-generated subtitles (VTT format) and calls the backend to
find the single most engaging 25-55 second clip window. Returns
start/end timestamps in seconds plus a caption.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import settings
from logging_setup import get_logger

log = get_logger("clip_finder")

_DEFAULT_START = 60
_DEFAULT_END = 110
_MIN_CLIP_SEC = 25
_MAX_CLIP_SEC = 55


@dataclass
class ClipSegment:
    start_sec: int
    end_sec: int
    caption: str
    num_parts: int = 1


def _fallback_segment(niche: str) -> ClipSegment:
    return ClipSegment(start_sec=_DEFAULT_START, end_sec=_DEFAULT_END, caption=niche.title() or "Clip")


def parse_vtt(vtt_path: str) -> list[dict]:
    """Parses an auto-caption VTT file into [{start, end, text}, ...]."""
    try:
        with open(vtt_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        log.error("Failed to read VTT %s: %s", vtt_path, e)
        return []

    def ts_to_sec(h, m, s, ms):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    blocks = re.split(r"\n\s*\n", content.strip())
    entries: list[dict] = []
    seen_texts: set[str] = set()

    for block in blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue

        ts_line, text_lines = None, []
        for i, line in enumerate(lines):
            if "-->" in line:
                ts_line, text_lines = line, lines[i + 1:]
                break
        if not ts_line:
            continue

        ts_match = re.match(r"(\d+):(\d+):(\d+)[\.,](\d+)\s*-->\s*(\d+):(\d+):(\d+)[\.,](\d+)", ts_line)
        if not ts_match:
            continue

        start = ts_to_sec(*ts_match.groups()[:4])
        end = ts_to_sec(*ts_match.groups()[4:])

        raw_text = " ".join(text_lines)
        raw_text = re.sub(r"<\d+:\d+:\d+[\.,]\d+>", "", raw_text)
        raw_text = re.sub(r"<[^>]+>", "", raw_text)
        raw_text = re.sub(r"\s+", " ", raw_text).strip()

        if not raw_text or raw_text in ("[Music]", "[Applause]"):
            continue
        if raw_text in seen_texts:
            continue
        seen_texts.add(raw_text)

        entries.append({"start": start, "end": end, "text": raw_text})

    log.info("Parsed %d subtitle entries from %s", len(entries), vtt_path)
    return entries


def build_transcript_block(entries: list[dict], max_chars: int = 10_000) -> str:
    lines = []
    total = 0
    for e in entries:
        secs_total = int(e["start"])
        line = f"[{secs_total // 60:02d}:{secs_total % 60:02d}] {e['text']}"
        total += len(line)
        if total > max_chars:
            break
        lines.append(line)
    return "\n".join(lines)


_RETRYABLE = (requests.ConnectionError, requests.Timeout)


@retry(stop=stop_after_attempt(settings.max_retries), wait=wait_exponential(multiplier=1, min=1, max=6),
       retry=retry_if_exception_type(_RETRYABLE), reraise=True)
def _call_backend(transcript: str, niche: str, user_id: str) -> dict:
    res = requests.post(
        f"{settings.api_base_url}/api/v1/worker/analyze-transcript",
        json={"transcript": transcript, "niche": niche},
        params={"user_id": user_id},
        timeout=30,
    )
    res.raise_for_status()
    return res.json()


def find_best_segment(sub_path: str, niche: str = "content", user_id: str = "demo_user_123") -> ClipSegment:
    """
    Uses the backend to find the best standalone viral-Short moment
    (25-55s). Falls back to a fixed default window if subtitles are
    missing or the backend call fails — never crashes the pipeline
    over a clip-selection hiccup.
    """
    entries = parse_vtt(sub_path)
    if not entries:
        log.warning("No subtitle entries — using default window.")
        return _fallback_segment(niche)

    transcript = build_transcript_block(entries)

    try:
        data = _call_backend(transcript, niche, user_id)
    except Exception as e:
        log.warning("Backend clip-analysis call failed, using default window: %s", e)
        return _fallback_segment(niche)

    if "error" in data:
        log.warning("Backend returned error: %s", data["error"])
        return _fallback_segment(niche)

    start = data.get("start_sec", _DEFAULT_START)
    end = data.get("end_sec", start + 50)
    caption = data.get("caption", niche.title())

    valid_starts = [int(e["start"]) for e in entries]
    valid_ends = [int(e["end"]) for e in entries]
    snapped_start = min(valid_starts, key=lambda x: abs(x - start)) if valid_starts else start
    snapped_end = min(valid_ends, key=lambda x: abs(x - end)) if valid_ends else end

    duration = snapped_end - snapped_start
    if duration > _MAX_CLIP_SEC:
        snapped_end = snapped_start + _MAX_CLIP_SEC
    elif duration < _MIN_CLIP_SEC:
        snapped_end = snapped_start + 45

    log.info("Selected segment %ss-%ss (%ss): %r", snapped_start, snapped_end, snapped_end - snapped_start, caption)
    return ClipSegment(start_sec=snapped_start, end_sec=snapped_end, caption=caption)
