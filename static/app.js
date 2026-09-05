// ─── User Session & Identifier ────────────────────────────────────────────────
function getActiveUserId() {
    let uid = document.cookie.split('; ').find(r => r.startsWith('user_id='))?.split('=')[1];
    if (!uid) {
        uid = localStorage.getItem('clipai_user_id');
    }
    if (!uid) {
        uid = 'user_43065'; // Default linked desktop account
    }
    localStorage.setItem('clipai_user_id', uid);
    if (!document.cookie.split('; ').find(r => r.startsWith('user_id='))) {
        document.cookie = `user_id=${uid};path=/;max-age=31536000;SameSite=Lax`;
    }
    return uid;
}

// ─── Worker Connection State ──────────────────────────────────────────────────
let workerIsAlive = false;
let workerIsStarting = false;

async function checkWorkerHeartbeat() {
    try {
        const userId = getActiveUserId();
        const res = await fetch(`/api/v1/debug/queue?user_id=${userId}`);
        const data = await res.json();
        workerIsAlive = !!data.worker_alive;
        if (workerIsAlive) workerIsStarting = false;
        
        const dot = document.getElementById('worker-dot');
        const label = document.getElementById('worker-label');
        if (dot && label) {
            if (workerIsAlive) {
                dot.style.background = '#10b981';
                dot.style.boxShadow = '0 0 10px rgba(16,185,129,0.5)';
                label.textContent = 'Worker Active (🟢)';
                label.style.color = '#10b981';
            } else if (workerIsStarting) {
                dot.style.background = '#f59e0b';
                dot.style.boxShadow = '0 0 10px rgba(245,158,11,0.5)';
                label.textContent = 'Worker Starting... (⏳)';
                label.style.color = '#f59e0b';
            } else {
                dot.style.background = '#ef4444';
                dot.style.boxShadow = 'none';
                label.textContent = 'Worker Offline';
                label.style.color = 'var(--text-3)';
            }
        }
    } catch (e) {
        workerIsAlive = false;
    }
}
setInterval(checkWorkerHeartbeat, 3000);

function startWorkerURI() {
    const userId = getActiveUserId();
    workerIsStarting = true;
    
    const dot = document.getElementById('worker-dot');
    const label = document.getElementById('worker-label');
    if (dot && label) {
        dot.style.background = '#f59e0b';
        dot.style.boxShadow = '0 0 10px rgba(245,158,11,0.5)';
        label.textContent = 'Worker Starting... (⏳)';
        label.style.color = '#f59e0b';
    }
    
    window.location.href = `clipai://start?user_id=${userId}`;
    showToast('Starting local desktop worker...', 'info');
    setTimeout(checkWorkerHeartbeat, 2000);
    setTimeout(checkWorkerHeartbeat, 5000);
}

