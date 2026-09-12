"""
app/db.py
Thin, typed wrapper around Supabase. v2 called `supabase.table(...)`
directly from inside route handlers, ~30 times, each with its own
try/except and its own print(). That means a schema/field rename has to
be hunted down across the whole file. Centralizing it here means a
schema change touches one file, and every call site gets the same
error handling and logging for free.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from supabase import Client, create_client

from .config import get_settings
from .logging_conf import get_logger

log = get_logger("db")
settings = get_settings()


@lru_cache
def get_client() -> Optional[Client]:
    if not settings.supabase_url or not settings.supabase_key:
        return None
    try:
        return create_client(settings.supabase_url, settings.supabase_key)
    except Exception as e:
        log.error("Supabase init failed: %s", e)
        return None


_in_memory_users = {}

class UserRepo:
    @staticmethod
    def get_or_create(user_id: str) -> dict:
        db = get_client()
        if not db:
            if user_id not in _in_memory_users:
                _in_memory_users[user_id] = {"id": user_id, "free_clips_used": 0, "license": "free_tier"}
            return _in_memory_users[user_id]
        try:
            res = db.table("users").select("*").eq("id", user_id).execute()
            if res.data:
                return res.data[0]
            default = {"id": user_id, "free_clips_used": 0, "license": "free_tier"}
            db.table("users").insert(default).execute()
            return default
        except Exception as e:
            log.error("get_or_create(%s) failed: %s", user_id, e)
            return {"id": user_id, "free_clips_used": 0, "license": "free_tier"}

    @staticmethod
    def get_by_email(email: str) -> Optional[dict]:
        db = get_client()
        if not db:
            for u in _in_memory_users.values():
                if u.get("email") == email:
                    return u
            return None
        res = db.table("users").select("*").eq("email", email).execute()
        return res.data[0] if res.data else None

    @staticmethod
    def update(user_id: str, fields: dict) -> None:
        db = get_client()
        if not db:
            return
        try:
            db.table("users").update(fields).eq("id", user_id).execute()
        except Exception as e:
            log.error("update(%s, %s) failed: %s", user_id, list(fields), e)

    @staticmethod
    def increment_free_used(user_id: str, current: int) -> None:
        UserRepo.update(user_id, {"free_clips_used": current + 1})

    @staticmethod
    def atomic_consume_free_clip(user_id: str, limit: int) -> tuple[bool, int]:
        """Atomically checks and consumes 1 free tier clip if under the limit.
        Returns (allowed, new_used_count). Fails closed on database errors."""
        db = get_client()
        if not db:
            # When running without DB (in-memory dev/test fallback)
            # Fetch user, check limit, increment
            user = UserRepo.get_or_create(user_id)
            used = user.get("free_clips_used", 0)
            if used >= limit:
                return False, used
            new_used = used + 1
            user["free_clips_used"] = new_used
            return True, new_used

        try:
            # Query current user state
            res = db.table("users").select("id, license, free_clips_used").eq("id", user_id).execute()
            if not res.data:
                # Ensure user exists
                UserRepo.get_or_create(user_id)
                res = db.table("users").select("id, license, free_clips_used").eq("id", user_id).execute()

            user = res.data[0]
            used = user.get("free_clips_used", 0)
            if user.get("license") != "free_tier":
                return True, used
            if used >= limit:
                return False, used

            # Atomic conditional update: only increment if free_clips_used is still < limit
            up_res = db.table("users").update({"free_clips_used": used + 1}).eq("id", user_id).eq("free_clips_used", used).execute()
            if not up_res.data:
                # Concurrent race lost: re-check whether we're still under limit
                refetch = db.table("users").select("free_clips_used").eq("id", user_id).execute()
                latest_used = refetch.data[0]["free_clips_used"] if refetch.data else used + 1
                if latest_used < limit:
                    # Retry the conditional update with the fresh value
                    retry_res = db.table("users").update({"free_clips_used": latest_used + 1}).eq("id", user_id).eq("free_clips_used", latest_used).execute()
                    if retry_res.data:
                        return True, latest_used + 1
                return False, latest_used

            return True, used + 1
        except Exception as e:
            log.error("atomic_consume_free_clip(%s) database error: %s", user_id, e)
            raise

    @staticmethod
    def refund_free_clip(user_id: str) -> None:
        """Compensating action: refunds 1 free clip if subsequent queue enqueue fails."""
        db = get_client()
        if not db:
            user = UserRepo.get_or_create(user_id)
            user["free_clips_used"] = max(0, user.get("free_clips_used", 1) - 1)
            return
        try:
            res = db.table("users").select("free_clips_used").eq("id", user_id).execute()
            if res.data:
                cur = res.data[0].get("free_clips_used", 1)
                db.table("users").update({"free_clips_used": max(0, cur - 1)}).eq("id", user_id).execute()
        except Exception as e:
            log.error("refund_free_clip(%s) failed: %s", user_id, e)


class ClipRepo:
    @staticmethod
    def list_for_user(user_id: str) -> list[dict]:
        db = get_client()
        if not db:
            return []
        res = db.table("clips").select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
        return res.data or []

    @staticmethod
    def insert(row: dict) -> None:
        db = get_client()
        if not db:
            return
        db.table("clips").insert(row).execute()

    @staticmethod
    def update(clip_id: str, user_id: str, fields: dict) -> bool:
        db = get_client()
        if not db:
            return False
        res = db.table("clips").select("id").eq("id", clip_id).eq("user_id", user_id).execute()
        if not res.data:
            return False
        db.table("clips").update(fields).eq("id", clip_id).execute()
        return True

    @staticmethod
    def delete(clip_id: str, user_id: str) -> None:
        db = get_client()
        if db:
            db.table("clips").delete().eq("id", clip_id).eq("user_id", user_id).execute()


class InviteRepo:
    @staticmethod
    def create(token: str) -> None:
        db = get_client()
        if db:
            db.table("invites").insert({"token": token, "redeemed": False}).execute()

    @staticmethod
    def redeem(token: str, user_id: str) -> bool:
        db = get_client()
        if not db:
            return False
        # Atomic conditional update: only update if still unredeemed
        up_res = db.table("invites").update({"redeemed": True, "redeemed_by": user_id}).eq("token", token).eq("redeemed", False).execute()
        if not up_res.data:
            return False
        return True


class PartnerChannelRepo:
    @staticmethod
    def get_by_channel_id(channel_id: str) -> Optional[dict]:
        db = get_client()
        if not db:
            return None
        try:
            res = db.table("partner_channels").select("*").eq("channel_id", channel_id).eq("active", True).execute()
            return res.data[0] if res.data else None
        except Exception as e:
            log.error("PartnerChannelRepo.get_by_channel_id(%s) failed: %s", channel_id, e)
            return None

    @staticmethod
    def list_active() -> list[dict]:
        db = get_client()
        if not db:
            return []
        try:
            res = db.table("partner_channels").select("*").eq("active", True).execute()
            return res.data or []
        except Exception as e:
            log.error("PartnerChannelRepo.list_active failed: %s", e)
            return []

    @staticmethod
    def onboard(channel_id: str, channel_title: str, owner_user_id: str) -> None:
        db = get_client()
        if db:
            try:
                db.table("partner_channels").insert({
                    "channel_id": channel_id, "channel_title": channel_title,
                    "owner_user_id": owner_user_id, "active": True,
                }).execute()
            except Exception as e:
                log.error("PartnerChannelRepo.onboard failed: %s", e)