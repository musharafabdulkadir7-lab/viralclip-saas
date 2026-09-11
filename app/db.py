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


class UserRepo:
    @staticmethod
    def get_or_create(user_id: str) -> dict:
        db = get_client()
        default = {"id": user_id, "free_clips_used": 0, "license": "free_tier"}
        if not db:
            return default
        try:
            res = db.table("users").select("*").eq("id", user_id).execute()
            if res.data:
                return res.data[0]
            db.table("users").insert(default).execute()
            return default
        except Exception as e:
            log.error("get_or_create(%s) failed: %s", user_id, e)
            return default

    @staticmethod
    def get_by_email(email: str) -> Optional[dict]:
        db = get_client()
        if not db:
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
        res = db.table("invites").select("*").eq("token", token).eq("redeemed", False).execute()
        if not res.data:
            return False
        db.table("invites").update({"redeemed": True, "redeemed_by": user_id}).eq("token", token).execute()
        return True