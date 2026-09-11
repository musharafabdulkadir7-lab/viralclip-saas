"""Ported from v2 unchanged — resumable chunked upload with token

auto-refresh, never raises (returns a dict either way)."""

from __future__ import annotations



import time

from typing import Callable, Optional



from google.auth.transport.requests import Request

from google.oauth2.credentials import Credentials

from googleapiclient.discovery import build

from googleapiclient.http import MediaFileUpload



from .logging_setup import get_logger



log = get_logger("youtube_uploader")



SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

CHUNK_SIZE = 16 * 1024 * 1024

MAX_UPLOAD_RETRIES = 5





def get_authenticated_service(creds_dict: dict):

    if not creds_dict:

        return None

    creds = Credentials(

        token=creds_dict.get("token"), refresh_token=creds_dict.get("refresh_token"),

        client_id=creds_dict.get("client_id"), client_secret=creds_dict.get("client_secret"),

        token_uri="https://oauth2.googleapis.com/token", scopes=SCOPES,

    )

    if creds.expired and creds.refresh_token:

        creds.refresh(Request())

    return build("youtube", "v3", credentials=creds)





def upload_video_to_youtube(video_path: str, title: str, description: str, tags: list[str], creds_dict: dict,

                             progress_callback: Optional[Callable[[int], None]] = None, privacy_status: str = "public") -> dict:

    import os

    if not os.path.exists(video_path):

        return {"error": f"Video file not found at {video_path}"}

    try:

        youtube = get_authenticated_service(creds_dict)

    except Exception as e:

        return {"error": f"Auth error: {e}"}

    if not youtube:

        return {"error": "Authentication failed."}



    body = {"snippet": {"title": title, "description": description, "tags": tags, "categoryId": "22"}, "status": {"privacyStatus": privacy_status}}

    media = MediaFileUpload(video_path, chunksize=CHUNK_SIZE, resumable=True)

    request = youtube.videos().insert(part=",".join(body.keys()), body=body, media_body=media)



    response, retries = None, 0

    while response is None:

        try:

            status, response = request.next_chunk()

            if status and progress_callback:

                try:

                    progress_callback(int(status.progress() * 100))

                except Exception as e:

                    log.warning("progress_callback raised: %s", e)

        except Exception as e:

            retries += 1

            if retries > MAX_UPLOAD_RETRIES:

                return {"error": str(e)}

            time.sleep(2 * retries)



    return {"status": "success", "video_id": response.get("id"), "url": f"https://youtube.com/shorts/{response.get('id')}"}