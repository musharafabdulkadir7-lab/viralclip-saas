# ClipAI pipeline (v2)

Two legitimate video-sourcing modes, hardened with retries, structured
logging, type hints, tests, a config file, and a CLI. See `HANDOFF.md` for
the full architecture writeup and `NOTES.md` for the earlier mode-rewrite
summary.

## Quick start
```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your keys
pytest                 # run the test suite
python worker.py --mode licensed_cc --user-id demo --niche "cooking tips" --no-upload
```
