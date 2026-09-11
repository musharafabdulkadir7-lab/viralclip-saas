"""Ported from v2 unchanged (licensed_cc-only pre-bake cache)."""

from __future__ import annotations



import glob

import json

import threading

import time

from pathlib import Path

from typing import Optional



from .config import settings

from .logging_setup import get_logger



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



        from . import clip_cutter, clip_finder, video_downloader, video_finder

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

                log.warning("Pre-bake candidate failed (%s): %s", candidate.id, e)

        if not video or not dl:

            return



        clip_info = clip_finder.find_best_segment(dl.sub_path, niche=niche) if dl.sub_path else clip_finder.ClipSegment(60, 110, niche.title())

        try:

            clip_path = clip_cutter.cut_clip(video_path=dl.video_path, start_sec=clip_info.start_sec, end_sec=clip_info.end_sec,

                                              caption=clip_info.caption, watermark=f"@{niche.replace(' ', '').capitalize()}", sub_path=dl.sub_path)

        except clip_cutter.ClipCutError as e:

            log.warning("Pre-bake clip cut failed: %s", e)

            return



        manifest = {"niche": niche, "video_id": video.id, "video_title": video.title, "attribution": video.attribution,

                    "clip_info": {"start_sec": clip_info.start_sec, "end_sec": clip_info.end_sec, "caption": clip_info.caption},

                    "clip_paths": [clip_path], "created_at": time.time()}

        (niche_dir / f"hot_{video.id}_{int(time.time())}.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    except Exception as e:

        log.error("Pre-baking error for %r: %s", niche, e)

    finally:

        with _lock:

            _replenishing_niches.discard(niche_key)





def trigger_replenish(niche: str) -> None:

    threading.Thread(target=prebake_clip_worker, args=(niche,), daemon=True).start()