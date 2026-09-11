import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from pydantic import ValidationError

from app.schemas import AutoPostSettings, ClipRequest


def test_clip_request_rejects_blank_niche():
    with pytest.raises(ValidationError):
        ClipRequest(niche="   ", rights_confirmed=True)


def test_clip_request_strips_niche():
    req = ClipRequest(niche="  finance  ", rights_confirmed=True)
    assert req.niche == "finance"


def test_clip_request_num_clips_bounds():
    with pytest.raises(ValidationError):
        ClipRequest(niche="x", num_clips=0, rights_confirmed=True)
    with pytest.raises(ValidationError):
        ClipRequest(niche="x", num_clips=6, rights_confirmed=True)
    assert ClipRequest(niche="x", num_clips=3, rights_confirmed=True).num_clips == 3


def test_clip_request_rights_confirmed_required_for_public_domain():
    with pytest.raises(ValidationError):
        ClipRequest(niche="crypto", source_mode="public_domain", rights_confirmed=False)
    req = ClipRequest(niche="crypto", source_mode="public_domain", rights_confirmed=True)
    assert req.rights_confirmed is True



def test_clip_request_requires_source_video_id_for_channel_modes():
    with pytest.raises(ValidationError):
        ClipRequest(source_mode="my_channel")
    with pytest.raises(ValidationError):
        ClipRequest(source_mode="partner_channel")
    req = ClipRequest(source_mode="my_channel", source_video_id="vid_123")
    assert req.source_video_id == "vid_123"
    req_partner = ClipRequest(source_mode="partner_channel", source_video_id="chan_abc", partner_channel_id="chan_abc")
    assert req_partner.partner_channel_id == "chan_abc"


def test_autopost_rejects_bad_time_format():
    with pytest.raises(ValidationError):
        AutoPostSettings(enabled=True, times=["25:99"], niche="x")


def test_autopost_accepts_valid_times():
    settings = AutoPostSettings(enabled=True, times=["09:30", "23:00"], niche="x")
    assert settings.times == ["09:30", "23:00"]