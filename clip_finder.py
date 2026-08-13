"""
clip_finder.py
Reads YouTube auto-generated subtitles (VTT format) and uses Gemini
to find the single most engaging 45-60 second clip window.
Returns start/end timestamps in seconds.
"""
import re
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

MODEL = "gemini-3.5-flash"
_client = None

def get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not set. Please add it in ⚙️ API Settings.")
        _client = genai.Client(api_key=api_key)
    return _client



def parse_vtt(vtt_path: str) -> list:
    """
    Parses a YouTube auto-caption VTT file into {start, end, text} dicts.
    Handles YouTube's inline timing tags and duplicate lines.
    """
    entries = []
    try:
        with open(vtt_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"[ClipFinder] Failed to read VTT: {e}")
        return []

    def ts_to_sec(h, m, s, ms):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    # Split into blocks separated by blank lines
    blocks = re.split(r"\n\s*\n", content.strip())

    seen_texts = set()
    for block in blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue

        # Find timestamp line (contains -->)
        ts_line = None
        text_lines = []
        for i, line in enumerate(lines):
            if "-->" in line:
                ts_line = line
                text_lines = lines[i + 1:]
                break

        if not ts_line:
            continue

        # Parse timestamps - handle optional trailing metadata (align:start position:0%)
        ts_match = re.match(
            r"(\d+):(\d+):(\d+)[\.,](\d+)\s*-->\s*(\d+):(\d+):(\d+)[\.,](\d+)",
            ts_line
        )
        if not ts_match:
            continue

        start = ts_to_sec(*ts_match.groups()[:4])
        end   = ts_to_sec(*ts_match.groups()[4:])

        # Combine text lines, strip inline timing tags and HTML tags
        raw_text = " ".join(text_lines)
        # Remove inline timestamp tags: <00:00:07.839>
        raw_text = re.sub(r"<\d+:\d+:\d+[\.,]\d+>", "", raw_text)
        # Remove <c>, </c> and similar tags
        raw_text = re.sub(r"<[^>]+>", "", raw_text)
        # Collapse whitespace
        raw_text = re.sub(r"\s+", " ", raw_text).strip()

        # Skip empty, whitespace-only, or music annotations
        if not raw_text or raw_text in (" ", "[Music]", "[Applause]"):
            continue

        # Skip if this exact text was already seen (YouTube duplicates adjacent blocks)
        if raw_text in seen_texts:
            continue
        seen_texts.add(raw_text)

        entries.append({"start": start, "end": end, "text": raw_text})

    print(f"[ClipFinder] Parsed {len(entries)} subtitle entries.")
    return entries


def build_transcript_block(entries: list, max_chars: int = 8000) -> str:
    """
    Converts VTT entries into a readable transcript with timestamps.
    Truncates if too long for Gemini context.
    """
    lines = []
    total = 0
    for e in entries:
        secs_total = int(e["start"])
        mins = secs_total // 60
        secs = secs_total % 60
        line = f"[{mins:02d}:{secs:02d}] {e['text']}"
        total += len(line)
        if total > max_chars:
            break
        lines.append(line)
    return "\n".join(lines)


def find_best_segment(sub_path: str, niche: str = "motivation") -> dict:
    """
    Uses Gemini to find a complete 2-4 minute segment (story/point/idea)
    that can be split into Part 1, Part 2, Part 3 Shorts.
    Returns {start_sec, end_sec, caption, num_parts}.
    """
    print("[ClipFinder] Analyzing transcript for best multi-part segment...")

    entries = parse_vtt(sub_path)
    if not entries:
        print("[ClipFinder] No subtitle entries, using fallback segment.")
        return {"start_sec": 60, "end_sec": 240, "caption": niche.title(), "num_parts": 3}

    transcript = build_transcript_block(entries, max_chars=10000)

    prompt = f"""You are an expert YouTube Shorts creator in the '{niche}' niche.

Below is a timestamped transcript from a long-form YouTube video.
Your job is to find the SINGLE most compelling complete segment — a story, life lesson, or argument that has:
- A clear beginning (hook/setup)
- A middle (buildup/details)  
- A natural ending (conclusion/punchline/resolution)

The segment should be 2 to 4 minutes long (120 to 240 seconds) so it can be split into 2-4 Parts of ~55 seconds each.

Rules:
- Pick where someone is telling a complete story or making a full point — NOT just a random 3-minute window
- The start should be a natural hook (a question, a surprising claim, or a story setup)
- The end should be a natural resolution (not mid-sentence)
- Total length must be 120-240 seconds

Transcript:
{transcript}

You MUST respond in EXACTLY this format, nothing else, no markdown, no bullet points:
START: 180
END: 360
CAPTION: How I Built My First Million
REASON: This segment tells a complete rags-to-riches story with a clear arc."""

    try:
        response = get_client().models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=256)
        )
        text = response.text.strip()

        def parse_ts(val):
            val = val.strip()
            if ":" in val:
                parts = val.split(":")
                if len(parts) == 2:
                    return int(parts[0]) * 60 + int(parts[1])
                elif len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            return int(val)

        start_m = re.search(r"START:\s*([\d:]+)", text)
        end_m   = re.search(r"END:\s*([\d:]+)", text)
        caption_m = re.search(r"CAPTION:\s*(.+)", text)

        if not start_m or not end_m:
            print(f"[ClipFinder] Segment parse failed, using fallback. Response: {text[:80]}")
            return {"start_sec": 60, "end_sec": 240, "caption": niche.title(), "num_parts": 3}

        start = parse_ts(start_m.group(1))
        end   = parse_ts(end_m.group(1))
        duration = end - start

        # Clamp to 120-240 seconds
        if duration < 100:
            end = start + 165
        if duration > 250:
            end = start + 220

        duration = end - start
        num_parts = max(2, min(4, round(duration / 55)))
        caption = caption_m.group(1).strip() if caption_m else niche.title()

        print(f"[ClipFinder] Segment: {start}s-{end}s ({duration}s) -> {num_parts} parts | '{caption}'")
        return {"start_sec": start, "end_sec": end, "caption": caption, "num_parts": num_parts}

    except Exception as e:
        print(f"[ClipFinder] Gemini segment error: {e}")
        return {"start_sec": 60, "end_sec": 240, "caption": niche.title(), "num_parts": 3}


