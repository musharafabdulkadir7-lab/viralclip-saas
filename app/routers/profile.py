from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request

from ..config import get_settings
from ..db import InviteRepo, UserRepo
from ..schemas import AutoPostSettings, UserProfileOut
from ..security import require_user, verify_admin
from ..redis_client import get_redis

router = APIRouter(prefix="/api/v1", tags=["profile"])
settings = get_settings()


@router.get("/user/profile", response_model=UserProfileOut)
async def get_profile(user_id: str = Depends(require_user)):
    user = UserRepo.get_or_create(user_id)
    return UserProfileOut(
        user_id=user.get("id", user_id),
        email=user.get("email", ""),
        license=user.get("license", "free_tier"),
        free_clips_used=user.get("free_clips_used", 0),
        referral_link=f"{settings.api_base_url}/?ref={user.get('id', user_id)}",
    )


@router.get("/auto-post/settings")
async def get_auto_post_settings(user_id: str = Depends(require_user)):
    r = get_redis()
    default = {"enabled": False, "times": ["12:00"], "niche": "motivation",
               "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], "rights_confirmed": False}
    if not r:
        return default
    import json
    data = await r.hgetall(f"user:{user_id}:autopost")
    if not data:
        return default
    return {
        "enabled": data.get("enabled") == "True",
        "times": json.loads(data.get("times", '["12:00"]')),
        "niche": data.get("niche", "motivation"),
        "days": json.loads(data.get("days", '["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]')),
        "rights_confirmed": data.get("rights_confirmed") == "True",
    }


@router.post("/auto-post/settings")
async def save_auto_post_settings(payload: AutoPostSettings, user_id: str = Depends(require_user)):
    r = get_redis()
    if r:
        import json
        await r.hset(f"user:{user_id}:autopost", mapping={
            "enabled": str(payload.enabled),
            "times": json.dumps(payload.times),
            "niche": payload.niche,
            "days": json.dumps(payload.days),
            "rights_confirmed": str(payload.rights_confirmed),
        })
        if payload.enabled:
            await r.sadd("autopost:enabled", user_id)
        else:
            await r.srem("autopost:enabled", user_id)
    return {"status": "success"}



@router.post("/admin/generate-invite")
async def generate_invite(request: Request, count: int = 1, _=Depends(verify_admin)):
    count = max(1, min(count, 100))
    links = []
    base_url = str(request.base_url).rstrip("/")
    for _ in range(count):
        token = secrets.token_urlsafe(24)
        InviteRepo.create(token)
        links.append(f"{base_url}/redeem/{token}")
    return {"links": links}


@router.get("/redeem/{token}")
async def redeem_invite(token: str, response=None):
    from fastapi.responses import RedirectResponse
    import uuid
    from ..security import issue_session_token, SESSION_COOKIE, SESSION_TTL_SEC

    new_user_id = f"user_{uuid.uuid4().hex[:8]}"
    UserRepo.get_or_create(new_user_id)
    UserRepo.update(new_user_id, {"license": "pro"})
    if not InviteRepo.redeem(token, new_user_id):
        raise HTTPException(status_code=400, detail="Invalid or already used invite link.")

    token_val = issue_session_token(new_user_id)
    redir = RedirectResponse(url="/", status_code=302)
    redir.set_cookie(SESSION_COOKIE, token_val, max_age=SESSION_TTL_SEC, httponly=True, samesite="lax", secure=settings.env == "production")
    return redir