// ─── YouTube Connection State ─────────────────────────────────────────────────
function updateYouTubeUI(connected) {
    const indicator = document.getElementById('youtube-indicator');
    const statusText = document.getElementById('youtube-status-text');
    const btn = document.getElementById('connect-youtube-btn');

    if (!indicator || !statusText || !btn) return;

    if (connected) {
        indicator.style.background = '#10b981';
        statusText.innerText = 'Connected ✔';
        statusText.style.color = '#10b981';
        btn.innerText = '✅ YouTube Connected';
        btn.style.background = '#1a3a2a';
        btn.style.color = '#10b981';
        btn.style.pointerEvents = 'none';
        btn.style.border = '1px solid #10b981';
    } else {
        indicator.style.background = '#ef4444';
        statusText.innerText = 'Not Connected';
        statusText.style.color = '#94a3b8';
        btn.innerText = '🔗 Connect YouTube';
        btn.style.background = '#ef4444';
        btn.style.color = '#fff';
        btn.style.pointerEvents = 'auto';
        btn.style.border = 'none';
    }
}

// ─── On Page Load ─────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {

    // Check if Google just redirected back with ?youtube=connected
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get('youtube') === 'connected') {
        localStorage.setItem('youtube_connected', 'true');
        window.history.replaceState({}, document.title, window.location.pathname);
        showToast('✅ YouTube connected successfully!', '#10b981');
    } else if (urlParams.get('youtube') === 'error') {
        showToast('❌ YouTube connection failed. Please try again.', '#ef4444');
        window.history.replaceState({}, document.title, window.location.pathname);
    }

    // Always verify against the server — localStorage can lie
    try {
        const res = await fetch('/api/v1/auth/youtube/status');
        const data = await res.json();
        if (data.connected) {
            localStorage.setItem('youtube_connected', 'true');
        }
        updateYouTubeUI(data.connected);
    } catch (e) {
        // Fallback to localStorage if server unreachable
        const cached = localStorage.getItem('youtube_connected') === 'true';
        updateYouTubeUI(cached);
    }

    // Assign unique user ID cookie if not exists
    if (!document.cookie.split('; ').find(r => r.startsWith('user_id='))) {
        document.cookie = `user_id=user_${Math.floor(Math.random()*100000)}; path=/; max-age=31536000`;
    }
});

// ─── Toast Notification ───────────────────────────────────────────────────────
function showToast(message, color) {
    const toast = document.createElement('div');
    toast.style.cssText = `
        position: fixed; top: 24px; left: 50%; transform: translateX(-50%);
        background: ${color}; color: white; padding: 14px 28px;
        border-radius: 12px; font-weight: 600; font-size: 15px;
        box-shadow: 0 4px 20px rgba(0,0,0,0.4); z-index: 9999;
        animation: fadeIn 0.3s ease;
    `;
    toast.innerText = message;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
}

// ─── Add Chat Message ─────────────────────────────────────────────────────────
function addMessage(sender, text, className) {
    const chatWindow = document.getElementById('chat-window');
    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${className}`;
    let avatar = sender === 'You' ? '👤' : '🤖';
    let formattedText = text.replace(/\n/g, '<br>').replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
    messageDiv.innerHTML = `
        <div class="avatar">${avatar}</div>
        <div class="bubble"><strong>${sender}:</strong> <br/>${formattedText}</div>
    `;
    chatWindow.appendChild(messageDiv);
    chatWindow.scrollTop = chatWindow.scrollHeight;
}

// ─── Job Status Polling ───────────────────────────────────────────────────────
let pollingInterval = null;

function startStatusPolling(jobId) {
    const progressContainer = document.getElementById('progress-container');
    const progressBar = document.getElementById('progress-bar-fill');
    const progressText = document.getElementById('progress-text');
    const runClipBtn = document.getElementById('run-clip-farm-btn');

    progressContainer.classList.remove('hidden');
    progressBar.style.width = '10%';
    progressText.textContent = 'Initializing AI Brainstorming...';
    if (pollingInterval) clearInterval(pollingInterval);

    pollingInterval = setInterval(async () => {
        try {
            const res = await fetch('/api/v1/job-status/' + jobId);
            const data = await res.json();

            if (data.status !== 'idle' && data.status !== 'queued') {
                progressBar.style.width = data.progress + '%';
                progressText.textContent = data.message;
            }

            if (data.progress >= 100 || data.status === 'complete' || data.status === 'error') {
                clearInterval(pollingInterval);
                runClipBtn.disabled = false;
                runClipBtn.textContent = '▶️ Find & Post Clip Now';
                setTimeout(() => progressContainer.classList.add('hidden'), 4000);

                if (data.url) {
                    addMessage('Director AI', `🎬 Your video is live!<br><a href="${data.url}" target="_blank" style="color:#60a5fa;">Watch on YouTube ↗</a>`, 'ai-message');
                } else if (data.status === 'error') {
                    addMessage('Director AI', `⚠️ Error: ${data.message}`, 'ai-message');
                }
            }
        } catch (e) {
            console.error(e);
            clearInterval(pollingInterval);
            runClipBtn.disabled = false;
            runClipBtn.textContent = '▶️ Find & Post Clip Now';
        }
    }, 1500);
}

// ─── Run Clip Button ──────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    const runClipBtn = document.getElementById('run-clip-farm-btn');

    runClipBtn.addEventListener('click', async () => {
        const isYouTubeConnected = localStorage.getItem('youtube_connected') === 'true';
        if (!isYouTubeConnected) {
            addMessage('Director AI', '⚠️ Please connect your YouTube account first using the button on the left!', 'ai-message');
            return;
        }

        const niche = document.getElementById('niche-input').value.trim() || 'motivation';
        runClipBtn.disabled = true;
        runClipBtn.textContent = '⏳ Running...';

        try {
            const res = await fetch('/api/v1/generate-clip', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ niche })
            });

            if (res.status === 402) {
                document.getElementById('paywall-modal').classList.remove('hidden');
                runClipBtn.textContent = '▶️ Find & Post Clip Now';
                runClipBtn.disabled = false;
                return;
            }

            const data = await res.json();
            if (data.job_id) {
                addMessage('Director AI', `🔍 Starting pipeline for **${niche}**.\n\nWatch the progress bar above!`, 'ai-message');
                startStatusPolling(data.job_id);
            } else {
                runClipBtn.disabled = false;
                runClipBtn.textContent = '▶️ Find & Post Clip Now';
            }
        } catch (e) {
            addMessage('Director AI', '❌ Connection error. Please try again.', 'ai-message');
            runClipBtn.disabled = false;
            runClipBtn.textContent = '▶️ Find & Post Clip Now';
        }
    });
});

// ─── Paywall Modal ────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('checkout-btn').addEventListener('click', async () => {
        const res = await fetch('/api/v1/create-checkout-session', { method: 'POST' });
        const data = await res.json();
        if (data.checkout_url) window.location.href = data.checkout_url;
    });

    document.getElementById('close-modal').addEventListener('click', () => {
        document.getElementById('paywall-modal').classList.add('hidden');
    });
});