// ─── In-App Video Player ───────────────────────────────────────────────────────
function openPlayer(videoId, youtubeUrl, title) {
    const modal = document.getElementById('player-modal');
    const iframe = document.getElementById('player-iframe');
    const video = document.getElementById('player-video');
    const titleEl = document.getElementById('player-title');
    const linkEl = document.getElementById('player-yt-link');
    if (!modal) return;

    currentVideoUrl = youtubeUrl || '';
    if (titleEl) titleEl.textContent = title || 'Viral Short';

    if (linkEl) {
        if (youtubeUrl && (youtubeUrl.includes('youtube.com') || youtubeUrl.includes('youtu.be'))) {
            linkEl.href = youtubeUrl;
            linkEl.style.display = 'block';
        } else {
            linkEl.style.display = 'none';
        }
    }

    // Extract genuine YouTube Video ID from any format
    let cleanYtId = videoId || '';
    if (youtubeUrl) {
        if (youtubeUrl.includes('/shorts/')) {
            cleanYtId = youtubeUrl.split('/shorts/')[1].split('?')[0].split('&')[0];
        } else if (youtubeUrl.includes('v=')) {
            cleanYtId = youtubeUrl.split('v=')[1].split('&')[0];
        } else if (youtubeUrl.includes('youtu.be/')) {
            cleanYtId = youtubeUrl.split('youtu.be/')[1].split('?')[0];
        }
    }

    const emptyNotice = document.getElementById('player-empty');

    // If it's a local file path rendered by desktop worker (e.g. C:\Users\...\\.clipai\\generated_videos\\clip_xyz.mp4)
    let playableStreamUrl = youtubeUrl || '';
    if (playableStreamUrl && (playableStreamUrl.includes('.mp4') || playableStreamUrl.includes('.clipai'))) {
        const filename = playableStreamUrl.split(/[/\\]/).pop();
        if (filename) {
            playableStreamUrl = `http://127.0.0.1:58921/${encodeURIComponent(filename)}`;
        }
    }

    if (cleanYtId && cleanYtId !== 'TEST_ANALYTICS' && (cleanYtId.length === 11 || (youtubeUrl && (youtubeUrl.includes('youtube.com') || youtubeUrl.includes('youtu.be'))))) {
        if (emptyNotice) emptyNotice.style.display = 'none';
        if (iframe) {
            iframe.style.display = 'block';
            iframe.src = `https://www.youtube.com/embed/${cleanYtId}?autoplay=1&rel=0`;
        }
        if (video) {
            video.style.display = 'none';
            video.pause();
            video.src = '';
        }
    } else if (playableStreamUrl && (playableStreamUrl.startsWith('http') || playableStreamUrl.startsWith('/') || playableStreamUrl.startsWith('blob:'))) {
        if (emptyNotice) emptyNotice.style.display = 'none';
        if (iframe) {
            iframe.style.display = 'none';
            iframe.src = '';
        }
        if (video) {
            video.style.display = 'block';
            video.src = playableStreamUrl;
            video.play().catch(() => {});
        }
    } else {
        // No playable streaming URL available yet
        if (iframe) {
            iframe.style.display = 'none';
            iframe.src = '';
        }
        if (video) {
            video.style.display = 'none';
            video.pause();
            video.src = '';
        }
        if (emptyNotice) emptyNotice.style.display = 'flex';
    }

    modal.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
}

function closePlayer() {
    const modal = document.getElementById('player-modal');
    const iframe = document.getElementById('player-iframe');
    const video = document.getElementById('player-video');
    if (iframe) iframe.src = '';
    if (video) {
        video.pause();
        video.src = '';
    }
    if (modal) modal.classList.add('hidden');
    document.body.style.overflow = '';
}

function copyVideoLink() {
    if (!currentVideoUrl) {
        showToast('No URL available to copy', 'error');
        return;
    }
    navigator.clipboard.writeText(currentVideoUrl).then(() => {
        showToast('Video link copied to clipboard!');
    }).catch(() => {
        showToast('Could not copy link', 'error');
    });
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
        btn.innerHTML = `
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
          <span>Connected</span>
        `;
        btn.className = 'btn btn-connect connected';
        btn.title = 'Connected to YouTube. Click to switch accounts or reconnect.';
    } else {
        dot.className = 'status-dot';
        label.textContent = 'Not Connected';
        btn.innerHTML = `
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M23.5 6.2a3 3 0 0 0-2.1-2.1C19.6 3.6 12 3.6 12 3.6s-7.6 0-9.4.5A3 3 0 0 0 .5 6.2 31.5 31.5 0 0 0 0 12a31.5 31.5 0 0 0 .5 5.8 3 3 0 0 0 2.1 2.1c1.8.5 9.4.5 9.4.5s7.6 0 9.4-.5a3 3 0 0 0 2.1-2.1A31.5 31.5 0 0 0 24 12a31.5 31.5 0 0 0-.5-5.8zM9.8 15.6V8.4l6.3 3.6-6.3 3.6z"/></svg>
          <span>Connect YouTube</span>
        `;
        btn.className = 'btn btn-connect';
        btn.title = 'Click to connect your YouTube channel';
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
    if (tab === 'workplace') loadWorkplaceClips();
    if (tab === 'autopost') loadAutoPostSettings();
}

