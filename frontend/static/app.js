/* ==========================================================================
   ClipAI v3 — Console UI Application Controller
   Matches the NLE editor layout in index.html & style.css.
   ========================================================================== */

let currentUser = null;
let pollTimer = null;

// ─── Toast Notifications ──────────────────────────────────────────────────
function showToast(message, type = 'live') {
  const container = document.getElementById('toasts');
  if (!container) return;
  const t = document.createElement('div');
  t.className = `toast ${type === 'error' ? 'error' : ''}`;
  t.textContent = message;
  container.appendChild(t);
  requestAnimationFrame(() => t.classList.add('show'));
  setTimeout(() => {
    t.classList.remove('show');
    setTimeout(() => t.remove(), 320);
  }, 3500);
}

// ─── View Switching ───────────────────────────────────────────────────────
function switchView(viewName) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('.rail-btn, .bottom-nav button').forEach(b => {
    if (b.dataset.view === viewName) {
      b.classList.add('active');
    } else {
      b.classList.remove('active');
    }
  });
  const target = document.getElementById(`view-${viewName}`);
  if (target) target.classList.add('active');

  if (viewName === 'workplace') loadWorkplace();
  if (viewName === 'clips') loadClips();
  if (viewName === 'autopost') loadAutoPost();
}
window.switchView = switchView;

// ─── Modal Management ─────────────────────────────────────────────────────
function openModal(id) {
  const m = document.getElementById(id);
  if (m) m.classList.remove('hidden');
}
window.openModal = openModal;

function closeModal(id) {
  const m = document.getElementById(id);
  if (m) m.classList.add('hidden');
}
window.closeModal = closeModal;

function openAccount() {
  openModal('account-modal');
  refreshAccountDetails();
}
window.openAccount = openAccount;

function openBilling() {
  openModal('billing-modal');
}
window.openBilling = openBilling;

async function signOut() {
  try {
    await fetch('/api/v1/auth/logout', { method: 'POST' });
  } catch (e) {
    console.error('Sign out error:', e);
  }
  window.location.href = '/';
}
window.signOut = signOut;

async function checkout(tier) {
  try {
    const res = await fetch('/api/v1/create-checkout-session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tier })
    });
    const data = await res.json();
    if (data.checkout_url) {
      window.location.href = data.checkout_url;
    } else {
      showToast(data.detail || 'Could not start checkout', 'error');
    }
  } catch (err) {
    showToast('Billing error: ' + err.message, 'error');
  }
}
window.checkout = checkout;

// ─── User Profile & Auth Verification ─────────────────────────────────────
async function checkAuthAndProfile() {
  try {
    const res = await fetch('/api/v1/user/profile');
    if (res.status === 401 || res.status === 403) {
      document.getElementById('gate').classList.remove('hidden');
      document.getElementById('shell').classList.add('hidden');
      return false;
    }
    const data = await res.json();
    currentUser = data;
    document.getElementById('gate').classList.add('hidden');
    document.getElementById('shell').classList.remove('hidden');

    updateQuotaDisplay(data);
    refreshAccountDetails();
    checkYouTubeStatus();
    checkWorkerHeartbeat();
    return true;
  } catch (e) {
    document.getElementById('gate').classList.remove('hidden');
    document.getElementById('shell').classList.add('hidden');
    return false;
  }
}

function updateQuotaDisplay(data) {
  const q = document.getElementById('quota-label');
  if (!q) return;
  if (data.license === 'pro' || data.license === 'full_version') {
    q.innerHTML = `Plan: <strong>${data.license.toUpperCase()}</strong> (Unlimited renders)`;
  } else {
    q.innerHTML = `Free tier: <strong>${data.free_clips_used || 0}/1 used</strong>`;
  }
}

function refreshAccountDetails() {
  if (!currentUser) return;
  const emailEl = document.getElementById('acc-email');
  const planEl = document.getElementById('acc-plan');
  const usedEl = document.getElementById('acc-used');
  if (emailEl) emailEl.textContent = currentUser.email || 'Google User';
  if (planEl) planEl.textContent = (currentUser.license || 'Free tier').toUpperCase();
  if (usedEl) usedEl.textContent = String(currentUser.free_clips_used || 0);
}

