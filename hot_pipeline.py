"""
hot_pipeline.py
Speculative pre-bake cache for licensed_cc mode only. own_content is
tied to a specific user's video and isn't something you can usefully
pre-render ahead of a request.
"""
from __future__ import annotations

import glob
import json
import threading
import time
from pathlib import Path
from typing import Optional

from config import settings
from logging_setup import get_logger

log = get_logger("hot_pipeline")

_replenishing_niches: set[str] = set()
_lock = threading.Lock()


def _niche_key(niche: str) -> str:
    return "".join(c for c in niche.lower() if c.isalnum() or c in (" ", "_")).strip().replace(" ", "_")


def get_hot_clip(niche: str) -> Optional[dict]:
    niche_dir = settings.hot_pool_dir / _niche_key(niche)
    if not niche_dir.exists():
        return None

    for manifest_path in glob.glob(str(niche_dir / "*.json")):
        try:
            data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            clips = data.get("clip_paths", [])
            if clips and all(Path(c).exists() and Path(c).stat().st_size > 102_400 for c in clips):
                Path(manifest_path).unlink()
                log.info("Hot-cache hit for %r", niche)
                return data
        except Exception as e:
            log.warning("Manifest check failed for %s: %s", manifest_path, e)
    return None


def prebake_clip_worker(niche: str) -> None:
    niche_key = _niche_key(niche)
    with _lock:
        if niche_key in _replenishing_niches:
            return
        _replenishing_niches.add(niche_key)

    try:
        niche_dir = settings.hot_pool_dir / niche_key
        niche_dir.mkdir(parents=True, exist_ok=True)

        if len(glob.glob(str(niche_dir / "*.json"))) >= 2:
            return

        log.info("Pre-baking next clip for %r...", niche)

        import clip_cutter
        import clip_finder
        import video_downloader
        import video_finder

        try:
            candidates = video_finder.find_licensed_cc_videos(niche=niche)
        except video_finder.VideoFinderError as e:
            log.warning("Pre-bake: no candidates for %r: %s", niche, e)
            return

        video, dl = None, None
        for candidate in candidates:
            try:
                dl = video_downloader.download_video_and_subs(candidate.url, candidate.id)
                video = candidate
                break
            except video_downloader.DownloadError as e:
                log.warning("Pre-bake candidate failed (%s), trying next: %s", candidate.id, e)
        if not video or not dl:
            return

        clip_info = clip_finder.find_best_segment(dl.sub_path, niche=niche) if dl.sub_path else \
            clip_finder.ClipSegment(start_sec=60, end_sec=110, caption=niche.title())

        try:
            clip_path = clip_cutter.cut_clip(
                video_path=dl.video_path, start_sec=clip_info.start_sec, end_sec=clip_info.end_sec,
                caption=clip_info.caption, watermark=f"@{niche.replace(' ', '').capitalize()}",
                sub_path=dl.sub_path,
            )
        except clip_cutter.ClipCutError as e:
            log.warning("Pre-bake clip cut failed: %s", e)
            return

        manifest_data = {
            "niche": niche,
            "video_id": video.id,
            "video_title": video.title,
            "attribution": video.attribution,
            "clip_info": {"start_sec": clip_info.start_sec, "end_sec": clip_info.end_sec, "caption": clip_info.caption},
            "clip_paths": [clip_path],
            "created_at": time.time(),
        }
        manifest_file = niche_dir / f"hot_{video.id}_{int(time.time())}.json"
        manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
        log.info("Pre-baked clip ready in cache for %r.", niche)

    except Exception as e:
        log.error("Pre-baking error for %r: %s", niche, e)
    finally:
        with _lock:
            _replenishing_niches.discard(niche_key)


def clear_stale_cache(keep_niche: Optional[str] = None) -> None:
    import shutil
    try:
        keep_key = _niche_key(keep_niche) if keep_niche else None
        if settings.hot_pool_dir.exists():
            for entry in settings.hot_pool_dir.iterdir():
                if keep_key and entry.name == keep_key:
                    continue
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
        if settings.download_dir.exists():
            for f in settings.download_dir.glob("*.*"):
                try:
                    if f.is_file():
                        f.unlink()
                except Exception:
                    pass
    except Exception as e:
        log.warning("Storage clean warning: %s", e)


def trigger_replenish(niche: str) -> None:
    t = threading.Thread(target=prebake_clip_worker, args=(niche,), daemon=True)
    t.start()
