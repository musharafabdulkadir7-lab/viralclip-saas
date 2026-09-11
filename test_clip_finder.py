import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

try:
    from pipeline.clip_finder import parse_vtt, build_transcript_block, _fallback_segment, ClipSegment
except ImportError:
    from clip_finder import parse_vtt, build_transcript_block, _fallback_segment, ClipSegment


SAMPLE_VTT = """WEBVTT

00:00:01.000 --> 00:00:03.000
Hello and welcome to the show

00:00:03.500 --> 00:00:05.000
Today we're talking about testing

00:00:05.500 --> 00:00:06.000
[Music]

00:00:06.500 --> 00:00:08.000
Today we're talking about testing
"""


def test_parse_vtt_basic(tmp_path):
    f = tmp_path / "sample.vtt"
    f.write_text(SAMPLE_VTT, encoding="utf-8")
    entries = parse_vtt(str(f))
    assert len(entries) == 2  # music + duplicate line both dropped
    assert entries[0]["text"] == "Hello and welcome to the show"
    assert entries[0]["start"] == 1.0
    assert entries[1]["text"] == "Today we're talking about testing"


def test_parse_vtt_missing_file_returns_empty():
    assert parse_vtt("/nonexistent/path.vtt") == []


def test_build_transcript_block_respects_max_chars():
    entries = [{"start": i, "text": "word " * 20} for i in range(0, 100, 10)]
    block = build_transcript_block(entries, max_chars=50)
    assert len(block) < 300  # should truncate well before including all entries


def test_fallback_segment_shape():
    seg = _fallback_segment("cooking tips")
    assert isinstance(seg, ClipSegment)
    assert seg.end_sec > seg.start_sec
    assert seg.caption  # non-empty