// ─── Engine Status & YouTube Status ───────────────────────────────────────
async function checkYouTubeStatus() {
  try {
    const res = await fetch('/api/v1/auth/youtube/status');
    const data = await res.json();
    const dot = document.getElementById('yt-dot');
    const label = document.getElementById('yt-label');
    const badge = document.getElementById('yt-badge');

    if (data.connected) {
      if (dot) dot.className = 'dot live';
      if (label) label.textContent = 'YouTube Connected';
      if (badge) badge.onclick = () => showToast('YouTube channel is connected!');
    } else {
      if (dot) dot.className = 'dot off';
      if (label) label.textContent = 'Connect YouTube';
      if (badge) badge.onclick = () => location.href = '/api/v1/auth/youtube/connect';
    }
  } catch (e) {
  }
}

async function checkWorkerHeartbeat() {
  try {
    const uid = currentUser ? currentUser.user_id : 'cloud';
    const res = await fetch(`/api/v1/worker/heartbeat?user_id=${uid}`);
    const data = await res.json();
    const dot = document.getElementById('worker-dot');
    const label = document.getElementById('worker-label');

    if (data.alive !== false) {
      if (dot) dot.className = 'dot live';
      if (label) label.textContent = 'Engine Active';
    } else {
      if (dot) dot.className = 'dot warn';
      if (label) label.textContent = 'Engine Standby';
    }
  } catch (e) {
    const dot = document.getElementById('worker-dot');
    const label = document.getElementById('worker-label');
    if (dot) dot.className = 'dot live';
    if (label) label.textContent = 'Engine Active';
  }
}
setInterval(checkWorkerHeartbeat, 15000);

let currentSourceMode = 'my_upload';

// ─── Studio: Clip Generation ──────────────────────────────────────────────
function initStudio() {
  const sourceTabs = document.getElementById('source-tabs');
  if (sourceTabs) {
    sourceTabs.addEventListener('click', (e) => {
      const btn = e.target.closest('.source-tab-btn');
      if (!btn) return;
      sourceTabs.querySelectorAll('.source-tab-btn').forEach(b => b.classList.remove('picked'));
      btn.classList.add('picked');
      setSourceMode(btn.dataset.mode);
    });
  }

  const reel = document.getElementById('reel');
  const nicheInput = document.getElementById('niche-input');
  if (reel && nicheInput) {
    reel.addEventListener('click', (e) => {
      const btn = e.target.closest('button');
      if (!btn) return;
      reel.querySelectorAll('button').forEach(b => b.classList.remove('picked'));
      btn.classList.add('picked');
      nicheInput.value = btn.dataset.n || btn.textContent.trim().toLowerCase();
    });
  }

  const runBtn = document.getElementById('run-btn');
  if (runBtn) {
    runBtn.addEventListener('click', startGeneration);
  }
}

function setSourceMode(mode) {
  currentSourceMode = mode;
  document.querySelectorAll('.source-picker').forEach(el => el.classList.add('hidden'));
  const activePicker = document.getElementById(`picker-${mode}`);
  if (activePicker) activePicker.classList.remove('hidden');

  if (mode === 'my_channel') loadMyChannelVideos();
  if (mode === 'partner_channel') loadPartnerChannels();
}

async function loadMyChannelVideos() {
  const select = document.getElementById('my-video-select');
  if (!select) return;
  select.innerHTML = '<option value="">Loading your videos…</option>';
  try {
    const res = await fetch('/api/v1/my-channel/videos');
    if (!res.ok) {
      const err = await res.json();
      select.innerHTML = `<option value="">${err.detail || 'YouTube not connected'}</option>`;
      return;
    }
    const data = await res.json();
    if (!data.videos || data.videos.length === 0) {
      select.innerHTML = '<option value="">No videos found on your channel</option>';
      return;
    }
    select.innerHTML = data.videos.map(v => `<option value="${v.id}">${v.title || v.id} (${Math.round((v.duration||0)/60)}m)</option>`).join('');
  } catch (err) {
    select.innerHTML = '<option value="">Failed to load videos</option>';
  }
}

