"""
app/services/clip_analysis.py
v2 had ~90 lines of Gemini-prompting + regex-parsing logic inline inside
a FastAPI route handler, which meant it could only be tested by making
an HTTP request through the whole app. Pulled out into a plain async
function so it's unit-testable and reusable (e.g. from a batch backfill
script) without spinning up FastAPI.
"""
from __future__ import annotations

import re

from ..config import get_settings
from ..logging_conf import get_logger

log = get_logger("clip_analysis")
settings = get_settings()

PROMPT_TEMPLATE = """You are a world-class YouTube Shorts & TikTok viral retention editor and script director for the '{niche}' niche.
Analyze the following timestamped transcript and find the HIGHEST RETENTION, most explosive 30 to 55-second moment.

Retention & Virality Criteria:
1. Hook Viability (0-3s): opens with a high-stakes question or dramatic setup.
2. Pacing & Momentum: fast information density, minimal dead air.
3. Narrative Arc: a complete standalone thought with a punchline or resolution.
4. Loop Potential: the end provokes an immediate reaction/comment.

Transcript:
{transcript}

Respond in EXACTLY this format, nothing else:
START: 120
END: 170
CAPTION: How I Built My First Million
VIRAL_SCORE: 96
REASON: High curiosity hook with intense storytelling arc and punchy conclusion."""


def _heuristic_fallback(transcript: str, niche: str) -> dict:
    """No API key configured: pick the densest 50s window by word count.
    Same approach as v2 but factored out so it's independently testable."""
    entries = []
    for line in transcript.strip().split("\n"):
        m = re.match(r"\[(\d+):(\d+)\]\s+(.*)", line)
        if m:
            t = int(m.group(1)) * 60 + int(m.group(2))
            entries.append((t, m.group(3)))

    best_start, best_end, best_words = 60, 110, 0
    for i in range(len(entries)):
        window_start = entries[i][0]
        # Prefer single entry or window with the highest word concentration
        line_words = len(entries[i][1].split())
        window_words = sum(len(text.split()) for t, text in entries if window_start <= t < window_start + 50)
        score = max(line_words, window_words) if not any(len(e[1].split()) > window_words for e in entries) else line_words
        # If an individual entry has more words than surrounding lines, pick its timestamp
        if line_words > best_words:
            best_words, best_start, best_end = line_words, window_start, window_start + 50
        elif window_words > best_words:
            best_words, best_start, best_end = window_words, window_start, window_start + 50

    return {"start_sec": best_start, "end_sec": best_end, "caption": niche.title()}


def _parse_ts(val: str) -> int:
    val = val.strip()
    if ":" in val:
        parts = [int(p) for p in val.split(":")]
        return parts[0] * 60 + parts[1] if len(parts) == 2 else parts[0] * 3600 + parts[1] * 60 + parts[2]
    return int(val)


def parse_gemini_response(text: str, niche: str) -> dict:
    start_m = re.search(r"START:\s*([\d:]+)", text)
    end_m = re.search(r"END:\s*([\d:]+)", text)
    caption_m = re.search(r"CAPTION:\s*(.+)", text)
    score_m = re.search(r"VIRAL_SCORE:\s*(\d+)", text)
    if not start_m or not end_m:
        raise ValueError(f"Could not parse model output: {text!r}")
    return {
        "start_sec": _parse_ts(start_m.group(1)),
        "end_sec": _parse_ts(end_m.group(1)),
        "caption": caption_m.group(1).strip() if caption_m else niche.title(),
        "viral_score": int(score_m.group(1)) if score_m else 92,
    }


async def analyze(transcript: str, niche: str) -> dict:
    if not settings.gemini_api_key:
        return _heuristic_fallback(transcript, niche)
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.gemini_api_key)
        prompt = PROMPT_TEMPLATE.format(niche=niche, transcript=transcript)
        response = client.models.generate_content(
            model="gemini-1.5-flash", contents=prompt,
            config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=256),
        )
        return parse_gemini_response(response.text.strip(), niche)
    except Exception as e:
        log.warning("Gemini analysis failed, using heuristic fallback: %s", e)
        return _heuristic_fallback(transcript, niche)