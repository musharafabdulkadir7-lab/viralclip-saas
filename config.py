"""
config.py
Centralized configuration. Loads from environment variables (optionally
via a .env file if python-dotenv is installed) with sane defaults and
validation. Every other module should import `settings` from here
instead of reading os.environ directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    try:
        return int(val) if val else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # ── Paths ──
    home_dir: Path = field(default_factory=lambda: Path.home() / ".clipai")

    # ── API keys / secrets ──
    youtube_api_key: str = field(default_factory=lambda: os.environ.get("YOUTUBE_API_KEY", ""))
    worker_secret: str = field(default_factory=lambda: os.environ.get("WORKER_SECRET", "clipai_worker_sec_997f7c9_v2"))
    admin_secret: str = field(default_factory=lambda: os.environ.get("ADMIN_SECRET", "clipai_admin_default_sec"))
    api_base_url: str = field(default_factory=lambda: os.environ.get("API_BASE_URL", "https://viralclip-saas.onrender.com"))

    # ── Google OAuth ──
    google_client_id: str = field(default_factory=lambda: os.environ.get("GOOGLE_CLIENT_ID", ""))
    google_client_secret: str = field(default_factory=lambda: os.environ.get("GOOGLE_CLIENT_SECRET", ""))
    google_redirect_uri: str = field(default_factory=lambda: os.environ.get("GOOGLE_REDIRECT_URI", "https://viralclip-saas.onrender.com/api/v1/auth/youtube/callback"))

    # ── Stripe & Supabase ──
    stripe_secret_key: str = field(default_factory=lambda: os.environ.get("STRIPE_SECRET_KEY", ""))
    stripe_webhook_secret: str = field(default_factory=lambda: os.environ.get("STRIPE_WEBHOOK_SECRET", ""))
    supabase_url: str = field(default_factory=lambda: os.environ.get("SUPABASE_URL", ""))
    supabase_key: str = field(default_factory=lambda: os.environ.get("SUPABASE_KEY", ""))

    # ── Redis ──
    redis_url: str = field(default_factory=lambda: os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    redis_url_2: str = field(default_factory=lambda: os.environ.get("REDIS_URL_2", ""))

    # ── User Quotas ──
    free_tier_limit: int = field(default_factory=lambda: _env_int("FREE_TIER_LIMIT", 1))
    referral_bonus_clips: int = field(default_factory=lambda: _env_int("REFERRAL_BONUS_CLIPS", 2))

    # ── Sourcing thresholds ──
    min_views: int = field(default_factory=lambda: _env_int("CLIPAI_MIN_VIEWS", 50_000))
    min_duration_sec: int = field(default_factory=lambda: _env_int("CLIPAI_MIN_DURATION_SEC", 300))
    max_age_days: int = field(default_factory=lambda: _env_int("CLIPAI_MAX_AGE_DAYS", 730))
    top_n_candidates: int = field(default_factory=lambda: _env_int("CLIPAI_TOP_N", 3))

    # ── Rendering ──
    max_short_duration_sec: int = field(default_factory=lambda: _env_int("CLIPAI_MAX_SHORT_SEC", 56))
    default_watermark: str = field(default_factory=lambda: os.environ.get("CLIPAI_DEFAULT_WATERMARK", "@YourChannel"))
    ffmpeg_timeout_sec: int = field(default_factory=lambda: _env_int("CLIPAI_FFMPEG_TIMEOUT", 600))

    # ── Networking / retries ──
    http_timeout_sec: int = field(default_factory=lambda: _env_int("CLIPAI_HTTP_TIMEOUT", 20))
    max_retries: int = field(default_factory=lambda: _env_int("CLIPAI_MAX_RETRIES", 3))

    # ── Webhooks ──
    webhook_url: str = field(default_factory=lambda: os.environ.get("CLIPAI_WEBHOOK_URL", ""))
    webhook_secret: str = field(default_factory=lambda: os.environ.get("CLIPAI_WEBHOOK_SECRET", ""))

    # ── Logging ──
    log_level: str = field(default_factory=lambda: os.environ.get("CLIPAI_LOG_LEVEL", "INFO"))
    log_json: bool = field(default_factory=lambda: _env_bool("CLIPAI_LOG_JSON", False))

    @property
    def download_dir(self) -> Path:
        return self.home_dir / "downloaded_videos"

    @property
    def output_dir(self) -> Path:
        return self.home_dir / "generated_videos"

    @property
    def hot_pool_dir(self) -> Path:
        return self.home_dir / "hot_pool"

    @property
    def used_videos_file(self) -> Path:
        return self.home_dir / "used_videos.json"

    def ensure_dirs(self) -> None:
        for d in (self.home_dir, self.download_dir, self.output_dir, self.hot_pool_dir):
            d.mkdir(parents=True, exist_ok=True)

    def validate_for_mode(self, mode: str) -> list[str]:
        problems = []
        if mode == "licensed_cc" and not self.youtube_api_key:
            problems.append("YOUTUBE_API_KEY is required for licensed_cc mode.")
        if not self.worker_secret:
            problems.append("WORKER_SECRET is not set — credential fetch will fail.")
        return problems

    def validate_for_startup(self) -> list[str]:
        problems = []
        if not self.worker_secret:
            problems.append("WORKER_SECRET is not set.")
        if not self.supabase_url:
            problems.append("SUPABASE_URL is not set.")
        if not self.redis_url:
            problems.append("REDIS_URL is not set.")
        return problems


settings = Settings()
settings.ensure_dirs()
