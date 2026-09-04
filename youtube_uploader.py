import os
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ['https://www.googleapis.com/auth/youtube.upload']

def get_authenticated_service(creds_dict):
    """
    Builds the YouTube service object using OAuth credentials from the database.
    Auto-refreshes expired tokens and saves the new token back to Supabase.
    """
    if not creds_dict:
        return None
        
    creds = Credentials(
        token=creds_dict.get("token"),
        refresh_token=creds_dict.get("refresh_token"),
        client_id=creds_dict.get("client_id"),
        client_secret=creds_dict.get("client_secret"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES
    )
            
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # Save the new refreshed access token back to Supabase so it's valid next time
        try:
            from supabase import create_client
            supabase_url = os.environ.get("SUPABASE_URL", "")
            supabase_key = os.environ.get("SUPABASE_KEY", "")
            user_id = creds_dict.get("user_id")
            if supabase_url and supabase_key and user_id:
                sb = create_client(supabase_url, supabase_key)
                sb.table("users").update({
                    "youtube_access_token": creds.token
                }).eq("id", user_id).execute()
                print("[Auth] Refreshed and saved new YouTube access token.")
        except Exception as e:
            print(f"[Auth] Warning: Could not save refreshed token: {e}")
            
    return build('youtube', 'v3', credentials=creds)

def upload_video_to_youtube(video_path, title, description, tags, creds_dict, progress_callback=None):
    """
    Uploads a video to YouTube using the authenticated service.
    """
    if not os.path.exists(video_path):
        return {"error": f"Video file not found at {video_path}"}
        
    try:
        youtube = get_authenticated_service(creds_dict)
    except Exception as e:
        return {"error": f"Auth error: {str(e)}"}
        
    if not youtube:
        return {"error": "Authentication failed. Missing or invalid credentials."}

    print(f"--- YOUTUBE UPLOAD INITIATED ---")
    
    body = {
        'snippet': {
            'title': title,
            'description': description,
            'tags': tags,
            'categoryId': '22' # 22 = People & Blogs
        },
        'status': {
            'privacyStatus': 'public'  # Post publicly to YouTube
        }
    }
    
    # For YouTube Shorts (typically 15MB - 35MB), 4MB chunks force 5-10 roundtrip HTTP requests.
    # Increasing CHUNK_SIZE to 16MB reduces roundtrips to 1-2 requests, significantly speeding up uploads.
    # If progress_callback is provided, update UI progress in real time.
    CHUNK_SIZE = 16 * 1024 * 1024
    media_file = MediaFileUpload(video_path, chunksize=CHUNK_SIZE, resumable=True)
    request = youtube.videos().insert(
        part=','.join(body.keys()),
        body=body,
        media_body=media_file
    )

    print("Uploading file in accelerated chunks...")
    response = None
    retries = 0
    max_retries = 5
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                print(f"  Upload progress: {pct}%")
                if progress_callback:
                    try:
                        progress_callback(pct)
                    except Exception:
                        pass
        except Exception as e:
            retries += 1
            if retries > max_retries:
                print(f"Upload failed after {max_retries} retries: {e}")
                return {"error": str(e)}
            print(f"  Connection error (attempt {retries}/{max_retries}), retrying...")
            import time
            time.sleep(2 * retries)

    print(f"--- YOUTUBE UPLOAD COMPLETE ---")
    return {
        "status": "success",
        "video_id": response.get("id"),
        "url": f"https://youtube.com/shorts/{response.get('id')}"
    }
