#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
#  ClipAI Cloud Worker — Oracle Cloud Ubuntu 22.04 ARM Setup
#  Run once on a fresh VM: bash oracle_setup.sh
# ═══════════════════════════════════════════════════════════
set -e

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   ClipAI Cloud Worker Setup Script   ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── 1. System packages ─────────────────────────────────────
echo "[1/6] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3.11 python3.11-venv python3-pip \
    ffmpeg git curl wget unzip \
    build-essential libssl-dev

# ── 2. Python dependencies ─────────────────────────────────
echo "[2/6] Installing Python packages..."
pip3 install --upgrade pip --quiet
pip3 install --quiet \
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

# ── 3. Clone / update repo ─────────────────────────────────
echo "[3/6] Cloning ClipAI repo..."
REPO_DIR="$HOME/viralclip"
if [ -d "$REPO_DIR/.git" ]; then
    echo "  Repo already exists — pulling latest..."
    git -C "$REPO_DIR" pull --quiet
else
    git clone https://github.com/musharafabdulkadir7-lab/viralclip-saas.git "$REPO_DIR" --quiet
fi

# ── 4. Write user_id and API config ────────────────────────
echo "[4/6] Writing config..."
mkdir -p "$HOME/.clipai/generated_videos"
mkdir -p "$HOME/.clipai/downloaded_videos"
mkdir -p "$HOME/.clipai/bin"
mkdir -p "$HOME/.clipai/broll"
mkdir -p "$HOME/.clipai/hot_pool"

# Write the user_id — this is the cloud worker's shared account
# All jobs are polled from the server queue, per-job user_id is used for tracking
echo "user_43065" > "$HOME/.clipai/user_id.txt"

echo "[4/6] Config written."

# ── 5. Systemd service ─────────────────────────────────────
echo "[5/6] Creating systemd service..."
PYTHON_BIN=$(which python3)

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
Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable clipai-worker
sudo systemctl start clipai-worker

# ── 6. Done ────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════╗"
echo "║   ✅  ClipAI Worker is running!      ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "  Check status: sudo systemctl status clipai-worker"
echo "  Live logs:    sudo journalctl -fu clipai-worker"
echo "  Restart:      sudo systemctl restart clipai-worker"
echo ""
