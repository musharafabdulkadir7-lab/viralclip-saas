#!/bin/bash
set -e

echo "Starting ViralClip AI SaaS..."

# Start the background RQ worker (video processing daemon)
echo "Launching background worker..."
python worker.py &

# Give the worker a moment to initialize
sleep 2

# Start the FastAPI web server on port 7860 (Hugging Face default)
echo "Launching web server on port 7860..."
exec uvicorn main:app --host 0.0.0.0 --port 7860
