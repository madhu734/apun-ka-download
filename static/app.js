const urlInput = document.getElementById("urlInput");
const pasteBtn = document.getElementById("pasteBtn");
const fetchForm = document.getElementById("fetchForm");
const modeVideoBtn = document.getElementById("modeVideoBtn");
const modeAudioBtn = document.getElementById("modeAudioBtn");

const detectedBadge = document.getElementById("detectedBadge");
const detectedIcon = document.getElementById("detectedIcon");
const detectedText = document.getElementById("detectedText");

const processingPanel = document.getElementById("processingPanel");
const kineticText = document.getElementById("kineticText");
const syncStageText = document.getElementById("syncStageText");
const syncProgressBar = document.getElementById("syncProgressBar");

const errorPanel = document.getElementById("errorPanel");
const errorMessage = document.getElementById("errorMessage");

const resultPanel = document.getElementById("resultPanel");
const overrideNotice = document.getElementById("overrideNotice");
const mediaThumb = document.getElementById("mediaThumb");
const mediaDuration = document.getElementById("mediaDuration");
const resultPlatformBadge = document.getElementById("resultPlatformBadge");
const mediaTitle = document.getElementById("mediaTitle");
const mediaUploader = document.getElementById("mediaUploader");
const optionsHeading = document.getElementById("optionsHeading");
const optionsList = document.getElementById("optionsList");

let currentMode = "video";
let currentUrl = "";
let currentPlatform = "";

const FALLBACK_THUMB =
  "data:image/svg+xml;utf8," +
  encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="640" height="400" viewBox="0 0 640 400"><rect width="640" height="400" fill="#111318"/><circle cx="320" cy="200" r="38" fill="#1e293b"/><polygon points="310,182 342,200 310,218" fill="#3b82f6"/></svg>`
  );

mediaThumb.addEventListener("error", () => {
  if (mediaThumb.src !== FALLBACK_THUMB) {
    mediaThumb.src = FALLBACK_THUMB;
  }
});

const PLATFORM_META = {
  youtube: {
    name: "YouTube Video",
    svg: `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M23.5 6.2a3 3 0 0 0-2.1-2.1C19.5 3.5 12 3.5 12 3.5s-7.5 0-9.4.6A3 3 0 0 0 .5 6.2 31.5 31.5 0 0 0 0 12a31.5 31.5 0 0 0 .5 5.8 3 3 0 0 0 2.1 2.1c1.9.6 9.4.6 9.4.6s7.5 0 9.4-.6a3 3 0 0 0 2.1-2.1A31.5 31.5 0 0 0 24 12a31.5 31.5 0 0 0-.5-5.8zM9.6 15.6V8.4l6.3 3.6-6.3 3.6z"/></svg>`
  },
  ytmusic: {
    name: "YouTube Music",
    svg: `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 14.5c-2.49 0-4.5-2.01-4.5-4.5S9.51 7.5 12 7.5s4.5 2.01 4.5 4.5-2.01 4.5-4.5 4.5zm-1.5-6.5v4l3.5-2-3.5-2z"/></svg>`
  },
  facebook: {
    name: "Facebook Video / Reel",
    svg: `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M22 12c0-5.52-4.48-10-10-10S2 6.48 2 12c0 4.84 3.44 8.87 8 9.8V15H8v-3h2V9.5C10 7.57 11.57 6 13.5 6H16v3h-2c-.55 0-1 .45-1 1v2h3v3h-3v6.95c5.05-.5 9-4.76 9-9.95z"/></svg>`
  },
  instagram: {
    name: "Instagram Reel / Post",
    svg: `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7.8 2h8.4C19.4 2 22 4.6 22 7.8v8.4a5.8 5.8 0 0 1-5.8 5.8H7.8C4.6 22 2 19.4 2 16.2V7.8A5.8 5.8 0 0 1 7.8 2zm-.2 2A3.6 3.6 0 0 0 4 7.6v8.8C4 18.39 5.61 20 7.6 20h8.8a3.6 3.6 0 0 0 3.6-3.6V7.6C20 5.61 18.39 4 16.4 4H7.6zm4.4 3a5 5 0 1 1 0 10 5 5 0 0 1 0-10zm0 2a3 3 0 1 0 0 6 3 3 0 0 0 0-6zm5.25-3.5a1.25 1.25 0 1 1 0 2.5 1.25 1.25 0 0 1 0-2.5z"/></svg>`
  },
  tiktok: {
    name: "TikTok Video",
    svg: `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M19.59 6.69a4.83 4.83 0 0 1-3.77-4.25V2h-3.45v13.67a2.89 2.89 0 0 1-5.2 1.74 2.89 2.89 0 0 1 2.31-4.64 2.93 2.93 0 0 1 .88.13V9.4a6.84 6.84 0 0 0-1-.05A6.33 6.33 0 0 0 5 20.1a6.34 6.34 0 0 0 10.86-4.43v-7a8.16 8.16 0 0 0 4.77 1.52v-3.4a4.85 4.85 0 0 1-1-.1z"/></svg>`
  }
};

// Initialize split-character kinetic "wait, processing" animation
function initKineticPhrase(phrase = "wait, processing") {
  kineticText.innerHTML = "";
  [...phrase].forEach((ch, idx) => {
    const span = document.createElement("span");
    span.className = ch === " " ? "kinetic-char space" : "kinetic-char";
    span.textContent = ch === " " ? "\u00A0" : ch;
    span.style.animationDelay = `${idx * 0.06}s`;
    kineticText.appendChild(span);
  });
}
initKineticPhrase();

