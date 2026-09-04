// ─── Worker Connection State ──────────────────────────────────────────────────
let workerIsAlive = false;

function startWorkerURI() {
    const userId = document.cookie.split('; ').find(r => r.startsWith('user_id='))?.split('=')[1] || 'demo_user_123';
    window.location.href = `clipai://start?user_id=${userId}`;
    showToast('Starting local worker...', 'info');
}

// ─── In-App Video Player ───────────────────────────────────────────────────────
function openPlayer(videoId, youtubeUrl, title) {
    const modal = document.getElementById('player-modal');
    const iframe = document.getElementById('player-iframe');
    const titleEl = document.getElementById('player-title');
    const linkEl = document.getElementById('player-yt-link');
    if (!modal || !iframe) return;

    // Build embed src — autoplay=1 kicks off the video immediately
    if (videoId) {
        iframe.src = `https://www.youtube.com/embed/${videoId}?autoplay=1&rel=0&modestbranding=1`;
    } else {
        iframe.src = '';
    }
    if (titleEl) titleEl.textContent = title || '';
    if (linkEl) { linkEl.href = youtubeUrl || '#'; }
    modal.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
}

function closePlayer() {
    const modal = document.getElementById('player-modal');
    const iframe = document.getElementById('player-iframe');
    if (iframe) iframe.src = ''; // stop video playback
    if (modal) modal.classList.add('hidden');
    document.body.style.overflow = '';
}

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

// ─── Pipeline Step Indicator ──────────────────────────────────────────────────
function updatePipelineSteps(pct) {
    // Steps: search (10%), download (25%), cut (60%), upload (85%)
    const steps = [
        { id: 'step-search',   threshold: 10 },
        { id: 'step-download', threshold: 25 },
        { id: 'step-cut',      threshold: 60 },
        { id: 'step-upload',   threshold: 85 },
    ];
    steps.forEach((step, i) => {
        const el = document.getElementById(step.id);
        if (!el) return;
        const nextThreshold = steps[i + 1]?.threshold ?? 101;
        if (pct >= nextThreshold) {
            el.className = 'pipeline-step done';
        } else if (pct >= step.threshold) {
            el.className = 'pipeline-step active';
        } else {
            el.className = 'pipeline-step';
        }
    });
}

function resetPipelineSteps() {
    ['step-search','step-download','step-cut','step-upload'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.className = 'pipeline-step';
    });
}



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
    // Hide all tab content panels
    document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
    // Deactivate all sidebar and mobile navigation buttons
    document.querySelectorAll('.sidebar-btn, .nav-tab, .mobile-nav-btn').forEach(el => el.classList.remove('active'));
    // Show selected tab content
    const content = document.getElementById('tab-content-' + tab);
    if (content) content.classList.remove('hidden');
    // Activate the correct buttons
    const btn = document.getElementById('tab-' + tab);
    if (btn) btn.classList.add('active');
    const mBtn = document.getElementById('m-tab-' + tab);
    if (mBtn) mBtn.classList.add('active');
    if (tab === 'analytics') loadAnalytics();
    if (tab === 'autopost') loadAutoPostSettings();
}

// ─── Brand Kit ────────────────────────────────────────────────────────────────
function saveBrandKit() {
    const handle = document.getElementById('brand-handle')?.value.trim() || '';
    const font   = document.getElementById('brand-font')?.value || 'Hormozi';
    localStorage.setItem('clipai_handle', handle);
    localStorage.setItem('clipai_font',   font);
    document.getElementById('brand-modal').classList.add('hidden');
    showToast('Brand Kit saved!');
}

function loadBrandKit() {
    const handle = localStorage.getItem('clipai_handle') || '';
    const font   = localStorage.getItem('clipai_font')   || 'Hormozi';
    const handleEl = document.getElementById('brand-handle');
    const fontEl   = document.getElementById('brand-font');
    if (handleEl) handleEl.value = handle;
    if (fontEl)   fontEl.value   = font;
}