// ─── Workplace (Review Before Post) ───────────────────────────────────────────
async function loadWorkplaceClips() {
    const container = document.getElementById('workplace-clips-container');
    if (!container) return;
    try {
        const userId = getActiveUserId();
        const res = await fetch(`/api/v1/analytics?user_id=${userId}`);
        const data = await res.json();
        const allClips = data.videos || [];
        // Workplace shows unposted drafts waiting for review (clips without a live YouTube link)
        const workplaceClips = allClips.filter(c => !c.youtube_url || (!c.youtube_url.includes('youtube.com') && !c.youtube_url.includes('youtu.be')));
        
        if (workplaceClips.length === 0) {
            container.innerHTML = '<div class="table-empty" style="grid-column: 1 / -1; padding: 48px 24px;">No draft clips waiting for review. Generate a new clip with auto-post turned off!</div>';
            return;
        }

        container.innerHTML = workplaceClips.map(c => {
            const rawId = c.youtube_url ? (c.youtube_url.split('shorts/')[1] || c.youtube_url.split('v=')[1] || '') : '';
            const videoId = rawId.split('?')[0];
            const isLive = Boolean(c.youtube_url);
            const thumbUrl = videoId
                ? `https://i.ytimg.com/vi/${videoId}/maxresdefault.jpg`
                : 'https://via.placeholder.com/400x700/18181b/3b82f6?text=Pending+Review';
            const title = escHtml(c.title || c.niche || 'Viral Short');
            
            const rawUrl = (c.youtube_url || '').replace(/\\/g, '/');
            const safeUrl = encodeURI(rawUrl);
            const rawFilename = rawUrl.split('/').pop() || '';
            const streamUrl = rawFilename ? `http://127.0.0.1:58921/${encodeURIComponent(rawFilename)}` : '';
            const mediaPreview = isLive && thumbUrl
                ? `<img src="${thumbUrl}" style="width:100%; height:100%; object-fit:cover; opacity:0.85; transition:opacity 0.2s;" onmouseover="this.style.opacity='1'" onmouseout="this.style.opacity='0.85'">`
                : `<video src="${streamUrl}" preload="metadata" muted playsinline style="width:100%; height:100%; object-fit:cover; opacity:0.9;"></video>`;

            return `
            <div class="glass-card" style="display:flex; flex-direction:column; overflow:hidden; border-radius:14px; border:1px solid rgba(255,255,255,0.08); background:rgba(20,20,20,0.6);">
                <div style="position:relative; aspect-ratio:9/16; background:#000; overflow:hidden; cursor:pointer;" onclick="openPlayer('${videoId}','${playUrl}','${title}')">
                    ${mediaPreview}
                    <div style="position:absolute; inset:0; display:flex; align-items:center; justify-content:center; background:rgba(0,0,0,0.25);">
                        <div style="width:48px; height:48px; border-radius:50%; background:rgba(220,38,38,0.9); display:flex; align-items:center; justify-content:center; box-shadow:0 4px 20px rgba(0,0,0,0.5);">
                            <svg width="20" height="20" viewBox="0 0 24 24" fill="white"><path d="M8 5v14l11-7z"/></svg>
                        </div>
                    </div>
                    <div style="position:absolute; top:12px; right:12px; background:rgba(234,179,8,0.85); color:white; font-size:11px; font-weight:700; padding:4px 8px; border-radius:6px; text-transform:uppercase;">
                        ⏳ Ready to Review
                    </div>
                </div>
                <div style="padding:16px; display:flex; flex-direction:column; gap:10px; flex:1; justify-content:space-between;">
                    <div>
                        <div style="font-weight:700; font-size:14px; color:#fff; line-height:1.4; margin-bottom:4px;">${title}</div>
                        <div style="font-size:12px; color:var(--text-3);">${c.created_at ? new Date(c.created_at).toLocaleDateString() : 'Recent'}</div>
                    </div>
                    <div style="display:flex; gap:8px;">
                        <button onclick="openPlayer('${videoId}','${playUrl}','${title}')" class="btn btn-outline" style="flex:1; justify-content:center; padding:8px; font-size:12px;">Watch</button>
                        <button onclick="publishClipToYouTube('${c.id}')" class="btn btn-generate" style="flex:1.4; justify-content:center; padding:8px; font-size:12px; background:#dc2626;">Post to YouTube</button>
                        <button onclick="deleteClip('${c.id}')" class="btn btn-outline" title="Delete Clip" style="padding:8px 10px; font-size:12px; color:#ef4444; border-color:rgba(239,68,68,0.25);">🗑</button>
                    </div>
                </div>
            </div>`;
        }).join('');

    } catch (e) {
        console.error('Workplace load error:', e);
    }
}

