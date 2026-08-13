FROM python:3.11-slim

# Install system dependencies (ffmpeg for video processing, imagemagick for subtitles)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    imagemagick \
    fonts-liberation \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Fix ImageMagick policy to allow TextClip generation in moviepy (handles IM6 and IM7)
RUN find /etc/ImageMagick* -name policy.xml -exec sed -i 's/none/read,write/g' {} \; 2>/dev/null || true

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
