// ─── Worker Connection State ──────────────────────────────────────────────────
let workerIsAlive = false;

function updateWorkerUI(alive) {
    workerIsAlive = alive;
    const dot = document.getElementById('worker-dot');
    const label = document.getElementById('worker-label');
    if (!dot || !label) return;
    if (alive) {
        dot.className = 'status-dot connected';
        label.textContent = 'Worker Active';
    } else {
        dot.className = 'status-dot';
        label.textContent = 'Worker Offline';
    }
}

async function checkWorkerHeartbeat() {
    try {
        const userId = document.cookie.split('; ').find(r => r.startsWith('user_id='))?.split('=')[1] || 'demo_user_123';
        const res = await fetch(`/api/v1/worker/heartbeat?user_id=${userId}`);
        const data = await res.json();
        updateWorkerUI(data.alive);
    } catch (e) {
        updateWorkerUI(false);
    }
}
setInterval(checkWorkerHeartbeat, 5000);

// ─── YouTube Connection State ─────────────────────────────────────────────────
function updateYouTubeUI(connected) {
    const dot = document.getElementById('yt-dot');
    const label = document.getElementById('yt-label');
    const btn = document.getElementById('connect-youtube-btn');
    if (!dot || !label || !btn) return;
    if (connected) {
        dot.className = 'status-dot connected';
        label.textContent = 'YouTube Connected';
        btn.textContent = '✓ Connected';
        btn.className = 'btn btn-connect connected';
    } else {
        dot.className = 'status-dot';
        label.textContent = 'Not Connected';
        btn.textContent = 'Connect YouTube';
        btn.className = 'btn btn-connect';
    }
}

// ─── Tab Switching ────────────────────────────────────────────────────────────
function switchTab(tab) {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
    document.querySelectorAll('.nav-tab').forEach(el => el.classList.remove('active'));
    document.getElementById('tab-content-' + tab).classList.remove('hidden');
    document.getElementById('tab-' + tab).classList.add('active');
    if (tab === 'analytics') loadAnalytics();
}

// ─── Analytics ────────────────────────────────────────────────────────────────
async function loadAnalytics() {
    try {
        const res = await fetch('/api/v1/analytics');
        const data = await res.json();

        document.getElementById('stat-total-views').textContent = formatNumber(data.total_views);
        document.getElementById('stat-total-videos').textContent = data.total_videos;
        document.getElementById('stat-avg-views').textContent = formatNumber(data.avg_views);

        const body = document.getElementById('videos-table-body');
        if (!data.videos || data.videos.length === 0) {
            body.innerHTML = '<div class="table-empty">No videos posted yet. Generate your first clip!</div>';
            return;
        }

        body.innerHTML = `
            <div class="table-row-header">
                <span>Title</span><span>Views</span><span>Posted</span><span>Link</span>
            </div>
            ${data.videos.map(v => `
            <div class="table-row">
                <div class="video-title">${escHtml(v.title || v.niche || 'Untitled')}</div>
                <div class="video-views">${formatNumber(v.views || 0)}</div>
                <div class="video-date">${v.created_at ? new Date(v.created_at).toLocaleDateString() : '—'}</div>
                <div class="video-link">${v.youtube_url ? `<a href="${v.youtube_url}" target="_blank">Watch ↗</a>` : '—'}</div>
            </div>`).join('')}
        `;
    } catch (e) {
        console.error('Analytics load error:', e);
    }
}

function formatNumber(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return String(n || 0);
}

function escHtml(str) {
    return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ─── Toast ────────────────────────────────────────────────────────────────────
function showToast(message, color = '#10b981') {
    const t = document.createElement('div');
    t.style.cssText = `position:fixed;top:20px;left:50%;transform:translateX(-50%);background:${color};color:#fff;padding:12px 24px;border-radius:10px;font-weight:600;font-size:14px;box-shadow:0 4px 20px rgba(0,0,0,0.4);z-index:9999;animation:toastIn 0.3s ease;font-family:Inter,sans-serif;`;
    t.textContent = message;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 3500);
}

