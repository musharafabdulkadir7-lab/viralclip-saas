import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("WORKER_SECRET", "test-secret")

from app.services.clip_analysis import _heuristic_fallback, parse_gemini_response


def test_heuristic_fallback_picks_densest_window():
    transcript = "\n".join([
        "[00:00] a short line here",
        "[00:10] this window has way more words packed into it than the others around it",
        "[01:00] another short one",
    ])
    result = _heuristic_fallback(transcript, "finance")
    assert result["start_sec"] == 10
    assert result["caption"] == "Finance"


def test_parse_gemini_response_happy_path():
    text = "START: 1:20\nEND: 2:10\nCAPTION: How I Built My First Million\nVIRAL_SCORE: 96\nREASON: strong hook"
    result = parse_gemini_response(text, "finance")
    assert result["start_sec"] == 80
    assert result["end_sec"] == 130
    assert result["caption"] == "How I Built My First Million"
    assert result["viral_score"] == 96


def test_parse_gemini_response_missing_fields_raises():
    import pytest
    with pytest.raises(ValueError):
        parse_gemini_response("garbage output", "finance")