import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

try:
    from pipeline.video_finder import _iso8601_to_seconds, VideoCandidate, register_uploaded_file, VideoFinderError
except ImportError:
    from video_finder import _iso8601_to_seconds, VideoCandidate, register_uploaded_file, VideoFinderError

import pytest


def test_iso8601_minutes_seconds():
    assert _iso8601_to_seconds("PT4M13S") == 4 * 60 + 13


def test_iso8601_hours_minutes_seconds():
    assert _iso8601_to_seconds("PT1H2M3S") == 3600 + 120 + 3


def test_iso8601_seconds_only():
    assert _iso8601_to_seconds("PT45S") == 45


def test_iso8601_empty_or_invalid():
    assert _iso8601_to_seconds("") == 0
    assert _iso8601_to_seconds("garbage") == 0


def test_video_candidate_to_dict_roundtrip():
    c = VideoCandidate(id="abc123", title="Test", url="https://youtu.be/abc123", view_count=1000)
    d = c.to_dict()
    assert d["id"] == "abc123"
    assert d["view_count"] == 1000


def test_register_uploaded_file_missing_raises(tmp_path):
    missing = tmp_path / "does_not_exist.mp4"
    with pytest.raises(VideoFinderError):
        register_uploaded_file(str(missing))


def test_register_uploaded_file_success(tmp_path):
    f = tmp_path / "myvideo.mp4"
    f.write_bytes(b"fake video bytes")
    candidate = register_uploaded_file(str(f), title="A" * 100)
    assert candidate.id == "myvideo"
    assert candidate.local_path == str(f)
    assert len(candidate.title) == 60  # truncated to 60 chars


def test_video_candidate_license_and_attribution():
    c = VideoCandidate(
        id="v1", title="Partner Video", url="https://youtu.be/v1",
        license="partner_licensed", attribution="Clipped with permission from Creator"
    )
    assert c.license == "partner_licensed"
    assert "with permission" in c.attribution
