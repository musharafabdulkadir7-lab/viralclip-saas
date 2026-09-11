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


@dataclass
class WorkerSettings:

    home_dir: Path = field(default_factory=lambda: Path.home() / ".clipai")
    youtube_api_key: str = field(default_factory=lambda: os.environ.get("YOUTUBE_API_KEY", ""))
    worker_secret: str = field(default_factory=lambda: os.environ.get("WORKER_SECRET", ""))
    api_base_url: str = field(default_factory=lambda: os.environ.get("API_BASE_URL", "http://localhost:8000"))

    min_views: int = field(default_factory=lambda: _env_int("CLIPAI_MIN_VIEWS", 50_000))
    min_duration_sec: int = field(default_factory=lambda: _env_int("CLIPAI_MIN_DURATION_SEC", 300))
    max_age_days: int = field(default_factory=lambda: _env_int("CLIPAI_MAX_AGE_DAYS", 730))
    top_n_candidates: int = field(default_factory=lambda: _env_int("CLIPAI_TOP_N", 5))

    max_short_duration_sec: int = field(default_factory=lambda: _env_int("CLIPAI_MAX_SHORT_SEC", 56))
    default_watermark: str = field(default_factory=lambda: os.environ.get("CLIPAI_DEFAULT_WATERMARK", "@YourChannel"))
    ffmpeg_timeout_sec: int = field(default_factory=lambda: _env_int("CLIPAI_FFMPEG_TIMEOUT", 600))

    http_timeout_sec: int = field(default_factory=lambda: _env_int("CLIPAI_HTTP_TIMEOUT", 20))
    max_retries: int = field(default_factory=lambda: _env_int("CLIPAI_MAX_RETRIES", 3))

    log_level: str = field(default_factory=lambda: os.environ.get("CLIPAI_LOG_LEVEL", "INFO"))
    log_json: bool = field(default_factory=lambda: os.environ.get("CLIPAI_LOG_JSON", "false").lower() == "true")

    max_clips_per_job: int = 5  # NEW: caps multi-clip generation per job

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

    def validate(self) -> list[str]:
        problems = []
        if not self.youtube_api_key:
            problems.append("YOUTUBE_API_KEY is required for licensed_cc mode.")
        if not self.worker_secret:
            problems.append("WORKER_SECRET is not set — credential fetch will fail.")
        return problems


settings = WorkerSettings()
settings.ensure_dirs()