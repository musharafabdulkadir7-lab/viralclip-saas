from __future__ import annotations

import re
from dataclasses import dataclass

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .config import settings
from .logging_setup import get_logger

log = get_logger("clip_finder")

_DEFAULT_START, _DEFAULT_END = 60, 110
_MIN_CLIP_SEC, _MAX_CLIP_SEC = 25, 55


@dataclass
class ClipSegment:
    start_sec: int
    end_sec: int
    caption: str


def _fallback_segment(niche: str, offset: int = 0) -> ClipSegment:
    return ClipSegment(start_sec=_DEFAULT_START + offset, end_sec=_DEFAULT_END + offset, caption=niche.title() or "Clip")


def parse_vtt(vtt_path: str) -> list[dict]:
    try:
        content = open(vtt_path, "r", encoding="utf-8").read()
    except Exception as e:
        log.error("Failed to read VTT %s: %s", vtt_path, e)
        return []

    def ts_to_sec(h, m, s, ms):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    entries: list[dict] = []
    seen: set[str] = set()
    for block in re.split(r"\n\s*\n", content.strip()):
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
        m = re.match(r"(\d+):(\d+):(\d+)[\.,](\d+)\s*-->\s*(\d+):(\d+):(\d+)[\.,](\d+)", ts_line)
        if not m:
            continue
        start, end = ts_to_sec(*m.groups()[:4]), ts_to_sec(*m.groups()[4:])
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", " ".join(text_lines))).strip()
        if not text or text in ("[Music]", "[Applause]") or text in seen:
            continue
        seen.add(text)
        entries.append({"start": start, "end": end, "text": text})
    return entries


def build_transcript_block(entries: list[dict], max_chars: int = 10_000) -> str:
    lines, total = [], 0
    for e in entries:
        s = int(e["start"])
        line = f"[{s // 60:02d}:{s % 60:02d}] {e['text']}"
        total += len(line)
        if total > max_chars:
            break
        lines.append(line)
    return "\n".join(lines)


_RETRYABLE = (requests.ConnectionError, requests.Timeout)


@retry(stop=stop_after_attempt(settings.max_retries), wait=wait_exponential(multiplier=1, min=1, max=6),
       retry=retry_if_exception_type(_RETRYABLE), reraise=True)
def _call_backend(transcript: str, niche: str, user_id: str) -> dict:
    from .security import sign_worker_token
    token = sign_worker_token(user_id, purpose="analyze")
    res = requests.post(
        f"{settings.api_base_url}/api/v1/worker/analyze-transcript",
        json={"transcript": transcript, "niche": niche}, params={"user_id": user_id, "token": token}, timeout=30,
    )
    res.raise_for_status()
    return res.json()


def _snap_and_clamp(entries: list[dict], start: int, end: int) -> tuple[int, int]:
    valid_starts = [int(e["start"]) for e in entries]
    valid_ends = [int(e["end"]) for e in entries]
    s = min(valid_starts, key=lambda x: abs(x - start)) if valid_starts else start
    e = min(valid_ends, key=lambda x: abs(x - end)) if valid_ends else end
    duration = e - s
    if duration > _MAX_CLIP_SEC:
        e = s + _MAX_CLIP_SEC
    elif duration < _MIN_CLIP_SEC:
        e = s + 45
    return s, e


def find_best_segment(sub_path: str, niche: str = "content", user_id: str = "demo_user_123") -> ClipSegment:
    segments = find_best_segments(sub_path, niche=niche, user_id=user_id, num_clips=1)
    return segments[0]


def find_best_segments(sub_path: str, niche: str = "content", user_id: str = "demo_user_123", num_clips: int = 1) -> list[ClipSegment]:
    """NEW: multi-clip support. Asks the backend once for the transcript's
    top moment, then greedily picks additional non-overlapping windows by
    local word-density so a single source video can yield several distinct
    Shorts instead of wasting a full download+analysis pass per clip."""
    entries = parse_vtt(sub_path)
    if not entries:
        return [_fallback_segment(niche, offset=i * 60) for i in range(num_clips)]

    transcript = build_transcript_block(entries)
    try:
        data = _call_backend(transcript, niche, user_id)
    except Exception as e:
        log.warning("Backend clip-analysis call failed, using default window: %s", e)
        return [_fallback_segment(niche, offset=i * 60) for i in range(num_clips)]

    if "error" in data:
        return [_fallback_segment(niche, offset=i * 60) for i in range(num_clips)]

    primary_start, primary_end = _snap_and_clamp(entries, data.get("start_sec", _DEFAULT_START), data.get("end_sec", _DEFAULT_START + 50))
    caption = data.get("caption", niche.title())
    segments = [ClipSegment(start_sec=primary_start, end_sec=primary_end, caption=caption)]

    if num_clips > 1:
        used_ranges = [(primary_start, primary_end)]
        window_starts = sorted({int(e["start"]) for e in entries})
        # score each candidate window by transcript word-density, skipping overlap with already-picked ranges
        scored = []
        for ws in window_starts:
            we = ws + 50
            if any(not (we <= u_s or ws >= u_e) for u_s, u_e in used_ranges):
                continue
            words = sum(len(e["text"].split()) for e in entries if ws <= e["start"] < we)
            scored.append((words, ws, we))
        scored.sort(reverse=True)

        idx = 1
        for words, ws, we in scored:
            if len(segments) >= num_clips:
                break
            if any(not (we <= u_s or ws >= u_e) for u_s, u_e in used_ranges):
                continue
            s, e = _snap_and_clamp(entries, ws, we)
            segments.append(ClipSegment(start_sec=s, end_sec=e, caption=f"{caption} (Part {idx + 1})"))
            used_ranges.append((s, e))
            idx += 1

        while len(segments) < num_clips:
            segments.append(_fallback_segment(niche, offset=len(segments) * 70))

    log.info("Selected %d segment(s) for %r", len(segments), niche)
    return segments