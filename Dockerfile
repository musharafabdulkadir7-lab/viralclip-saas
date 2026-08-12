FROM python:3.11-slim

# Install system dependencies (ffmpeg for video processing)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first to leverage layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the full application
COPY . .

# Make start script executable
RUN chmod +x start.sh

# Expose Hugging Face default port
EXPOSE 7860

# Run the entrypoint
CMD ["./start.sh"]
