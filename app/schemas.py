from __future__ import annotations

import re
from typing import List, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


class ClipRequest(BaseModel):
    source_mode: Literal["my_upload", "my_channel", "partner_channel", "public_domain"] = "public_domain"
    source_video_id: Optional[str] = None      # required for my_channel / partner_channel
    partner_channel_id: Optional[str] = None   # required for partner_channel
    niche: str = Field("", description="Optional topic hint, used for partner/public_domain search")
    num_clips: int = Field(1, ge=1, le=5)
    layout: Literal["cinematic_blur", "split_screen"] = "cinematic_blur"
    subtitle_style: Literal["bold_captions", "clean_minimal"] = "bold_captions"
    auto_upload: bool = False
    rights_confirmed: bool = False  # NEW

    @field_validator("niche")
    @classmethod
    def validate_niche(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def validate_source_and_niche(self) -> "ClipRequest":
        if self.source_mode == "public_domain" and not self.niche:
            raise ValueError("Niche cannot be blank for public_domain search")
        if self.source_mode in ("my_channel", "partner_channel") and not self.source_video_id:
            raise ValueError(f"source_video_id is required for source_mode={self.source_mode!r}")
        if self.source_mode == "public_domain" and not self.rights_confirmed:
            raise ValueError(
                "rights_confirmed must be true for public_domain sourcing — the user must "
                "explicitly acknowledge that clips will carry attribution to the source creator."
            )
        return self



class PublishDraftRequest(BaseModel):
    clip_id: str
    title: Optional[str] = None


class AutoPostSettings(BaseModel):
    enabled: bool = False
    times: List[str] = Field(default_factory=lambda: ["12:00"])
    niche: str = "motivation"
    days: List[str] = Field(default_factory=lambda: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])

    @field_validator("times")
    @classmethod
    def validate_times(cls, times: List[str]) -> List[str]:
        time_pattern = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
        for t in times:
            if not time_pattern.match(t):
                raise ValueError(f"Invalid time format: {t}. Expected HH:MM in 24h format")
        return times


class UserProfileOut(BaseModel):
    user_id: str
    email: str = ""
    license: str = "free_tier"
    free_clips_used: int = 0
    referral_link: str = ""


class CheckoutRequest(BaseModel):
    tier: Literal["pro", "full_version"]


class JobCompletePayload(BaseModel):
    job_id: str
    status: str
    message: str = ""
    url: Optional[str] = None
    title: Optional[str] = None
    niche: Optional[str] = None
    attribution: Optional[str] = None  # NEW — audit trail for sourced content
    license: Optional[str] = None      # NEW — e.g. "creativeCommon", "owned", "partner_licensed"
    source_url: Optional[str] = None   # NEW — original video URL, for the audit trail



class ProgressPayload(BaseModel):
    job_id: str
    status: str
    progress: int = 0
    message: str = ""
    url: Optional[str] = None


class AnalyzeRequest(BaseModel):
    transcript: str
    niche: str
