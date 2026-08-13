import os
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ['https://www.googleapis.com/auth/youtube.upload']

def get_authenticated_service(creds_dict):
    """
    Builds the YouTube service object using OAuth credentials from the database.
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
            
    return build('youtube', 'v3', credentials=creds)

def upload_video_to_youtube(video_path, title, description, tags, creds_dict):
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
    
    # Use 4MB chunks for resumable upload (handles network interruptions)
    CHUNK_SIZE = 4 * 1024 * 1024
    media_file = MediaFileUpload(video_path, chunksize=CHUNK_SIZE, resumable=True)
    request = youtube.videos().insert(
        part=','.join(body.keys()),
        body=body,
        media_body=media_file
    )

    print("Uploading file in chunks...")
    response = None
    retries = 0
    max_retries = 5
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                print(f"  Upload progress: {pct}%")
        except Exception as e:
            retries += 1
            if retries > max_retries:
                print(f"Upload failed after {max_retries} retries: {e}")
                return {"error": str(e)}
            print(f"  Connection error (attempt {retries}/{max_retries}), retrying...")
            import time
            time.sleep(3 * retries)  # back-off: 3s, 6s, 9s...

    print(f"--- YOUTUBE UPLOAD COMPLETE ---")
    return {
        "status": "success",
        "video_id": response.get("id"),
        "url": f"https://youtube.com/shorts/{response.get('id')}"
    }
