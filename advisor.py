"""
advisor.py — Gemini AI advisor that reviews code and gives tips
Run: python advisor.py <question or file>
"""
import sys, os
from google import genai

GEMINI_KEY = os.environ.get("GEMINI_ADVISOR_KEY", "")
MODEL      = "gemini-3.6-flash"
LOG_FILE   = "advisor_tips.md"

SYSTEM_PROMPT = """You are an elite senior software engineer reviewing a YouTube SaaS automation project.
The project downloads viral YouTube videos, cuts the best 45-second clip, and auto-uploads to the user's YouTube channel.
Stack: Python, FastAPI, Redis, Supabase, Render (cloud), Oracle VM (worker), yt-dlp, ffmpeg.

When asked to review code or suggest improvements:
- Be direct and specific — no filler
- Prioritize fixes that increase reliability and reduce errors
- Flag any critical bugs immediately
- Suggest battle-tested patterns used by top SaaS companies
- Keep responses concise and actionable"""

client = genai.Client(api_key=GEMINI_KEY)

def ask(question: str) -> str:
    response = client.models.generate_content(
        model=MODEL,
        contents=f"{SYSTEM_PROMPT}\n\n{question}"
    )
    return response.text

def review_file(filepath: str) -> str:
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        code = f.read()
    return ask(f"Review this file and give top 5 actionable improvements:\n\nFile: {filepath}\n\n`python\n{code[:8000]}\n`")

def log_tip(tip: str, context: str = ""):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        from datetime import datetime
        f.write(f"\n---\n### {datetime.now().strftime('%H:%M:%S')} — {context}\n{tip}\n")
    print(f"[Advisor] Tip logged to {LOG_FILE}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python advisor.py 'your question'")
        print("       python advisor.py --file automation.py")
        sys.exit(0)

    if sys.argv[1] == "--file":
        filepath = sys.argv[2]
        print(f"[Advisor] Reviewing {filepath} with Gemini 3.6...")
        tip = review_file(filepath)
    else:
        question = " ".join(sys.argv[1:])
        print(f"[Advisor] Asking Gemini: {question[:80]}...")
        tip = ask(question)

    print("\n" + "="*60)
    print(tip)
    print("="*60)
    log_tip(tip, context=" ".join(sys.argv[1:])[:60])