function detectPlatformClient(url) {
  const clean = url.toLowerCase().trim();
  if (clean.includes("music.youtube.com")) return "ytmusic";
  if (clean.includes("youtube.com") || clean.includes("youtu.be")) return "youtube";
  if (clean.includes("facebook.com") || clean.includes("fb.watch") || clean.includes("fb.gg")) return "facebook";
  if (clean.includes("instagram.com")) return "instagram";
  if (clean.includes("tiktok.com")) return "tiktok";
  return null;
}

function updatePlatformBadge(url) {
  const platform = detectPlatformClient(url);
  if (platform && PLATFORM_META[platform]) {
    detectedBadge.classList.add("active");
    detectedIcon.innerHTML = PLATFORM_META[platform].svg;
    detectedText.textContent = `Detected: ${PLATFORM_META[platform].name}`;
    if (platform === "ytmusic") {
      setMode("audio");
    }
  } else {
    detectedBadge.classList.remove("active");
    detectedIcon.innerHTML = "";
    detectedText.textContent = "Supported: YouTube • YT Music • Facebook • Instagram • TikTok";
  }
}

function setMode(mode) {
  currentMode = mode;
  modeVideoBtn.classList.toggle("active", mode === "video");
  modeAudioBtn.classList.toggle("active", mode === "audio");
}

modeVideoBtn.addEventListener("click", () => setMode("video"));
modeAudioBtn.addEventListener("click", () => setMode("audio"));

urlInput.addEventListener("input", (e) => {
  updatePlatformBadge(e.target.value);
});

// Auto-advance immediately when user pastes via keyboard/context menu
urlInput.addEventListener("paste", () => {
  setTimeout(() => {
    const val = urlInput.value.trim();
    updatePlatformBadge(val);
    if (val) {
      triggerAnalyze(val);
    }
  }, 40);
});

// Clipboard Paste Icon click -> auto-paste & auto-advance
pasteBtn.addEventListener("click", async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (text && text.trim()) {
      urlInput.value = text.trim();
      updatePlatformBadge(urlInput.value);
      triggerAnalyze(urlInput.value);
    } else {
      urlInput.focus();
    }
  } catch (err) {
    urlInput.focus();
  }
});

fetchForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const val = urlInput.value.trim();
  if (val) {
    triggerAnalyze(val);
  }
});

function showProcessing(stageMsg, pct = null) {
  errorPanel.classList.add("hidden");
  processingPanel.classList.remove("hidden");
  syncStageText.textContent = stageMsg;
  if (pct !== null && pct > 0) {
    syncProgressBar.classList.add("determinate");
    syncProgressBar.style.width = `${Math.min(100, Math.max(8, pct))}%`;
  } else {
    syncProgressBar.classList.remove("determinate");
    syncProgressBar.style.width = "42%";
  }
}

function hideProcessing() {
  processingPanel.classList.add("hidden");
}

function showError(msg) {
  hideProcessing();
  resultPanel.classList.add("hidden");
  errorMessage.textContent = msg;
  errorPanel.classList.remove("hidden");
}

async function triggerAnalyze(url) {
  currentUrl = url;
  resultPanel.classList.add("hidden");
  showProcessing("Analyzing link & inspecting available streams...");

  try {
    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, mode: currentMode })
    });

    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Could not analyze this link.");
    }

    hideProcessing();
    renderResults(data);
  } catch (err) {
    showError(err.message);
  }
}

function renderResults(data) {
  currentPlatform = data.platform;
  overrideNotice.classList.toggle("hidden", !data.mode_overridden);

  mediaThumb.src = data.thumbnail || FALLBACK_THUMB;
  mediaDuration.textContent = data.duration || "0:00";
  resultPlatformBadge.textContent = PLATFORM_META[data.platform]?.name || data.platform.toUpperCase();
  mediaTitle.textContent = data.title;
  mediaUploader.textContent = data.uploader;

  optionsHeading.textContent =
    data.effective_mode === "audio" ? "Audio Extraction Options" : "Select Video Resolution";

  optionsList.innerHTML = "";
  data.options.forEach((opt) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "option-btn";
    btn.innerHTML = `
      <div class="option-main">
        <span class="option-title">${opt.label}</span>
        <span class="option-sub">${opt.sublabel}</span>
      </div>
      <div class="option-right">
        <span class="option-size">${opt.size}</span>
        <span class="option-badge">${opt.badge}</span>
      </div>
    `;
    btn.addEventListener("click", () => startSyncedDownload(opt.id));
    optionsList.appendChild(btn);
  });

  resultPanel.classList.remove("hidden");
}

async function startSyncedDownload(preset) {
  showProcessing("Queueing stream extraction on server...", 8);

  try {
    const prepRes = await fetch("/api/prepare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: currentUrl,
        preset,
        platform: currentPlatform
      })
    });

    const prepData = await prepRes.json();
    if (!prepRes.ok) {
      throw new Error(prepData.detail || "Failed to start processing.");
    }

    const jobId = prepData.job_id;
    pollJobStatus(jobId);
  } catch (err) {
    showError(err.message);
  }
}

function pollJobStatus(jobId) {
  const interval = setInterval(async () => {
    try {
      const res = await fetch(`/api/status/${jobId}`);
      const data = await res.json();

      if (!res.ok) {
        clearInterval(interval);
        showError(data.detail || "Processing session expired.");
        return;
      }

      showProcessing(data.stage || "Processing stream...", data.progress || null);

      if (data.status === "ready") {
        clearInterval(interval);
        showProcessing("Stream ready! Starting browser download...", 100);
        setTimeout(() => {
          window.location.href = `/api/file/${jobId}`;
          hideProcessing();
        }, 400);
      } else if (data.status === "error") {
        clearInterval(interval);
        showError(data.error || "Failed to process this format.");
      }
    } catch (err) {
      clearInterval(interval);
      showError("Lost connection to server while syncing progress.");
    }
  }, 600);
}
