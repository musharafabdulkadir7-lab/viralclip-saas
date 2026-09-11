#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
#  ClipAI Cloud Worker — Oracle Linux 9 Setup
# ═══════════════════════════════════════════════════════════
set -e

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   ClipAI Cloud Worker Setup Script   ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── 1. Enable EPEL and CodeReady Builder for ffmpeg ────────
echo "[1/6] Installing EPEL and repositories..."
sudo dnf install -y oracle-epel-release-el9 || sudo dnf install -y epel-release || true

# ── 2. Install Python 3.11, git, tools ───────────────────
echo "[2/6] Installing system packages..."
sudo dnf install -y python3.11 python3.11-pip git curl wget tar bzip2 xz make gcc openssl-devel

# Install ffmpeg static build
if ! command -v ffmpeg &> /dev/null; then
    echo "Installing static ffmpeg build..."
    ARCH=$(uname -m)
    if [ "$ARCH" = "aarch64" ]; then
        curl -sL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz | tar -xJ -C /tmp
        sudo cp /tmp/ffmpeg-*-arm64-static/ffmpeg /usr/local/bin/
        sudo cp /tmp/ffmpeg-*-arm64-static/ffprobe /usr/local/bin/
    else
        curl -sL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz | tar -xJ -C /tmp
        sudo cp /tmp/ffmpeg-*-amd64-static/ffmpeg /usr/local/bin/
        sudo cp /tmp/ffmpeg-*-amd64-static/ffprobe /usr/local/bin/
    fi
    sudo chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe || true
fi

# ── 3. Python dependencies ─────────────────────────────────
echo "[3/6] Installing Python packages with Python 3.11..."
python3.11 -m pip install --upgrade pip --quiet
python3.11 -m pip install --quiet \
    yt-dlp \
    requests \
    imageio-ffmpeg \
    google-api-python-client \
    google-auth-httplib2 \
    google-auth-oauthlib \
    openai \
    anthropic \
    praw \
    supabase

# ── 4. Clone / update repo ─────────────────────────────────
echo "[4/6] Cloning ClipAI repo..."
REPO_DIR="$HOME/viralclip"
if [ -d "$REPO_DIR/.git" ]; then
    echo "  Repo already exists — pulling latest..."
    git -C "$REPO_DIR" pull --quiet
else
    git clone https://github.com/musharafabdulkadir7-lab/viralclip-saas.git "$REPO_DIR" --quiet
fi

# ── 5. Write user_id and directories ───────────────────────
echo "[5/6] Writing config..."
mkdir -p "$HOME/.clipai/generated_videos"
mkdir -p "$HOME/.clipai/downloaded_videos"
mkdir -p "$HOME/.clipai/bin"
mkdir -p "$HOME/.clipai/broll"
mkdir -p "$HOME/.clipai/hot_pool"

echo "user_43065" > "$HOME/.clipai/user_id.txt"

# ── 6. Systemd service ─────────────────────────────────────
echo "[6/6] Creating systemd service..."
PYTHON_BIN=$(which python3.11)

sudo tee /etc/systemd/system/clipai-worker.service > /dev/null <<SERVICE
[Unit]
Description=ClipAI Cloud Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$REPO_DIR
ExecStart=$PYTHON_BIN $REPO_DIR/client_worker.py --cloud
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
Environment="API_BASE_URL=https://viralclip-saas.onrender.com"
Environment="PATH=/usr/local/bin:/usr/bin:/bin:$HOME/bin"

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable clipai-worker
sudo systemctl restart clipai-worker

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   ✅  ClipAI Worker is running!      ║"
echo "╚══════════════════════════════════════╝"