async function loadPartnerChannels() {
  const select = document.getElementById('partner-select');
  if (!select) return;
  select.innerHTML = '<option value="">Loading partner channels…</option>';
  try {
    const res = await fetch('/api/v1/partner-channels');
    const data = await res.json();
    if (!data.channels || data.channels.length === 0) {
      select.innerHTML = '<option value="">No partner channels currently active</option>';
      return;
    }
    select.innerHTML = data.channels.map(c => `<option value="${c.channel_id}">${c.channel_title || c.channel_id}</option>`).join('');
  } catch (err) {
    select.innerHTML = '<option value="">Failed to load partner channels</option>';
  }
}

async function startGeneration() {
  const layoutSel = document.getElementById('layout-select');
  const subSel = document.getElementById('subtitle-select');
  const numSel = document.getElementById('numclips-select');
  const autoToggle = document.getElementById('autopost-toggle');
  const runBtn = document.getElementById('run-btn');

  const payload = {
    source_mode: currentSourceMode,
    layout: layoutSel?.value || 'cinematic_blur',
    subtitle_style: subSel?.value || 'bold_captions',
    num_clips: parseInt(numSel?.value || '1', 10),
    auto_upload: Boolean(autoToggle?.checked),
  };

  if (currentSourceMode === 'my_upload') {
    const uploadInput = document.getElementById('upload-input');
    const file = uploadInput?.files?.[0];
    if (!file) {
      showToast('Please select a video file to upload', 'error');
      return;
    }
    payload.source_video_id = file.name;
    payload.niche = file.name.replace(/\.[^/.]+$/, "");
  } else if (currentSourceMode === 'my_channel') {
    const myVid = document.getElementById('my-video-select')?.value;
    if (!myVid) {
      showToast('Please select a video from your YouTube channel', 'error');
      return;
    }
    payload.source_video_id = myVid;
  } else if (currentSourceMode === 'partner_channel') {
    const partnerId = document.getElementById('partner-select')?.value;
    if (!partnerId) {
      showToast('Please select a partner creator', 'error');
      return;
    }
    payload.partner_channel_id = partnerId;
    payload.source_video_id = partnerId; // signals partner sourcing target
  } else if (currentSourceMode === 'public_domain') {
    const nicheInput = document.getElementById('niche-input');
    const niche = (nicheInput?.value || '').trim();
    if (!niche) {
      showToast('Please enter a topic or niche hint', 'error');
      return;
    }
    const rightsCheck = document.getElementById('rights-confirm-check');
    if (rightsCheck && !rightsCheck.checked) {
      showToast('Please confirm attribution acknowledgment to proceed', 'error');
      return;
    }
    payload.niche = niche;
    payload.rights_confirmed = Boolean(rightsCheck ? rightsCheck.checked : true);
  }


  runBtn.disabled = true;
  runBtn.textContent = 'Queuing…';

  try {
    const res = await fetch('/api/v1/generate-clip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

    if (res.status === 402) {
      openBilling();
      showToast('Free tier used — upgrade for unlimited renders', 'error');
      runBtn.disabled = false;
      runBtn.textContent = 'Generate';
      return;
    }

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Failed to queue job');
    }

    const data = await res.json();
    showToast('Pipeline started! Watch progress below.', 'live');
    startProgress(data.job_id);
  } catch (err) {
    showToast(err.message, 'error');
    runBtn.disabled = false;
    runBtn.textContent = 'Generate';
  }
}