// ─── Dynamic Ticker ───────────────────────────────────────────────────────────
function initTicker() {
    const ticker = document.getElementById('dynamic-ticker');
    if (!ticker) return;
    
    const names = ['mike_h', 'viral_king', 'sarah_j', 'anon', 'user183', 'crypto_god', 'hustler99', 'clip_master', 'tt_creator', 'passive_inc'];
    const actions = ['generated a', 'auto-posted a', 'hit 50k views on a', 'hit 1M views on a', 'rendered a', 'scheduled a'];
    const niches = ['Crypto', 'Motivation', 'MrBeast', 'Finance', 'Tech', 'Podcast', 'Gaming', 'Fitness'];
    const colors = ['var(--blue-light)', 'var(--green)'];
    
    let html = '';
    // Generate 30 random items
    for (let i = 0; i < 30; i++) {
        const time = Math.floor(Math.random() * 59) + 1;
        const name = names[Math.floor(Math.random() * names.length)];
        const action = actions[Math.floor(Math.random() * actions.length)];
        const niche = niches[Math.floor(Math.random() * niches.length)];
        const color = colors[Math.floor(Math.random() * colors.length)];
        
        let timeStr = i === 0 ? 'Just now' : `${time}m ago`;
        html += `<span style="color: ${color};">● ${timeStr}:</span> <strong>${name}</strong> ${action} ${niche} clip &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;`;
    }
    // Duplicate the content so the infinite scroll is seamless
    ticker.innerHTML = html + html;
}
document.addEventListener('DOMContentLoaded', initTicker);

// ─── Analytics ────────────────────────────────────────────────────────────────
let viewsChart = null;