def find_best_clip(sub_path: str, niche: str = "motivation") -> dict:
    """
    Uses Gemini to analyze the transcript and return the best 45-60s clip.
    Returns {start_sec, end_sec, reason}.
    """
    print(f"[ClipFinder] Analyzing transcript for best clip...")

    entries = parse_vtt(sub_path)
    if not entries:
        print("[ClipFinder] No subtitle entries parsed.")
        return {}

    transcript = build_transcript_block(entries)

    prompt = f"""You are an expert viral YouTube Shorts editor specializing in the '{niche}' niche.

Below is a timestamped transcript from a long-form YouTube video.
Your job is to find the SINGLE most emotionally powerful, hook-worthy, and self-contained 45 to 60 second window.

Rules:
- The clip MUST be between 45 and 60 seconds long (end_sec - start_sec must be 45-60)
- Pick a moment with a strong emotional peak, inspiring quote, surprising fact, or powerful story moment
- The clip should make sense on its own without needing context from the rest of the video
- Prefer moments with high energy, clear narration, or a memorable line

Transcript:
{transcript}

You MUST respond in EXACTLY this format, nothing else, no markdown, no bullet points:
START: 243
END: 298
REASON: This moment contains the most emotionally resonant quote of the speech.
CAPTION: Nothing In Life Is Worthwhile Without Risk"""

    try:
        response = get_client().models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=512)
        )
        text = response.text.strip()

        def parse_timestamp(val: str) -> int:
            """Parse timestamp as raw seconds (243) or MM:SS (02:21) or HH:MM:SS."""
            val = val.strip()
            if ":" in val:
                parts = val.split(":")
                if len(parts) == 2:
                    return int(parts[0]) * 60 + int(parts[1])
                elif len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            return int(val)

        # Match START/END in both raw seconds and MM:SS/HH:MM:SS formats
        start_match = re.search(r"START:\s*([\d:]+)", text)
        end_match   = re.search(r"END:\s*([\d:]+)", text)
        reason_match  = re.search(r"REASON:\s*(.+)", text)
        caption_match = re.search(r"CAPTION:\s*(.+)", text)

        if not start_match or not end_match:
            print(f"[ClipFinder] Could not parse response, using fallback. Response was: {text[:80]}")
            return {"start_sec": 60, "end_sec": 116, "reason": "Fallback", "caption": niche.upper()}

        try:
            start = parse_timestamp(start_match.group(1))
            end   = parse_timestamp(end_match.group(1))
        except ValueError:
            print(f"[ClipFinder] Timestamp parse error, using fallback.")
            return {"start_sec": 60, "end_sec": 116, "reason": "Fallback", "caption": niche.upper()}

        # Enforce Shorts duration limits
        if end - start < 40:
            end = start + 52
        if end - start > 58:
            end = start + 55

        caption = (caption_match.group(1).strip() if caption_match else niche.upper())
        result = {
            "start_sec": start,
            "end_sec": end,
            "reason": reason_match.group(1).strip() if reason_match else "",
            "caption": caption,
        }
        print(f"[ClipFinder] Best clip: {start}s - {end}s | '{caption}'")
        return result

    except Exception as e:
        print(f"[ClipFinder] Gemini error: {e}")
        return {"start_sec": 60, "end_sec": 115, "reason": "Fallback", "caption": niche.upper()}
