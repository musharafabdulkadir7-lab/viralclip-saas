from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..config import get_settings
from ..db import ClipRepo, UserRepo
from ..schemas import ClipRequest, PublishDraftRequest
from ..security import rate_limit, require_user
from ..services import job_queue

router = APIRouter(prefix="/api/v1", tags=["jobs"])
settings = get_settings()


@router.post("/generate-clip")
async def generate_clip(payload: ClipRequest, user_id: str = Depends(require_user)):
    await rate_limit(f"generate:{user_id}", *settings.rl_generate_clip)

    user = UserRepo.get_or_create(user_id)
    used = user.get("free_clips_used", 0)
    if user.get("license") == "free_tier" and used >= settings.free_tier_limit:
        raise HTTPException(status_code=402, detail=f"Free tier limit reached ({settings.free_tier_limit}). Upgrade required.")

    if payload.source_mode in ("my_channel",):
        yt_user = UserRepo.get_or_create(user_id)
        if not yt_user.get("youtube_refresh_token"):
            raise HTTPException(status_code=400, detail="Connect your YouTube channel first.")

    if payload.source_mode == "partner_channel":
        from ..db import PartnerChannelRepo
        partner = PartnerChannelRepo.get_by_channel_id(payload.partner_channel_id or "")
        if not partner:
            raise HTTPException(status_code=403, detail="That channel hasn't opted into the clipping program.")

    if user.get("license") == "free_tier":
        UserRepo.increment_free_used(user_id, used)

    job_id = await job_queue.enqueue({
        "mode": payload.source_mode,
        "source_kind": "channel" if payload.source_mode == "my_channel" else ("file" if payload.source_mode == "my_upload" else None),
        "source": payload.source_video_id,
        "partner_channel_id": payload.partner_channel_id,
        "niche": payload.niche,
        "user_id": user_id,
        "is_free_tier": user.get("license") == "free_tier",
        "auto_upload": payload.auto_upload,
        "layout": payload.layout,
        "subtitle_style": payload.subtitle_style,
        "num_clips": payload.num_clips,
    })
    remaining = max(0, settings.free_tier_limit - (used + 1)) if user.get("license") == "free_tier" else None
    return {"status": "success", "job_id": job_id, "free_remaining": remaining}


@router.get("/my-channel/videos")
async def list_my_channel_videos(user_id: str = Depends(require_user)):
    """Powers the 'pick a video to clip' picker in the my_channel flow."""
    user = UserRepo.get_or_create(user_id)
    if not user.get("youtube_refresh_token"):
        raise HTTPException(status_code=400, detail="YouTube not connected.")
    creds = {
        "token": user.get("youtube_access_token"), "refresh_token": user.get("youtube_refresh_token"),
        "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
    }
    from pipeline import video_finder
    videos = video_finder.get_own_channel_videos(creds)
    return {"videos": [v.to_dict() for v in videos]}


@router.get("/partner-channels")
async def list_partner_channels():
    from ..db import PartnerChannelRepo
    return {"channels": PartnerChannelRepo.list_active()}


@router.get("/job-status/{job_id}")
async def get_job_status(job_id: str, user_id: str = Depends(require_user)):
    # NOTE: job_id is a random uuid so this doesn't leak other users' jobs by
    # guessing, but a stricter deployment would also store job->user_id and
    # check ownership here. Left as a documented follow-up rather than
    # silently assumed-safe.
    return await job_queue.get_status(job_id)


@router.get("/workplace/clips")
async def list_workplace_clips(user_id: str = Depends(require_user)):
    clips = ClipRepo.list_for_user(user_id)
    drafts = [c for c in clips if not _is_live_youtube_url(c.get("youtube_url", ""))]
    return {"clips": drafts}


@router.get("/clips")
async def list_published_clips(user_id: str = Depends(require_user)):
    clips = ClipRepo.list_for_user(user_id)
    live = [c for c in clips if _is_live_youtube_url(c.get("youtube_url", ""))]
    total_views = sum(c.get("views", 0) for c in live)
    return {
        "videos": live,
        "total_views": total_views,
        "total_videos": len(live),
        "avg_views": total_views // len(live) if live else 0,
    }


@router.post("/clip/publish-draft")
async def publish_draft(payload: PublishDraftRequest, user_id: str = Depends(require_user)):
    ok = ClipRepo.update(payload.clip_id, user_id, {
        "status": "published",
        "title": payload.title or None,
    })
    if not ok:
        raise HTTPException(status_code=404, detail="Clip not found in your Workplace")
    return {"status": "success", "message": "Clip submitted for YouTube publishing!"}


@router.delete("/clip/{clip_id}")
async def delete_clip(clip_id: str, user_id: str = Depends(require_user)):
    ClipRepo.delete(clip_id, user_id)
    return {"status": "success"}


def _is_live_youtube_url(url: str) -> bool:
    return bool(url) and ("youtube.com" in url or "youtu.be" in url)