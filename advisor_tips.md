
---
### 09:40:48 — --file automation.py
Here are the top 5 critical improvements to bring this code to production-grade SaaS standards for reliability, scalability, and performance.

---

### 1. **CRITICAL: Replace Ephemeral Local JSON with Supabase / Redis State**
* **Problem:** Storing state in `~/.clipai/used_videos.json` fails on cloud workers (Render / Oracle VM). Redeployments wipe state, concurrent workers cause file lock corruption, and duplicate videos will get processed—triggering YouTube duplicate content strikes.
* **Fix:** Persist processed video IDs directly in **Supabase** (with a `UNIQUE` constraint on `video_id`) or a **Redis Set** (`SADD processed_videos <id>`).

```python
# Redis / Supabase Atomic Deduplication Pattern
async def is_video_processed(video_id: str, redis_client) -> bool:
    return await redis_client.sismember("processed_youtube_ids", video_id)

async def mark_video_processed(video_id: str, metadata: dict, redis_client, db_client):
    await redis_client.sadd("processed_youtube_ids", video_id)
    # Async background task to commit persistent state
    db_client.table("processed_videos").insert({"video_id": video_id, **metadata}).execute()
```

---

### 2. **CRITICAL: Fix `_safe()` Unicode Destruction**
* **Problem:** `text.encode("ascii", errors="replace")` strips non-ASCII characters, mangling emojis, foreign languages, and non-English viral video titles into strings of `????`.
* **Fix:** Delete `_safe()`. Python 3 handles UTF-8 natively. Use standard string stripping or `unicodedata.normalize` if needed for filenames.

```python
import unicodedata

def clean_filename(text: str) -> str:
    """Sanitize string only for OS filesystem operations, keeping metadata UTF-8 intact."""
    return "".join(c for c in text if c.isalnum() or c in (" ", "_", "-")).rstrip()
```

---

### 3. **Resilience: Handle YouTube API Quota Exhaustion (Code 403) with Seamless Fallback**
* **Problem:** `_search_via_api` costs **100 quota units** per search call (default quota is 10,000 units = 100 calls/day). When quota exhausts, `httpx.get` raises an unhandled `Exception`, crashing the workflow instead of falling back to `_search_via_ytdlp`.
* **Fix:** Catch HTTP `403 Forbidden` / `429 Too Many Requests` specifically and trigger `_search_via_ytdlp` seamlessly.

```python
def search_videos(niche: str) -> list:
    if YOUTUBE_API_KEY:
        try:
            return _search_via_api(niche)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (403, 429):
                log(f"[VideoFinder] API quota exceeded/blocked ({e.response.status_code}). Falling back to yt-dlp.")
            else:
                log(f"[VideoFinder] API Error: {e}")
        except Exception as e:
            log(f"[VideoFinder] API search failed unexpectedly: {e}")

    # Seamless fallback
    return _search_via_ytdlp(niche)
```

---

### 4. **Network Hardening: Add Retries & Exponential Backoff**
* **Problem:** Single `httpx.get` calls fail on momentary packet loss, cloud network blips, or transient 5xx errors from Google servers.
* **Fix:** Use `tenacity` decorator for backoff retries on external network calls.

```python
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException)),
    reraise=True
)
def _http_get_with_retry(url: str, params: dict) -> httpx.Response:
    return httpx.get(url, params=params, timeout=15)
```

---

### 5. **Anti-Bot Infrastructure: Remove Fragile Sync Socket Checks for Proxies**
* **Problem:** `socket.socket().connect_ex(('127.0.0.1', 1080))` introduces blocking synchronous delay on runtime threads and relies on brittle local SSH tunnels.
* **Fix:** Inject proxy configuration via standard platform environment variables (`YOUTUBE_PROXY`) managed by the worker runtime orchestration layer, and rotate proxies directly inside `yt-dlp`.

```python
# Pass clean proxy configuration directly from environment
proxy_url = os.environ.get("YOUTUBE_PROXY")

ydl_opts = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "skip_download": True,
    "extractor_args": {"youtube": {"player_client": client_profile}},
    "proxy": proxy_url if proxy_url else None
}
```