// ─── Add Log Entry ────────────────────────────────────────────────────────────
function addMessage(sender, text) {
    const chatWindow = document.getElementById('chat-window');
    if (!chatWindow) return;
    const el = document.createElement('div');
    el.className = 'log-entry';
    const icon = sender === 'You' ? '👤' : '🤖';
    const formatted = text.replace(/\n/g,'<br>').replace(/\*\*(.*?)\*\*/g,'<strong>$1</strong>');
    el.innerHTML = `
        <div class="log-icon">${icon}</div>
        <div class="log-body">
            <div class="log-sender">${sender}</div>
            <div class="log-text">${formatted}</div>
        </div>`;
    chatWindow.appendChild(el);
    chatWindow.scrollTop = chatWindow.scrollHeight;
}

// ─── Job Polling ──────────────────────────────────────────────────────────────
let pollingInterval = null;

function startStatusPolling(jobId) {
    const progressContainer = document.getElementById('progress-container');
    const progressFill = document.getElementById('progress-bar-fill');
    const progressText = document.getElementById('progress-text');
    const progressPct = document.getElementById('progress-pct');
    const runBtn = document.getElementById('run-clip-farm-btn');

    progressContainer.classList.remove('hidden');
    progressFill.style.width = '5%';
    if (pollingInterval) clearInterval(pollingInterval);

    pollingInterval = setInterval(async () => {
        try {
            const res = await fetch('/api/v1/job-status/' + jobId);
            const data = await res.json();

            if (data.status !== 'idle' && data.status !== 'queued') {
                const pct = Math.max(5, data.progress);
                progressFill.style.width = pct + '%';
                if (progressPct) progressPct.textContent = pct + '%';
                progressText.textContent = data.message;
            }

            if (data.progress >= 100 || data.status === 'complete' || data.status === 'error') {
                clearInterval(pollingInterval);
                localStorage.removeItem('active_job_id');
                runBtn.disabled = false;
                runBtn.innerHTML = `
                    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                        <path d="M3 2.5L13.5 8L3 13.5V2.5Z" fill="currentColor"/>
                    </svg>
                    Find & Post Clip Now
                `;
                progressFill.style.width = '0%';
                setTimeout(() => progressContainer.classList.add('hidden'), 4000);

                if (data.url) {
                    addMessage('Director AI', `Video is live! <a href="${data.url}" target="_blank">Watch on YouTube ↗</a>`);
                    showToast('Video posted to YouTube!', '#10b981');
                } else if (data.status === 'error') {
                    addMessage('Director AI', `Error: ${data.message}`);
                    showToast('Generation failed — see activity log', '#ef4444');
                }
            }
        } catch (e) {
            console.error(e);
            clearInterval(pollingInterval);
            localStorage.removeItem('active_job_id');
            runBtn.disabled = false;
            runBtn.innerHTML = `
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                    <path d="M3 2.5L13.5 8L3 13.5V2.5Z" fill="currentColor"/>
                </svg>
                Find & Post Clip Now
            `;
        }
    }, 1500);
}

