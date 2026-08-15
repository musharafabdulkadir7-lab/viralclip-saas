"""
clip_finder.py
Reads YouTube auto-generated subtitles (VTT format) and uses the Render backend
to find the single most engaging 45-60 second clip window.
Returns start/end timestamps in seconds.
"""
import re
import os



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
    Uses the Render backend to find a complete 2-4 minute segment (story/point/idea)
    that can be split into Part 1, Part 2, Part 3 Shorts.
    Returns {start_sec, end_sec, caption, num_parts}.
    """
    print("[ClipFinder] Analyzing transcript for best multi-part segment...")

    entries = parse_vtt(sub_path)
    if not entries:
        print("[ClipFinder] No subtitle entries, using fallback segment.")
        return {"start_sec": 60, "end_sec": 240, "caption": niche.title(), "num_parts": 3}

    transcript = build_transcript_block(entries, max_chars=10000)

    try:
        import requests
        api_base_url = os.environ.get("API_BASE_URL", "https://viralclip-saas.onrender.com")
        user_id = os.environ.get("CLIPAI_USER_ID", "demo_user_123")
        
        print("[ClipFinder] Sending transcript to Render backend for AI analysis...")
        res = requests.post(f"{api_base_url}/api/v1/worker/analyze-transcript", 
                            json={"transcript": transcript, "niche": niche},
                            params={"user_id": user_id},
                            timeout=30)
                            
        if res.status_code == 200:
            data = res.json()
            if "error" in data:
                print(f"[ClipFinder] Backend AI error: {data['error']}")
                return {"start_sec": 60, "end_sec": 240, "caption": niche.title(), "num_parts": 3}
                
            start = data.get("start_sec", 60)
            end = data.get("end_sec", 240)
            caption = data.get("caption", niche.title())
            
            duration = end - start
            if duration < 100:
                end = start + 165
            if duration > 250:
                end = start + 220
                
            duration = end - start
            num_parts = max(2, min(4, round(duration / 55)))
            
            print(f"[ClipFinder] Segment: {start}s-{end}s ({duration}s) -> {num_parts} parts | '{caption}'")
            return {"start_sec": start, "end_sec": end, "caption": caption, "num_parts": num_parts}
        else:
            print(f"[ClipFinder] Backend returned {res.status_code}")
            return {"start_sec": 60, "end_sec": 240, "caption": niche.title(), "num_parts": 3}
            
    except Exception as e:
        print(f"[ClipFinder] Request to backend failed: {e}")
        return {"start_sec": 60, "end_sec": 240, "caption": niche.title(), "num_parts": 3}



