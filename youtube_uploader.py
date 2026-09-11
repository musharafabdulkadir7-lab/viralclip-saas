"""
youtube_uploader.py
Resumable, chunked upload to YouTube via the Data API, with OAuth
token auto-refresh.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from config import settings
from logging_setup import get_logger

log = get_logger("youtube_uploader")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CHUNK_SIZE = 16 * 1024 * 1024  # 16MB — fewer round trips for typical Shorts file sizes
MAX_UPLOAD_RETRIES = 5


class UploadError(Exception):
    """Raised when authentication or upload ultimately fails."""


def get_authenticated_service(creds_dict: dict):
    if not creds_dict:
        return None

    creds = Credentials(
        token=creds_dict.get("token"),
        refresh_token=creds_dict.get("refresh_token"),
        client_id=creds_dict.get("client_id"),
        client_secret=creds_dict.get("client_secret"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_refreshed_token(creds_dict.get("user_id"), creds.token)

    return build("youtube", "v3", credentials=creds)


def _save_refreshed_token(user_id: Optional[str], new_token: str) -> None:
    """Best-effort save of the refreshed access token back to storage."""
    try:
        from supabase import create_client
        import os
        supabase_url = os.environ.get("SUPABASE_URL", "")
        supabase_key = os.environ.get("SUPABASE_KEY", "")
        if supabase_url and supabase_key and user_id:
            sb = create_client(supabase_url, supabase_key)
            sb.table("users").update({"youtube_access_token": new_token}).eq("id", user_id).execute()
            log.info("Refreshed and saved new YouTube access token for user %s.", user_id)
    except Exception as e:
        log.warning("Could not persist refreshed token: %s", e)


def upload_video_to_youtube(
    video_path: str,
    title: str,
    description: str,
    tags: list[str],
    creds_dict: dict,
    progress_callback: Optional[Callable[[int], None]] = None,
    privacy_status: str = "public",
) -> dict:
    """
    Uploads a video to YouTube. Returns {"status": "success", "video_id":
    ..., "url": ...} on success or {"error": ...} on failure — never
    raises, so callers can report a clean job-status error either way.
    """
    import os
    if not os.path.exists(video_path):
        return {"error": f"Video file not found at {video_path}"}

    try:
        youtube = get_authenticated_service(creds_dict)
    except Exception as e:
        return {"error": f"Auth error: {e}"}
    if not youtube:
        return {"error": "Authentication failed. Missing or invalid credentials."}

    log.info("Starting YouTube upload: %s", video_path)

    body = {
        "snippet": {"title": title, "description": description, "tags": tags, "categoryId": "22"},
        "status": {"privacyStatus": privacy_status},
    }

    media_file = MediaFileUpload(video_path, chunksize=CHUNK_SIZE, resumable=True)
    request = youtube.videos().insert(part=",".join(body.keys()), body=body, media_body=media_file)

    response = None
    retries = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                log.info("Upload progress: %d%%", pct)
                if progress_callback:
                    try:
                        progress_callback(pct)
                    except Exception as e:
                        log.warning("progress_callback raised: %s", e)
        except Exception as e:
            retries += 1
            if retries > MAX_UPLOAD_RETRIES:
                log.error("Upload failed after %d retries: %s", MAX_UPLOAD_RETRIES, e)
                return {"error": str(e)}
            log.warning("Upload connection error (attempt %d/%d), retrying...", retries, MAX_UPLOAD_RETRIES)
            time.sleep(2 * retries)

    log.info("Upload complete: video_id=%s", response.get("id"))
    return {
        "status": "success",
        "video_id": response.get("id"),
        "url": f"https://youtube.com/shorts/{response.get('id')}",
    }
