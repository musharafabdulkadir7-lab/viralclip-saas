const chatWindow = document.getElementById('chat-window');
const progressContainer = document.getElementById('progress-container');
const progressBar = document.getElementById('progress-bar-fill');
const progressText = document.getElementById('progress-text');
const runClipBtn = document.getElementById('run-clip-farm-btn');
let pollingInterval = null;

document.addEventListener('DOMContentLoaded', () => {
    const runBtn = document.getElementById('run-clip-farm-btn');
    const nicheInput = document.getElementById('niche-input');

    // YouTube State
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get('youtube') === 'connected') {
        localStorage.setItem('youtube_connected', 'true');
        // Clean URL
        window.history.replaceState({}, document.title, window.location.pathname);
    }
    
    const isYouTubeConnected = localStorage.getItem('youtube_connected') === 'true';
    if (isYouTubeConnected) {
        document.getElementById('youtube-indicator').style.background = '#10b981';
        document.getElementById('youtube-status-text').innerText = 'Connected';
        document.getElementById('youtube-status-text').style.color = '#10b981';
        document.getElementById('connect-youtube-btn').innerText = 'YouTube Connected ✔';
        document.getElementById('connect-youtube-btn').style.background = '#334155';
        document.getElementById('connect-youtube-btn').style.pointerEvents = 'none';
    }

    // Assign mock user ID if not exists
    let userId = document.cookie.split('; ').find(row => row.startsWith('user_id='));
    if (!userId) {
        document.cookie = `user_id=demo_user_${Math.floor(Math.random()*10000)}; path=/; max-age=31536000`;
    }

    runBtn.addEventListener('click', async () => {
        if (!isYouTubeConnected) {
            addMessage('Director AI', 'Please connect your YouTube account first using the button on the left!', 'ai-message');
            return;
        }

        const niche = nicheInput.value.trim() || 'motivation';
        runBtn.disabled = true;
        runBtn.textContent = '⏳ Running...';
        
        try {
            const res = await fetch('/api/v1/generate-clip', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ niche: niche })
            });
            
            // PAYWALL TRIGGER
            if (res.status === 402) {
                document.getElementById('paywall-modal').classList.remove('hidden');
                runBtn.textContent = '▶️ Find & Post Clip Now';
                runBtn.disabled = false;
                return;
            }
            
            const data = await res.json();
            if (data.job_id) {
                addMessage('Director AI', `🔍 Starting pipeline for **${niche}**.\n\nWatch the progress bar above!`, 'ai-message');
                startStatusPolling(data.job_id);
            } else {
                runBtn.disabled = false;
                runBtn.textContent = '▶️ Find & Post Clip Now';
            }
        } catch (e) {
            addMessage('System', 'Connection error.', 'ai-message');
            runBtn.disabled = false;
            runBtn.textContent = '▶️ Find & Post Clip Now';
        }
    });
});

function addMessage(sender, text, className) {
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

function startStatusPolling(jobId) {
    progressContainer.classList.remove('hidden');
    progressBar.style.width = '10%';
    progressText.textContent = 'Initializing AI Brainstorming...';
    if(pollingInterval) clearInterval(pollingInterval);
    
    pollingInterval = setInterval(async () => {
        try {
            const res = await fetch('/api/v1/job-status/' + jobId);
            const data = await res.json();
            
            if (data.status !== 'idle' && data.status !== 'queued') {
                progressBar.style.width = data.progress + '%';
                progressText.textContent = data.message;
            }
            
            // Återställ knappen och rensa intervall om jobbet är klart eller misslyckat
            if (data.progress >= 100 || data.status === 'complete' || data.status === 'error') {
                clearInterval(pollingInterval);
                runClipBtn.disabled = false;
                runClipBtn.textContent = '▶️ Find & Post Clip Now';

                setTimeout(() => { progressContainer.classList.add('hidden'); }, 4000);
                
                if (data.url) {
                    addMessage('Director AI', `Here is your generated video!<br><a href="${data.url}" target="_blank" style="color: #60a5fa;">Watch on YouTube</a>`, 'ai-message');
                } else if (data.status === 'error') {
                    addMessage('Director AI', `⚠️ Pipeline error: ${data.message}`, 'ai-message');
                }
            }
        } catch(e) { 
            console.error(e);
            clearInterval(pollingInterval);
            runClipBtn.disabled = false;
            runClipBtn.textContent = '▶️ Find & Post Clip Now';
        }
    }, 1500);
}

runClipBtn.addEventListener('click', async () => {
    const niche = document.getElementById('niche-input').value.trim() || 'motivation';
    runClipBtn.disabled = true;
    runClipBtn.textContent = '⏳ Running...';
    
    try {
        const res = await fetch('/api/v1/generate-clip', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ niche: niche })
        });
        
        // PAYWALL TRIGGER
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
        addMessage('System', 'Connection error.', 'ai-message');
        runClipBtn.disabled = false;
        runClipBtn.textContent = '▶️ Find & Post Clip Now';
    }
});

document.getElementById('checkout-btn').addEventListener('click', async () => {
    const res = await fetch('/api/v1/create-checkout-session', { method: 'POST' });
    const data = await res.json();
    if(data.checkout_url) window.location.href = data.checkout_url;
});

document.getElementById('close-modal').addEventListener('click', () => {
    document.getElementById('paywall-modal').classList.add('hidden');
});
