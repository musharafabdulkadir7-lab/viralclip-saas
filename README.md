---
title: ViralClip AI SaaS
emoji: 🎬
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# ViralClip AI SaaS

Autonomous YouTube Shorts generation powered by AI. Enter a niche, and the system finds viral content, cuts the best segments, adds captions, and uploads directly to YouTube.

## How It Works

1. Enter a niche (e.g. "finance", "motivation", "fitness")
2. The AI finds viral videos in that category
3. It clips the most engaging 60-second segment
4. Captions are burned in and the video is uploaded as a YouTube Short

## Environment Variables (Secrets)

Set these in your Hugging Face Space Secrets panel:

| Secret | Description |
|---|---|
| `STRIPE_SECRET_KEY` | Your Stripe API key |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret |
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_KEY` | Your Supabase service key |
| `REDIS_URL` | Upstash Redis URL (redis://...) |