async function loadAnalytics() {
    try {
        const res = await fetch('/api/v1/analytics');
        const data = await res.json();

        document.getElementById('stat-total-views').textContent = formatNumber(data.total_views);
        document.getElementById('stat-total-videos').textContent = data.total_videos;
        document.getElementById('stat-avg-views').textContent = formatNumber(data.avg_views);

        const galleryGrid = document.getElementById('videos-gallery-grid');
        if (!galleryGrid) return;

        if (!data.videos || data.videos.length === 0) {
            galleryGrid.innerHTML = '<div class="table-empty" style="grid-column: 1 / -1;">No videos posted yet. Generate your first clip!</div>';
            return;
        }
        
        // Add canvas for chart dynamically
        
        // Render Chart container
        const chartContainer = document.createElement('div');
        chartContainer.style.marginBottom = '40px';
        chartContainer.style.height = '250px';
        chartContainer.style.width = '100%';
        chartContainer.innerHTML = '<canvas id="viewsChart"></canvas>';
        
        // Insert chart right before the gallery header (only if not already inserted)
        const galleryHeader = document.querySelector('.gallery-header');
        if (galleryHeader && !document.getElementById('viewsChart')) {
            galleryHeader.parentNode.insertBefore(chartContainer, galleryHeader);
        }
        
        // Render video cards
        galleryGrid.innerHTML = data.videos.map(v => {
            const rawId = v.youtube_url ? (v.youtube_url.split('shorts/')[1] || v.youtube_url.split('v=')[1] || '') : '';
            const videoId = rawId.split('?')[0];
            const thumbUrl = videoId
                ? `https://i.ytimg.com/vi/${videoId}/maxresdefault.jpg`
                : 'https://via.placeholder.com/400x700/1e293b/3b82f6?text=ClipAI';
            const viralScore = Math.floor(Math.random() * 12) + 88;
            const title = escHtml(v.title || v.niche || 'Untitled');
            
            return `
            <div class="clip-card" onclick="openPlayer('${videoId}','${v.youtube_url || ''}','${title}')">
                <div class="clip-card-thumb" style="background-image:url('${thumbUrl}')">
                    <div class="clip-card-overlay">
                        <div class="clip-play-btn">
                            <svg width="24" height="24" viewBox="0 0 24 24" fill="white"><path d="M8 5v14l11-7z"/></svg>
                        </div>
                    </div>
                    <div class="clip-score-badge">🔥 ${viralScore}</div>
                </div>
                <div class="clip-card-info">
                    <div class="clip-title">${title}</div>
                    <div class="clip-meta">
                        <span>${v.created_at ? new Date(v.created_at).toLocaleDateString() : '—'}</span>
                        <span class="clip-views">👁 ${formatNumber(v.views || 0)}</span>
                    </div>
                </div>
            </div>`;
        }).join('');
        
        // Render Chart
        if (viewsChart) {
            viewsChart.destroy();
        }
        
        // Prepare chart data (reverse to show chronological order)
        const chartVideos = [...data.videos].reverse();
        const labels = chartVideos.map(v => v.created_at ? new Date(v.created_at).toLocaleDateString() : '');
        const views = chartVideos.map(v => v.views || 0);
        
        const ctx = document.getElementById('viewsChart').getContext('2d');
        
        // Create gradient
        const gradient = ctx.createLinearGradient(0, 0, 0, 250);
        gradient.addColorStop(0, 'rgba(59, 130, 246, 0.5)'); // Blue
        gradient.addColorStop(1, 'rgba(59, 130, 246, 0.0)');
        
        viewsChart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [{
                    label: 'Views',
                    data: views,
                    borderColor: '#3b82f6',
                    backgroundColor: gradient,
                    borderWidth: 3,
                    pointBackgroundColor: '#3b82f6',
                    pointBorderColor: '#fff',
                    pointBorderWidth: 2,
                    pointRadius: 4,
                    pointHoverRadius: 6,
                    fill: true,
                    tension: 0.4
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: 'rgba(17, 24, 39, 0.9)',
                        titleColor: '#fff',
                        bodyColor: '#cbd5e1',
                        padding: 12,
                        cornerRadius: 8,
                        displayColors: false
                    }
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        grid: { color: 'rgba(255, 255, 255, 0.05)', drawBorder: false },
                        ticks: { color: '#64748b', maxTicksLimit: 5 }
                    },
                    x: {
                        grid: { display: false, drawBorder: false },
                        ticks: { color: '#64748b', maxTicksLimit: 7 }
                    }
                }
            }
        });
        
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
function showToast(message, type = 'success') {
    const container = document.getElementById('toast-container');
    if (!container) return;
    
    const t = document.createElement('div');
    t.className = `toast ${type}`;
    
    let icon = '✅';
    if (type === 'error') icon = '❌';
    if (type === 'info') icon = 'ℹ️';
    
    t.innerHTML = `
        <div class="toast-icon">${icon}</div>
        <div class="toast-content">
            <h4>${type === 'error' ? 'Error' : 'Success'}</h4>
            <p>${message}</p>
        </div>
    `;
    
    container.appendChild(t);
    
    // Animate in
    requestAnimationFrame(() => {
        t.classList.add('show');
    });
    
    // Remove after 3.5s
    setTimeout(() => {
        t.classList.remove('show');
        setTimeout(() => t.remove(), 400); // Wait for transition
    }, 3500);
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

    // Guard: all elements must exist before starting
    if (!progressContainer || !progressFill || !progressText || !runBtn) {
        console.warn('startStatusPolling: required DOM elements missing, aborting poll.');
        return;
    }

    progressContainer.classList.remove('hidden');
    progressFill.style.width = '5%';
    if (pollingInterval) clearInterval(pollingInterval);

    const BTN_ICON = `<svg width="18" height="18" viewBox="0 0 15 15" fill="none"><path d="M3 1.5L13.5 7.5L3 13.5V1.5Z" fill="currentColor"/></svg>`;

    pollingInterval = setInterval(async () => {
        try {
            const res = await fetch('/api/v1/job-status/' + jobId);
            const data = await res.json();

            if (data.status !== 'idle' && data.status !== 'queued') {
                const pct = Math.max(5, data.progress);
                progressFill.style.width = pct + '%';
                if (progressPct) progressPct.textContent = pct + '%';
                progressText.textContent = data.message;
                
                // Show Virality Score once the search/AI analysis is complete (around 50%)
                const viralityBadge = document.getElementById('virality-badge');
                if (pct >= 50 && viralityBadge && viralityBadge.style.display === 'none') {
                    viralityBadge.style.display = 'block';
                    const score = Math.floor(Math.random() * (99 - 88 + 1)) + 88;
                    const scoreEl = document.getElementById('virality-score');
                    if (scoreEl) scoreEl.textContent = score;
                }
                
                updatePipelineSteps(pct);
            }

            if (data.progress >= 100 || data.status === 'complete' || data.status === 'error') {
                clearInterval(pollingInterval);
                localStorage.removeItem('active_job_id');
                runBtn.disabled = false;
                runBtn.innerHTML = `${BTN_ICON} Generate Clip`;
                progressFill.style.width = '100%';
                setTimeout(() => progressContainer.classList.add('hidden'), 4000);

                if (data.url) {
                    addMessage('Director AI', `Video is live! <a href="${data.url}" target="_blank">Watch on YouTube ↗</a>`);
                    showToast('Video posted to YouTube!');
                } else if (data.status === 'error') {
                    addMessage('Director AI', `Error: ${data.message}`);
                    showToast('Generation failed — see activity log', 'error');
                }
            }
        } catch (e) {
            console.error(e);
            clearInterval(pollingInterval);
            localStorage.removeItem('active_job_id');
            if (runBtn) {
                runBtn.disabled = false;
                runBtn.innerHTML = `${BTN_ICON} Generate Clip`;
            }
        }
    }, 1500);
}

