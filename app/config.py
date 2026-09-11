"""
app/config.py
Typed, validated settings. Replaces the old dataclass-with-os.environ.get
pattern. Two real upgrades over v2:

1. Secrets have NO hardcoded fallback values. `WORKER_SECRET` used to
   default to a string literal baked into the repo (`clipai_worker_sec_997f7c9_v2`).
   That means anyone who read the source (or found it on GitHub) could
   forge worker tokens for ANY deployment that didn't override it. Now
   every secret is required at startup in "production" mode and the app
   refuses to boot without it — same idea as Django's SECRET_KEY check.
2. `Settings` is a pydantic BaseSettings, so env parsing/validation errors
   surface as one readable startup error instead of scattered runtime
   KeyErrors deep in a request handler.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: Literal["dev", "staging", "production"] = "dev"

    # ── Paths ──
    home_dir: Path = Path.home() / ".clipai"

    # ── Secrets (NO fallback defaults — see module docstring) ──
    youtube_api_key: str = ""
    worker_secret: str = Field(default="", min_length=0)
    admin_secret: str = ""
    api_base_url: str = "http://localhost:8000"

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/v1/auth/youtube/callback"

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    supabase_url: str = ""
    supabase_key: str = ""
    gemini_api_key: str = ""

    redis_url: str = "redis://localhost:6379/0"
    redis_url_2: str = ""
    redis_url_3: str = ""

    jwt_signing_key: str = ""  # replaces the old HMAC-with-worker-secret user-token scheme

    # ── Quotas / business ──
    free_tier_limit: int = 1
    referral_bonus_clips: int = 2

    # ── Sourcing thresholds ──
    min_views: int = 50_000
    min_duration_sec: int = 300
    max_age_days: int = 730
    top_n_candidates: int = 3

    # ── Rendering ──
    max_short_duration_sec: int = 56
    default_watermark: str = "@YourChannel"
    ffmpeg_timeout_sec: int = 600

    # ── Networking / retries ──
    http_timeout_sec: int = 20
    max_retries: int = 3

    # ── Webhooks ──
    webhook_url: str = ""
    webhook_secret: str = ""

    # ── Logging / observability ──
    log_level: str = "INFO"
    log_json: bool = False
    sentry_dsn: str = ""

    # ── Rate limits (requests per window, seconds) ──
    rl_generate_clip: tuple[int, int] = (5, 60)
    rl_default: tuple[int, int] = (60, 60)

    @field_validator("home_dir", mode="before")
    @classmethod
    def _expand(cls, v):
        return Path(v).expanduser()

    @model_validator(mode="after")
    def _require_secrets_in_prod(self) -> "Settings":
        if self.env == "production":
            missing = [
                name
                for name, val in (
                    ("WORKER_SECRET", self.worker_secret),
                    ("JWT_SIGNING_KEY", self.jwt_signing_key),
                    ("ADMIN_SECRET", self.admin_secret),
                    ("SUPABASE_URL", self.supabase_url),
                    ("SUPABASE_KEY", self.supabase_key),
                    ("REDIS_URL", self.redis_url),
                )
                if not val
            ]
            if missing:
                raise ValueError(
                    "Refusing to start in production without: " + ", ".join(missing) +
                    ". Set them as environment variables — there are no baked-in fallbacks."
                )
        return self

    @property
    def download_dir(self) -> Path:
        return self.home_dir / "downloaded_videos"

    @property
    def output_dir(self) -> Path:
        return self.home_dir / "generated_videos"

    @property
    def hot_pool_dir(self) -> Path:
        return self.home_dir / "hot_pool"

    def ensure_dirs(self) -> None:
        for d in (self.home_dir, self.download_dir, self.output_dir, self.hot_pool_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s