function startProgress(jobId) {
  const pBox = document.getElementById('progress');
  const pMsg = document.getElementById('progress-msg');
  const pPct = document.getElementById('progress-pct');
  const pFill = document.getElementById('progress-fill');
  const runBtn = document.getElementById('run-btn');

  if (pBox) pBox.classList.add('active');
  if (pFill) pFill.style.width = '5%';
  if (pPct) pPct.textContent = '5%';
  if (pMsg) pMsg.textContent = 'Finding CC source video…';

  updateTicks(10);

  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const res = await fetch(`/api/v1/job-status/${jobId}`);
      if (!res.ok) return;
      const data = await res.json();
      const pct = Math.max(5, Math.min(100, data.progress || 0));

      if (pFill) pFill.style.width = `${pct}%`;
      if (pPct) pPct.textContent = `${pct}%`;
      if (pMsg) pMsg.textContent = data.message || 'Processing…';
      updateTicks(pct);

      if (data.status === 'complete' || data.status === 'draft_ready' || data.status === 'error' || pct >= 100) {
        clearInterval(pollTimer);
        runBtn.disabled = false;
        runBtn.textContent = 'Generate';

        if (data.status === 'complete') {
          showToast('Short published to YouTube!', 'live');
          setTimeout(() => switchView('clips'), 1200);
        } else if (data.status === 'draft_ready') {
          showToast('Short saved to Workplace drafts!', 'live');
          setTimeout(() => switchView('workplace'), 1200);
        } else if (data.status === 'error') {
          showToast(`Pipeline failed: ${data.message}`, 'error');
        }
      }
    } catch (e) {
    }
  }, 1800);
}

function updateTicks(pct) {
  const search = document.getElementById('tick-search');
  const download = document.getElementById('tick-download');
  const cut = document.getElementById('tick-cut');
  const upload = document.getElementById('tick-upload');

  if (search) {
    search.className = pct >= 25 ? 'tp-tick done' : (pct >= 5 ? 'tp-tick on' : 'tp-tick');
  }
  if (download) {
    download.className = pct >= 50 ? 'tp-tick done' : (pct >= 25 ? 'tp-tick on' : 'tp-tick');
  }
  if (cut) {
    cut.className = pct >= 80 ? 'tp-tick done' : (pct >= 50 ? 'tp-tick on' : 'tp-tick');
  }
  if (upload) {
    upload.className = pct >= 100 ? 'tp-tick done' : (pct >= 80 ? 'tp-tick on' : 'tp-tick');
  }
}

// ─── Workplace (Review & Publish Drafts) ───────────────────────────────────
// ─── Workplace (Review & Publish Drafts) ───────────────────────────────────
async function loadWorkplace() {
  const grid = document.getElementById('workplace-grid');
  if (!grid) return;
  grid.innerHTML = '<div class="empty">Loading drafts…</div>';

  try {
    const res = await fetch('/api/v1/workplace/clips');
    if (!res.ok) throw new Error('Failed to load drafts');
    const data = await res.json();
    const drafts = data.clips || data.drafts || (Array.isArray(data) ? data : []);

    if (!drafts || drafts.length === 0) {
      grid.innerHTML = '<div class="empty">No drafts waiting for review. Render a clip with auto-post turned off to review it here first.</div>';
      return;
    }

    grid.innerHTML = drafts.map(d => `
      <div class="clip-card" id="card-${d.id}">
        <div class="clip-thumb">
          ${d.youtube_url ? `<video src="${d.youtube_url}" preload="metadata" muted playsinline></video>` : ''}
          <div class="badge">DRAFT</div>
        </div>
        <div class="clip-body">
          <div class="clip-title" title="${escapeHtml(d.title || d.niche || 'Untitled Short')}">${escapeHtml(d.title || d.niche || 'Untitled Short')}</div>
          <div class="clip-actions">
            <button class="btn btn-primary" onclick="publishDraft('${d.id}')">Publish</button>
            <button class="btn btn-danger" onclick="deleteDraft('${d.id}')">Delete</button>
          </div>
        </div>
      </div>
    `).join('');
  } catch (err) {
    grid.innerHTML = `<div class="empty">Error loading drafts: ${escapeHtml(err.message)}</div>`;
  }
}