// ─── On Page Load ─────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
    // Handle YouTube OAuth redirect
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get('youtube') === 'connected') {
        localStorage.setItem('youtube_connected', 'true');
        window.history.replaceState({}, '', window.location.pathname);
        showToast('YouTube connected successfully!');
    } else if (urlParams.get('youtube') === 'error') {
        const detail = urlParams.get('detail') || 'Unknown error';
        showToast('YouTube connection failed: ' + detail, '#ef4444');
        window.history.replaceState({}, '', window.location.pathname);
    }

    // Check YouTube status from server
    try {
        const res = await fetch('/api/v1/auth/youtube/status');
        const data = await res.json();
        if (data.connected) localStorage.setItem('youtube_connected', 'true');
        updateYouTubeUI(data.connected);
    } catch (e) {
        updateYouTubeUI(localStorage.getItem('youtube_connected') === 'true');
    }

    // Set user cookie
    if (!document.cookie.split('; ').find(r => r.startsWith('user_id='))) {
        document.cookie = `user_id=user_${Math.floor(Math.random()*100000)};path=/;max-age=31536000`;
    }

    // Resume polling if a job was running before page refresh
    const activeJobId = localStorage.getItem('active_job_id');
    if (activeJobId) {
        // First check if the job is actually still active on the server
        try {
            const res = await fetch('/api/v1/job-status/' + activeJobId);
            const data = await res.json();
            // If the job is complete, errored, or stuck at 0% (initializing) — clear it, don't resume
            if (data.status === 'complete' || data.status === 'error' || data.progress === 0 || !data.status || data.status === 'idle') {
                localStorage.removeItem('active_job_id');
            } else {
                // Job is genuinely still running — resume polling
                const runBtn = document.getElementById('run-clip-farm-btn');
                runBtn.disabled = true;
                runBtn.textContent = 'Running...';
                startStatusPolling(activeJobId);
            }
        } catch (e) {
            // Can't reach server — clear the job to avoid infinite stuck state
            localStorage.removeItem('active_job_id');
        }
    }
});

// ─── Generate Button ──────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    checkWorkerHeartbeat(); // initial check

    const runBtn = document.getElementById('run-clip-farm-btn');

    runBtn.addEventListener('click', async () => {
        const isConnected = localStorage.getItem('youtube_connected') === 'true';
        if (!isConnected) {
            addMessage('Director AI', 'Please connect your YouTube account first using the **Connect YouTube** button in the top right.');
            showToast('Connect YouTube first', '#f59e0b');
            return;
        }

        const niche = document.getElementById('niche-input').value.trim() || 'motivation';
        
        // If worker is offline, wake it up but DON'T submit yet — let them click again once it's active
        if (!workerIsAlive) {
            const userId = document.cookie.split('; ').find(r => r.startsWith('user_id='))?.split('=')[1] || 'demo_user_123';
            window.location.href = `clipai://start?user_id=${userId}`;
            // Show the modal in case they haven't downloaded the app yet
            document.getElementById('worker-modal').classList.remove('hidden');
            addMessage('Director AI', 'Worker is offline. Starting it now... Once the **🟢 Worker Active** badge appears, click the button again to generate your clip!');
            return; // Don't submit the job yet — wait for worker to come online
        }

        runBtn.disabled = true;
        runBtn.textContent = 'Running...';

        try {
            const res = await fetch('/api/v1/generate-clip', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ niche })
            });

            if (res.status === 402) {
                document.getElementById('paywall-modal').classList.remove('hidden');
                runBtn.disabled = false;
                runBtn.innerHTML = `
                    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                        <path d="M3 2.5L13.5 8L3 13.5V2.5Z" fill="currentColor"/>
                    </svg>
                    Find & Post Clip Now
                `;
                return;
            }

            const data = await res.json();
            if (data.job_id) {
                localStorage.setItem('active_job_id', data.job_id);
                addMessage('Director AI', `Pipeline started for **${niche}**. Watch the progress bar!`);
                startStatusPolling(data.job_id);
            } else {
                runBtn.disabled = false;
                runBtn.innerHTML = `
                    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                        <path d="M3 2.5L13.5 8L3 13.5V2.5Z" fill="currentColor"/>
                    </svg>
                    Find & Post Clip Now
                `;
            }
        } catch (e) {
            addMessage('Director AI', 'Connection error. Please try again.');
            runBtn.disabled = false;
            runBtn.innerHTML = `
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                    <path d="M3 2.5L13.5 8L3 13.5V2.5Z" fill="currentColor"/>
                </svg>
                Find & Post Clip Now
            `;
        }
    });

    // Paywall modal buttons
    document.getElementById('checkout-btn').addEventListener('click', async () => {
        const res = await fetch('/api/v1/create-checkout-session', { method: 'POST' });
        const data = await res.json();
        if (data.checkout_url) window.location.href = data.checkout_url;
    });
    document.getElementById('close-modal').addEventListener('click', () => {
        document.getElementById('paywall-modal').classList.add('hidden');
    });
});
