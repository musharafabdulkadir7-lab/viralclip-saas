from __future__ import annotations

import re
from typing import List, Literal, Optional
from pydantic import BaseModel, Field, field_validator


class ClipRequest(BaseModel):
    niche: str = Field(..., min_length=1, description="Niche or topic to clip")
    num_clips: int = Field(1, ge=1, le=5, description="Number of distinct clips (1-5)")
    layout: Literal["cinematic_blur", "split_screen"] = "cinematic_blur"
    subtitle_style: Literal["bold_captions", "clean_minimal"] = "bold_captions"
    auto_upload: bool = True

    @field_validator("niche")
    @classmethod
    def validate_niche(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Niche cannot be blank")
        return s


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


class ProgressPayload(BaseModel):
    job_id: str
    status: str
    progress: int = 0
    message: str = ""
    url: Optional[str] = None


class AnalyzeRequest(BaseModel):
    transcript: str
    niche: str
