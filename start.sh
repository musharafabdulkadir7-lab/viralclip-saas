#!/bin/bash
set -e

echo "Starting ViralClip AI SaaS..."

# Start the background worker daemon (Redis stream processor)
echo "Launching background worker daemon..."
python client_worker.py &

# Give the worker a moment to initialize
sleep 2

# Start the FastAPI web server on port 7860 (Hugging Face default)
echo "Launching web server on port 7860..."
exec uvicorn app.main:app --host 0.0.0.0 --port 7860
