import os
import sys
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("WORKER_SECRET", "test-secret")

from pipeline.clip_finder import parse_vtt, build_transcript_block  # noqa: E402

SAMPLE_VTT = textwrap.dedent("""\
    WEBVTT

    00:00:01.000 --> 00:00:04.000
    Hey everyone welcome back to the channel

    00:00:04.000 --> 00:00:07.000
    [Music]

    00:00:07.000 --> 00:00:10.000
    Today we're talking about something crazy
    """)


def test_parse_vtt_skips_music_markers(tmp_path):
    p = tmp_path / "subs.vtt"
    p.write_text(SAMPLE_VTT, encoding="utf-8")
    entries = parse_vtt(str(p))
    texts = [e["text"] for e in entries]
    assert "[Music]" not in texts
    assert len(entries) == 2


def test_parse_vtt_dedupes_repeated_lines(tmp_path):
    vtt = SAMPLE_VTT + "\n00:00:10.000 --> 00:00:13.000\nHey everyone welcome back to the channel\n"
    p = tmp_path / "subs.vtt"
    p.write_text(vtt, encoding="utf-8")
    entries = parse_vtt(str(p))
    texts = [e["text"] for e in entries]
    assert texts.count("Hey everyone welcome back to the channel") == 1


def test_build_transcript_block_respects_char_limit(tmp_path):
    p = tmp_path / "subs.vtt"
    p.write_text(SAMPLE_VTT, encoding="utf-8")
    entries = parse_vtt(str(p))
    block = build_transcript_block(entries, max_chars=20)
    assert len(block) < 100  # truncated well below the full transcript