async function deleteClip(clipId) {
    if (!confirm('Are you sure you want to delete this clip?')) return;
    try {
        const res = await fetch(`/api/v1/clip/${clipId}`, { method: 'DELETE' });
        if (res.ok) {
            showToast('Clip deleted');
            loadWorkplaceClips();
            loadAnalytics();
        } else {
            showToast('Could not delete clip', 'error');
        }
    } catch (e) {
        showToast('Delete request failed', 'error');
    }
}

async function publishClipToYouTube(clipId) {
    try {
        const res = await fetch('/api/v1/clip/publish-draft', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ clip_id: clipId })
        });
        if (res.ok) {
            showToast('Video published to YouTube!');
            loadWorkplaceClips();
        } else {
            showToast('Publishing failed. Check YouTube connection.', 'error');
        }
    } catch (e) {
        showToast('Network error while publishing', 'error');
    }
}

// ─── Subscriptions Modal ──────────────────────────────────────────────────────
function openSubscriptionsModal() {
    const modal = document.getElementById('subscriptions-modal');
    if (modal) modal.classList.remove('hidden');
}

function closeSubscriptionsModal() {
    const modal = document.getElementById('subscriptions-modal');
    if (modal) modal.classList.add('hidden');
}

async function checkoutPlan(tier) {
    showToast(`Redirecting to ${tier} checkout...`, 'info');
    try {
        const res = await fetch('/api/v1/create-checkout-session', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tier: tier })
        });
        const data = await res.json();
        if (data.checkout_url) {
            window.location.href = data.checkout_url;
        } else {
            showToast('Could not initialize checkout', 'error');
        }
    } catch (e) {
        showToast('Checkout connection error', 'error');
    }
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
        const userId = getActiveUserId();
        const res = await fetch(`/api/v1/analytics?user_id=${userId}`);
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
        
        // Render video cards (only live published clips appear in My Clips)
        const publishedClips = (data.videos || []).filter(v => v.youtube_url && (v.youtube_url.includes('youtube.com') || v.youtube_url.includes('youtu.be')));
        if (publishedClips.length === 0) {
            galleryGrid.innerHTML = '<div class="table-empty" style="grid-column: 1 / -1;">No live YouTube clips yet. Review your drafts in Workplace to publish them!</div>';
            return;
        }
        galleryGrid.innerHTML = publishedClips.map(v => {
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

            if (data.progress >= 100 || data.status === 'complete' || data.status === 'draft_ready' || data.status === 'error') {
                clearInterval(pollingInterval);
                localStorage.removeItem('active_job_id');
                runBtn.disabled = false;
                runBtn.innerHTML = `${BTN_ICON} Generate Clip`;
                progressFill.style.width = '100%';
                setTimeout(() => progressContainer.classList.add('hidden'), 4000);

                if (data.status === 'draft_ready' || (data.message && data.message.includes('Workplace'))) {
                    addMessage('Director AI', `🎬 **Video rendered!** It is waiting in your <a href="javascript:void(0)" onclick="switchTab('workplace')">Workplace</a> for review before posting.`);
                    showToast('Video saved to Workplace for review!');
                    loadWorkplaceClips();
                } else if (data.url && (data.url.includes('youtube.com') || data.url.includes('youtu.be'))) {
                    addMessage('Director AI', `Video is live! <a href="${data.url}" target="_blank">Watch on YouTube ↗</a>`);
                    showToast('Video posted to YouTube!');
                    loadAnalytics();
                } else if (data.status === 'error') {
                    addMessage('Director AI', `Error: ${data.message}`);
                    showToast('Generation failed — see activity log', 'error');
                } else {
                    addMessage('Director AI', `🎬 Video ready! Check your <a href="javascript:void(0)" onclick="switchTab('workplace')">Workplace</a>.`);
                    showToast('Video ready in Workplace!');
                    loadWorkplaceClips();
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

// ─── Account Modal & Profile Management ─────────────────────────────────────
async function openAccountModal() {
    const modal = document.getElementById('account-modal');
    if (!modal) return;
    try {
        const res = await fetch('/api/v1/user/profile');
        const data = await res.json();
        const emailInput = document.getElementById('account-email-input');
        const planBadge = document.getElementById('account-plan-badge');
        const userIdSpan = document.getElementById('account-user-id');
        if (emailInput) emailInput.value = data.email || '';
        if (planBadge) planBadge.textContent = (data.license || 'free_tier').toUpperCase();
        if (userIdSpan) userIdSpan.textContent = data.user_id || '';
    } catch (e) {
        console.error('Failed to load profile:', e);
    }
    modal.classList.remove('hidden');
}

function closeAccountModal() {
    const modal = document.getElementById('account-modal');
    if (modal) modal.classList.add('hidden');
}

async function saveAccountEmail() {
    const email = document.getElementById('account-email-input')?.value.trim();
    if (!email || !email.includes('@')) {
        showToast('Please enter a valid email address', 'error');
        return;
    }
    try {
        const res = await fetch('/api/v1/user/profile', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email: email })
        });
        const data = await res.json();
        if (res.ok) {
            if (data.user_id) {
                localStorage.setItem('clipai_user_id', data.user_id);
                document.cookie = `user_id=${data.user_id};path=/;max-age=31536000;SameSite=Lax`;
            }
            showToast('Account profile linked successfully!');
            const userLabel = document.getElementById('user-display-label');
            if (userLabel) userLabel.textContent = email.split('@')[0];
            closeAccountModal();
            loadWorkplaceClips();
            loadAnalytics();
            checkWorkerHeartbeat();
        } else {
            showToast(data.detail || 'Failed to update email', 'error');
        }
    } catch (e) {
        showToast('Error saving account profile', 'error');
    }
}

// ─── On Page Load ─────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
    // Force initialize layout state
    switchTab('generate');
    
    // Load saved brand kit settings
    loadBrandKit();

    // Check existing account profile
    try {
        const res = await fetch('/api/v1/user/profile');
        const data = await res.json();
        if (data.email) {
            const userLabel = document.getElementById('user-display-label');
            if (userLabel) userLabel.textContent = data.email.split('@')[0];
        }
    } catch (e) {}

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

// ─── Niche Presets ─────────────────────────────────────────────────────────────
function selectNichePreset(nicheName, btnEl) {
    const input = document.getElementById('niche-input');
    if (input) input.value = nicheName;
    document.querySelectorAll('.niche-pill').forEach(btn => {
        btn.style.borderColor = 'rgba(255,255,255,0.12)';
        btn.style.background = 'rgba(255,255,255,0.06)';
    });
    if (btnEl) {
        btnEl.style.borderColor = 'var(--blue)';
        btnEl.style.background = 'rgba(37,99,235,0.25)';
    }
}

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
        const autoUploadToggle = document.getElementById('studio-autopost-toggle');
        const autoUpload = autoUploadToggle ? autoUploadToggle.checked : true;
        
        // If worker is offline, tell them to start it
        if (!workerIsAlive) {
            addMessage('Director AI', '⚠️ **Worker is offline.** Please start the ClipAI_Worker.exe app on your computer before generating a clip. If you haven\'t downloaded it yet, you can download it from the top bar.');
            showToast('Worker is offline', 'error');
            return;
        }

        const layout = document.getElementById('studio-layout-select')?.value || 'split_screen';
        const subtitleStyle = document.getElementById('studio-subtitle-select')?.value || 'hormozi';

        runBtn.disabled = true;
        runBtn.textContent = 'Running...';

        try {
            const res = await fetch('/api/v1/generate-clip', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    niche, 
                    auto_upload: autoUpload,
                    layout: layout,
                    subtitle_style: subtitleStyle
                })
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
    const checkoutBtn = document.getElementById('checkout-btn');
    if (checkoutBtn) {
        checkoutBtn.addEventListener('click', async () => {
            const res = await fetch('/api/v1/create-checkout-session', { method: 'POST' });
            const data = await res.json();
            if (data.checkout_url) window.location.href = data.checkout_url;
        });
    }
    const closeModalBtn = document.getElementById('close-modal');
    if (closeModalBtn) {
        closeModalBtn.addEventListener('click', () => {
            document.getElementById('paywall-modal').classList.add('hidden');
        });
    }
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