async function publishDraft(clipId) {
  try {
    const res = await fetch('/api/v1/clip/publish-draft', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clip_id: clipId })
    });
    if (!res.ok) throw new Error('Failed to publish');
    showToast('Draft queued for upload to your channel!', 'live');
    document.getElementById(`card-${clipId}`)?.remove();
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function deleteDraft(clipId) {
  if (!confirm('Delete this draft?')) return;
  try {
    const res = await fetch(`/api/v1/clip/${clipId}`, { method: 'DELETE' });
    if (!res.ok) throw new Error('Delete failed');
    showToast('Draft deleted');
    document.getElementById(`card-${clipId}`)?.remove();
  } catch (err) {
    showToast(err.message, 'error');
  }
}

// ─── My Clips (Live Videos & Stats) ───────────────────────────────────────
async function loadClips() {
  const grid = document.getElementById('clips-grid');
  if (!grid) return;
  grid.innerHTML = '<div class="empty">Loading channel clips…</div>';

  try {
    const res = await fetch('/api/v1/clips');
    if (!res.ok) throw new Error('Failed to load clips');
    const data = await res.json();

    const viewsEl = document.getElementById('stat-views');
    const countEl = document.getElementById('stat-count');
    const avgEl = document.getElementById('stat-avg');

    if (viewsEl) viewsEl.textContent = formatCompact(data.total_views || 0);
    if (countEl) countEl.textContent = String(data.total_videos || 0);
    if (avgEl) avgEl.textContent = formatCompact(data.avg_views || 0);

    const published = (data.videos || []).filter(v => v.youtube_url);
    if (!published || published.length === 0) {
      grid.innerHTML = '<div class="empty">No live clips posted yet. Start in the Studio!</div>';
      return;
    }

    grid.innerHTML = published.map(c => {
      const vidId = extractYtId(c.youtube_url);
      const thumb = vidId ? `https://i.ytimg.com/vi/${vidId}/hqdefault.jpg` : '';
      return `
        <div class="clip-card">
          <div class="clip-thumb" onclick="window.open('${c.youtube_url}', '_blank')">
            ${thumb ? `<img src="${thumb}" alt="thumbnail" loading="lazy">` : ''}
            <div class="badge">${formatCompact(c.views || 0)} VIEWS</div>
          </div>
          <div class="clip-body">
            <div class="clip-title">${escapeHtml(c.title || c.niche || 'Short')}</div>
            <div class="clip-actions">
              <a class="btn" href="${c.youtube_url}" target="_blank" rel="noopener">Watch ↗</a>
            </div>
          </div>
        </div>
      `;
    }).join('');
  } catch (err) {
    grid.innerHTML = `<div class="empty">Error loading clips: ${escapeHtml(err.message)}</div>`;
  }
}

// ─── Auto-Post Scheduler ──────────────────────────────────────────────────
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

async function loadAutoPost() {
  renderDays([]);
  try {
    const res = await fetch('/api/v1/auto-post/settings');
    if (!res.ok) return;
    const cfg = await res.json();
    const enabledEl = document.getElementById('ap-enabled');
    const nicheEl = document.getElementById('ap-niche');
    const rightsEl = document.getElementById('ap-rights-check');

    if (enabledEl) enabledEl.checked = Boolean(cfg.enabled);
    if (nicheEl) nicheEl.value = cfg.niche || 'motivation';
    if (rightsEl) rightsEl.checked = Boolean(cfg.rights_confirmed);

    const tList = document.getElementById('times-list');
    if (tList) {
      tList.innerHTML = '';
      (cfg.times || ["12:00"]).forEach(t => addTime(t));
    }
    renderDays(cfg.days || DAYS);
  } catch (e) {
    renderDays(DAYS);
  }
}

function renderDays(activeDays) {
  const container = document.getElementById('ap-days');
  if (!container) return;
  container.innerHTML = DAYS.map(d => {
    const isChecked = activeDays.includes(d);
    return `
      <label class="day-chip">
        <input type="checkbox" value="${d}" ${isChecked ? 'checked' : ''}>
        ${d.slice(0, 2)}
      </label>
    `;
  }).join('');
}