// ─── On Page Load ─────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
    // Force initialize layout state
    switchTab('generate');
    
    // Load saved brand kit settings
    loadBrandKit();

    // Handle YouTube OAuth redirect
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get('youtube') === 'connected') {
        localStorage.setItem('youtube_connected', 'true');
        window.history.replaceState({}, '', window.location.pathname);
        showToast('YouTube connected successfully!');
    } else if (urlParams.get('youtube') === 'error') {
        const detail = urlParams.get('detail') || 'Unknown error';
        showToast('YouTube connection failed: ' + detail, 'error');
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
            // Only clear if genuinely done or job not found — NOT just because progress is 0
            const terminalStates = ['complete', 'error', 'idle'];
            if (!data.status || terminalStates.includes(data.status) || data.status === 'unknown') {
                localStorage.removeItem('active_job_id');
            } else {
                // Job is genuinely still running (queued, processing, running) — resume polling
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
            showToast('Connect YouTube first', 'info');
            return;
        }

        const niche = document.getElementById('niche-input').value.trim() || 'motivation';
        
        // If worker is offline, tell them to start it
        if (!workerIsAlive) {
            addMessage('Director AI', '⚠️ **Worker is offline.** Please start the ClipAI_Worker.exe app on your computer before generating a clip. If you haven\'t downloaded it yet, you can download it from the top bar.');
            showToast('Worker is offline', 'error');
            return;
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
                runBtn.innerHTML = `<svg width="18" height="18" viewBox="0 0 15 15" fill="none"><path d="M3 1.5L13.5 7.5L3 13.5V1.5Z" fill="currentColor"/></svg> Generate Clip`;
                return;
            }

            const data = await res.json();
            if (data.job_id) {
                localStorage.setItem('active_job_id', data.job_id);
                addMessage('Director AI', `Pipeline started for **${niche}**. Watch the progress bar!`);
                startStatusPolling(data.job_id);
                // Live-update the free generations badge
                if (data.free_remaining !== null && data.free_remaining !== undefined) {
                    const badge = document.getElementById('free-tier-badge');
                    const span = document.getElementById('free-remaining');
                    if (span) span.textContent = data.free_remaining;
                    if (badge && data.free_remaining === 0) {
                        badge.style.color = '#ef4444';
                        badge.innerHTML = '🔒 Free Tier — <span id="free-remaining">0</span> generations remaining';
                    }
                }
            } else {
                runBtn.disabled = false;
                runBtn.innerHTML = `<svg width="18" height="18" viewBox="0 0 15 15" fill="none"><path d="M3 1.5L13.5 7.5L3 13.5V1.5Z" fill="currentColor"/></svg> Generate Clip`;
            }
        } catch (e) {
            addMessage('Director AI', 'Connection error. Please try again.');
            runBtn.disabled = false;
            runBtn.innerHTML = `<svg width="18" height="18" viewBox="0 0 15 15" fill="none"><path d="M3 1.5L13.5 7.5L3 13.5V1.5Z" fill="currentColor"/></svg> Generate Clip`;
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

// ─── Cancel Job (global scope — called from onclick in HTML) ──────────────────
function cancelJob() {
    // Clear the stored job so the UI stops polling
    localStorage.removeItem('active_job_id');
    // Hide the progress card and virality badge
    const progressContainerEl = document.getElementById('progress-container');
    if (progressContainerEl) progressContainerEl.classList.add('hidden');
    const viralityBadge = document.getElementById('virality-badge');
    if (viralityBadge) viralityBadge.style.display = 'none';
    
    // Re-enable the generate button
    const runBtn = document.getElementById('run-clip-farm-btn');
    if (runBtn) {
        runBtn.disabled = false;
        runBtn.innerHTML = `
            <svg width="18" height="18" viewBox="0 0 15 15" fill="none">
                <path d="M3 1.5L13.5 7.5L3 13.5V1.5Z" fill="currentColor"/>
            </svg>
            Generate Clip
        `;
    }
    resetPipelineSteps();
    addMessage('Director AI', 'Job cancelled. Ready to generate a new clip!');
}

// ─── Auto Post ────────────────────────────────────────────────────────────────

function addTimeInput(value = '') {
    const container = document.getElementById('times-container');
    const row = document.createElement('div');
    row.style.cssText = 'display:flex; align-items:center; gap:10px;';
    row.innerHTML = `
        <input type="time" class="time-input niche-input-lg" style="max-width:160px;" value="${value}">
        <button type="button" onclick="this.parentElement.remove()" style="background:none;border:none;color:var(--text-3);cursor:pointer;font-size:20px;line-height:1;padding:0 4px;" title="Remove">×</button>
    `;
    container.appendChild(row);
}

async function loadAutoPostSettings() {
    try {
        const res = await fetch('/api/v1/auto-post/settings');
        const data = await res.json();
        
        document.getElementById('autopost-enable').checked = data.enabled || false;
        document.getElementById('autopost-niche').value = data.niche || 'motivation';
        
        // Populate dynamic time inputs
        const container = document.getElementById('times-container');
        container.innerHTML = '';
        const times = data.times && data.times.length ? data.times : (data.time ? [data.time] : ['12:00']);
        times.forEach(t => addTimeInput(t));
        
        if (data.days && Array.isArray(data.days)) {
            document.querySelectorAll('.day-cb').forEach(cb => {
                cb.checked = data.days.includes(cb.value);
            });
        } else {
            document.querySelectorAll('.day-cb').forEach(cb => cb.checked = true);
        }
    } catch (e) {
        console.error('Failed to load auto post settings:', e);
        addTimeInput('12:00');
    }
}

async function saveAutoPostSettings() {
    const btn = document.getElementById('btn-save-autopost');
    const prevText = btn.textContent;
    btn.textContent = 'Saving...';
    btn.disabled = true;
    
    const days = Array.from(document.querySelectorAll('.day-cb'))
                      .filter(cb => cb.checked)
                      .map(cb => cb.value);
                      
    const times = Array.from(document.querySelectorAll('.time-input'))
                       .map(i => i.value)
                       .filter(v => v);
    
    try {
        const payload = {
            enabled: document.getElementById('autopost-enable').checked,
            times: times,
            niche: document.getElementById('autopost-niche').value || 'motivation',
            days: days
        };
        
        const res = await fetch('/api/v1/auto-post/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        
        if (res.ok) {
            showToast('Auto Post Schedule Saved!');
        } else {
            showToast('Failed to save settings', 'error');
        }

    } catch (e) {
        console.error('Error saving:', e);
        showToast('Error saving settings', 'error');
    } finally {
        btn.textContent = prevText;
        btn.disabled = false;
    }
}
