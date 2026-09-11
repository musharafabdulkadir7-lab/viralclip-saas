# HANDOFF — ClipAI pipeline, v2 (own_content + licensed_cc, hardened)

## What v2 adds on top of the mode rewrite
- **config.py** — every tunable (thresholds, timeouts, paths, secrets) now
  comes from env vars via a single `Settings` dataclass, instead of scattered
  `os.environ.get()` calls. Supports a `.env` file if `python-dotenv` is
  installed.
- **logging_setup.py** — structured logging (`get_logger(name)`) replacing
  `print()` everywhere. Set `CLIPAI_LOG_JSON=1` for JSON-line output if you
  want to ship logs to something like Datadog/Loki.
- **Retries** — network calls (YouTube Data API search/details, the
  clip-analysis backend call, yt-dlp downloads) now retry with exponential
  backoff via `tenacity`, bounded by `CLIPAI_MAX_RETRIES` (default 3).
- **Type hints + dataclasses** — `VideoCandidate`, `DownloadResult`,
  `ClipSegment`, `ClipJob` replace the old loose dicts, so mismatched keys
  fail at call time instead of silently producing `None`/`KeyError`s deep in
  the pipeline.
- **Custom exceptions** — `VideoFinderError`, `DownloadError`, `ClipCutError`,
  `PipelineError`, `UploadError` instead of bare `Exception`. `worker.py`
  catches these specifically and reports a clean message; anything
  unexpected is logged with a full traceback (`log.exception(...)`) rather
  than swallowed.
- **Webhook support** — set `CLIPAI_WEBHOOK_URL` (and optionally
  `CLIPAI_WEBHOOK_SECRET` for an HMAC-SHA256 signature in the
  `X-ClipAI-Signature` header) and every `update_job_status` call also POSTs
  a JSON event there, independent of the website's own progress API. This is
  the integration point for Antigravity or any other external listener —
  point its webhook receiver at this URL and it gets every status update the
  website gets, without needing to poll or share code.
- **CLI** — `python worker.py --mode licensed_cc --user-id U --niche "cooking tips"`
  runs one job end-to-end from the command line (see below). Useful for
  manual testing, cron-triggered runs, or letting Antigravity shell out to
  it directly instead of importing it as a library.
- **Tests** — `tests/` covers the pure logic: ISO8601 duration parsing, VTT
  parsing, ASS timestamp formatting, ffmpeg text escaping, and `ClipJob`
  validation rules. These don't hit the network or ffmpeg, so they run fast
  and are safe for CI. Run with `pytest` from the project root.

## Everything from the v1 handoff still applies
- Two modes: `own_content` (`source_kind="file"` or `"channel"`) and
  `licensed_cc` (searches YouTube Data API, CC-licensed only, re-verified
  server-side via `status.license`).
- No scraping fallback, no client-fingerprint rotation, no proxy evasion, no
  fingerprint-defeating transforms, no auto-fetched b-roll. Same "do not
  reintroduce" list as before — see below.
- Attribution is mandatory (not optional) in the upload description for
  `licensed_cc` mode.

## CLI usage
```bash
# Render only, no upload — good for testing
python worker.py --mode licensed_cc --user-id U --niche "cooking tips" --no-upload

# Full pipeline, own uploaded file, auto-upload to the user's channel
python worker.py --mode own_content --user-id U --source-kind file \
  --source /path/to/video.mp4 --job-id job-123

# Own channel video, split-screen layout with licensed b-roll
python worker.py --mode own_content --user-id U --source-kind channel \
  --source dQw4w9WgXcQ --layout split_screen --broll-path /path/to/broll.mp4
```

## Config reference (env vars)
| Var | Default | Notes |
|---|---|---|
| `YOUTUBE_API_KEY` | — | required for `licensed_cc` |
| `WORKER_SECRET` | — | required always (creds fetch HMAC) |
| `API_BASE_URL` | `https://viralclip-saas.onrender.com` | website job-status API |
| `REDIS_URL` | `redis://localhost:6379/0` | optional, falls back to JSON file |
| `CLIPAI_MIN_VIEWS` | `50000` | CC search filter |
| `CLIPAI_MIN_DURATION_SEC` | `300` | CC search filter |
| `CLIPAI_MAX_AGE_DAYS` | `730` | CC search filter |
| `CLIPAI_TOP_N` | `3` | candidates considered per search |
| `CLIPAI_MAX_SHORT_SEC` | `56` | render cap, stays under YT's 60s limit |
| `CLIPAI_DEFAULT_WATERMARK` | `@YourChannel` | fallback if job doesn't set one |
| `CLIPAI_FFMPEG_TIMEOUT` | `600` | seconds |
| `CLIPAI_HTTP_TIMEOUT` | `20` | seconds, per API call |
| `CLIPAI_MAX_RETRIES` | `3` | applies to API calls + downloads |
| `CLIPAI_WEBHOOK_URL` | — | optional, generic status webhook |
| `CLIPAI_WEBHOOK_SECRET` | — | optional, HMAC-signs webhook body |
| `CLIPAI_LOG_LEVEL` | `INFO` | |
| `CLIPAI_LOG_JSON` | `false` | |

## Explicitly do not reintroduce
- Any yt-dlp *search* fallback when the Data API path fails.
- Client-fingerprint rotation (`ios`/`android`/`mweb`/`tv`) or proxy routing
  used to get past bot-detection.
- Speed/color/crop transforms whose purpose is to alter a fingerprint rather
  than serve a real formatting/aesthetic goal.
- Auto-downloading third-party footage without an explicit rights check.
- A "blacklist of major studios" as a stand-in for an actual license check.

## Open items for you to wire up
- Website: file upload form → `source_kind="file"`; channel picker UI →
  `source_kind="channel"`.
- Consent screen: add `youtube.readonly` scope for own-channel mode.
- Point `CLIPAI_WEBHOOK_URL` at Antigravity (or whatever's consuming job
  events) if you want it watching pipeline runs live.
- `pip install -r requirements.txt` and `pytest` in CI before merging any
  change to these modules.