function addTime(val = '12:00') {
  const list = document.getElementById('times-list');
  if (!list) return;
  const row = document.createElement('div');
  row.className = 'time-row';
  row.innerHTML = `
    <input type="time" value="${val}">
    <button class="btn btn-ghost" type="button" onclick="this.parentElement.remove()">✕</button>
  `;
  list.appendChild(row);
}

async function saveAutoPost() {
  const enabled = Boolean(document.getElementById('ap-enabled')?.checked);
  const niche = (document.getElementById('ap-niche')?.value || 'motivation').trim();
  const times = Array.from(document.querySelectorAll('#times-list input[type=time]')).map(i => i.value).filter(Boolean);
  const days = Array.from(document.querySelectorAll('#ap-days input:checked')).map(i => i.value);
  const rights_confirmed = Boolean(document.getElementById('ap-rights-check')?.checked);

  if (enabled && !rights_confirmed) {
    showToast('Please confirm attribution acknowledgment to enable auto-post', 'error');
    return;
  }

  try {
    const res = await fetch('/api/v1/auto-post/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled, niche, times: times.length ? times : ["12:00"], days, rights_confirmed })
    });
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.detail || 'Save failed');
    }
    showToast('Auto-post schedule saved!', 'live');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

window.loadWorkplace = loadWorkplace;
window.publishDraft = publishDraft;
window.deleteDraft = deleteDraft;
window.loadClips = loadClips;
window.loadAutoPost = loadAutoPost;
window.addTime = addTime;
window.saveAutoPost = saveAutoPost;

// ─── Utilities ────────────────────────────────────────────────────────────
function escapeHtml(str) {
  return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function formatCompact(num) {
  num = Number(num) || 0;
  if (num >= 1_000_000) return (num / 1_000_000).toFixed(1) + 'M';
  if (num >= 1_000) return (num / 1_000).toFixed(1) + 'K';
  return String(num);
}

function extractYtId(url) {
  if (!url) return '';
  const m = url.match(/(?:shorts\/|v=|youtu\.be\/)([a-zA-Z0-9_-]{11})/);
  return m ? m[1] : '';
}

// ─── DOM Initialization ───────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // Handle auth / youtube redirect query parameters
  const params = new URLSearchParams(window.location.search);
  const authStatus = params.get('auth');
  const ytStatus = params.get('youtube');
  const detail = params.get('detail');

  if (authStatus === 'error') {
    const msg = detail === 'invalid_state' ? 'Login session expired or invalid. Please try again.'
              : detail === 'unverified_email' ? 'Please verify your Google email address.'
              : detail === 'not_configured' ? 'Google OAuth is not configured yet on this instance.'
              : 'Google sign-in failed. Please try again.';
    showToast(msg, 'error');
    window.history.replaceState({}, document.title, window.location.pathname);
  } else if (authStatus === 'success') {
    showToast('Signed in successfully!', 'live');
    window.history.replaceState({}, document.title, window.location.pathname);
  }

  if (ytStatus === 'error') {
    const msg = detail === 'invalid_state' ? 'YouTube connection session expired. Please retry.'
              : detail === 'not_configured' ? 'YouTube OAuth is not configured on this instance.'
              : 'Failed to connect YouTube channel. Please try again.';
    showToast(msg, 'error');
    window.history.replaceState({}, document.title, window.location.pathname);
  } else if (ytStatus === 'connected') {
    showToast('YouTube channel connected successfully!', 'live');
    window.history.replaceState({}, document.title, window.location.pathname);
  }

  initStudio();
  checkAuthAndProfile();
});

function getVisitorId() {
  let id = sessionStorage.getItem('clipai_visitor_id');
  if (!id) {
    id = 'v_' + Math.random().toString(36).slice(2) + Date.now();
    sessionStorage.setItem('clipai_visitor_id', id);
  }
  return id;
}

async function sendPresencePing() {
  try {
    await fetch(`/api/v1/presence/ping?visitor_id=${getVisitorId()}`, { method: 'POST' });
  } catch (e) {}
}

sendPresencePing();
setInterval(sendPresencePing, 20000);
