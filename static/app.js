(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const el = {
    toggle: $("#modeToggle"),
    form: $("#urlForm"),
    input: $("#urlInput"),
    paste: $("#pasteBtn"),
    fetch: $("#fetchBtn"),
    badge: $("#badgeRow"),
    notice: $("#notice"),
    error: $("#error"),
    proc: $("#proc"),
    kinetic: $("#kinetic"),
    term: $("#termText"),
    result: $("#result"),
    thumb: $("#thumb"),
    thumbImg: $("#thumbImg"),
    thumbFallback: $("#thumbFallback"),
    chip: $("#durationChip"),
    resultBadge: $("#resultBadge"),
    title: $("#mediaTitle"),
    uploader: $("#mediaUploader"),
    options: $("#options"),
  };

  const PLATFORMS = {
    youtube: {
      label: "YouTube", color: "#FF0033",
      svg: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z"/></svg>',
    },
    ytmusic: {
      label: "YT Music", color: "#FF0033",
      svg: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="11" fill="currentColor"/><circle cx="12" cy="12" r="7.2" stroke="#fff" stroke-width="1.4"/><path d="M10 8.6v6.8l5.6-3.4z" fill="#fff"/></svg>',
    },
    facebook: {
      label: "Facebook", color: "#1877F2",
      svg: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z"/></svg>',
    },
    instagram: {
      label: "Instagram", color: "#E1306C",
      svg: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><rect x="2.5" y="2.5" width="19" height="19" rx="5.5"/><circle cx="12" cy="12" r="4.4"/><circle cx="17.6" cy="6.4" r="1.2" fill="currentColor" stroke="none"/></svg>',
    },
    tiktok: {
      label: "TikTok", color: "#111318",
      svg: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12.525.02c1.31-.02 2.61-.01 3.91-.02.08 1.53.63 3.09 1.75 4.17 1.12 1.11 2.7 1.62 4.24 1.79v4.03c-1.44-.05-2.89-.35-4.2-.97-.57-.26-1.1-.59-1.62-.93-.01 2.92.01 5.84-.02 8.75-.08 1.4-.54 2.79-1.35 3.94-1.31 1.92-3.58 3.17-5.91 3.21-1.43.08-2.86-.31-4.08-1.03-2.02-1.19-3.44-3.37-3.65-5.71-.02-.5-.03-1-.01-1.49.18-1.9 1.12-3.72 2.58-4.96 1.66-1.44 3.98-2.13 6.15-1.72.02 1.48-.04 2.96-.04 4.44-.99-.32-2.15-.23-3.02.37-.63.41-1.11 1.04-1.36 1.75-.21.51-.15 1.07-.14 1.61.24 1.64 1.82 3.02 3.5 2.87 1.12-.01 2.19-.66 2.77-1.61.19-.33.4-.67.41-1.06.1-1.79.06-3.57.07-5.36.01-4.03-.01-8.05.02-12.07z"/></svg>',
    },
  };

  const state = { mode: "video", busy: false, analysis: null };

  /* ---------- Platform detection (mirrors the backend) ---------- */
  function detectPlatform(raw) {
    let url = (raw || "").trim();
    if (!url) return null;
    if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(url)) url = "https://" + url;
    let host;
    try { host = new URL(url).hostname.toLowerCase(); } catch { return null; }
    const is = (...ds) => ds.some((d) => host === d || host.endsWith("." + d));
    if (host === "music.youtube.com") return "ytmusic";
    if (is("youtube.com", "youtube-nocookie.com") || host === "youtu.be") return "youtube";
    if (is("facebook.com", "fb.watch", "fb.gg")) return "facebook";
    if (is("instagram.com")) return "instagram";
    if (is("tiktok.com")) return "tiktok";
    return null;
  }

  function badgeNode(key) {
    const span = document.createElement("span");
    span.className = "pbadge";
    const p = PLATFORMS[key];
    if (!p) { span.classList.add("unknown"); span.textContent = "Unsupported link"; return span; }
    span.innerHTML = p.svg;
    span.firstChild.style.color = p.color;
    const label = document.createElement("span");
    label.textContent = p.label;
    span.appendChild(label);
    return span;
  }

  function renderBadge(key, hasText) {
    el.badge.textContent = "";
    if (!hasText) { el.badge.hidden = true; return; }
    el.badge.hidden = false;
    el.badge.appendChild(badgeNode(key));
  }

  /* ---------- Messages & UI state ---------- */
  function clearMessages() { el.notice.hidden = true; el.error.hidden = true; el.notice.classList.remove("ok"); }
  function showError(msg) { el.error.textContent = msg; el.error.hidden = false; }
  function showNotice(msg, ok) {
    el.notice.textContent = msg;
    el.notice.classList.toggle("ok", !!ok);
    el.notice.hidden = false;
  }

  function lockUI(locked) {
    state.busy = locked;
    el.fetch.disabled = locked;
    el.paste.disabled = locked;
    el.toggle.querySelectorAll("button").forEach((b) => (b.disabled = locked));
    el.options.querySelectorAll("button").forEach((b) => (b.disabled = locked));
  }

  function setMode(mode) {
    state.mode = mode;
    el.toggle.querySelectorAll(".seg-btn").forEach((b) => {
      const on = b.dataset.mode === mode;
      b.classList.toggle("is-active", on);
      b.setAttribute("aria-selected", String(on));
    });
  }

  function buildKinetic() {
    const text = "wait, processing...";
    el.kinetic.textContent = "";
    el.kinetic.setAttribute("aria-label", "wait, processing...");
    [...text].forEach((ch, i) => {
      const s = document.createElement("span");
      s.className = "k";
      s.style.setProperty("--i", i);
      s.setAttribute("aria-hidden", "true");
      s.textContent = ch === " " ? "\u00A0" : ch;
      el.kinetic.appendChild(s);
    });
  }

  function showProcessing() { el.proc.hidden = false; }
  function hideProcessing() { el.proc.hidden = true; }
  function setStage(text) { if (el.term) el.term.textContent = text || ""; }

  /* ---------- API ---------- */
  async function api(path, options) {
    let res;
    try {
      res = await fetch(path, options);
    } catch {
      throw new Error("Cannot reach the server. Check your connection and try again.");
    }
    let data = {};
    try { data = await res.json(); } catch { /* non-JSON body */ }
    if (!res.ok) {
      const detail = typeof data.detail === "string" ? data.detail : "Something went wrong. Please try again.";
      throw new Error(detail);
    }
    return data;
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  /* ---------- Analyze ---------- */
  async function analyze() {
    if (state.busy) return;
    const url = el.input.value.trim();
    clearMessages();
    if (!url) { showError("Paste a link first."); return; }
    const platform = detectPlatform(url);
    renderBadge(platform, true);
    if (!platform) {
      showError("Unsupported link. Paste a YouTube, YT Music, Facebook, Instagram or TikTok URL.");
      return;
    }
    el.result.hidden = true;
    state.analysis = null;
    lockUI(true);
    showProcessing("Analyzing link & reading available streams...");
    try {
      const data = await api("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, mode: state.mode }),
      });
      state.analysis = data;
      if (data.mode !== state.mode) setMode(data.mode);
      if (data.mode_overridden && data.override_message) showNotice(data.override_message);
      renderResult(data);
    } catch (err) {
      showError(err.message);
    } finally {
      hideProcessing();
      lockUI(false);
    }
  }

  /* ---------- Result ---------- */
  function renderResult(data) {
    el.title.textContent = data.title;
    el.uploader.textContent = data.uploader || "";
    el.resultBadge.textContent = "";
    el.resultBadge.appendChild(badgeNode(data.platform));
    renderBadge(data.platform, true);

    el.chip.hidden = !data.duration_text;
    el.chip.textContent = data.duration_text || "";

    el.thumb.classList.remove("no-img");
    el.thumbFallback.innerHTML = (PLATFORMS[data.platform] || PLATFORMS.youtube).svg;
    el.thumbImg.onerror = () => el.thumb.classList.add("no-img");
    if (data.thumbnail) { el.thumbImg.src = data.thumbnail; } else { el.thumb.classList.add("no-img"); }

    el.options.textContent = "";
    data.options.forEach((opt) => el.options.appendChild(optionNode(opt)));
    el.result.hidden = false;
    el.result.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function optionNode(opt) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "option";

    const res = document.createElement("span");
    res.className = "res";
    res.textContent = opt.resolution;

    const body = document.createElement("span");
    const title = document.createElement("span");
    title.className = "o-title";
    title.textContent = opt.label;
    const desc = document.createElement("span");
    desc.className = "o-desc";
    desc.style.display = "block";
    desc.textContent = opt.description;
    body.append(title, desc);

    const side = document.createElement("span");
    side.className = "o-side";
    const size = document.createElement("span");
    size.className = "o-size";
    size.textContent = opt.size_text || "Size n/a";
    const fmt = document.createElement("span");
    fmt.className = "o-fmt";
    fmt.textContent = opt.format;
    side.append(size, fmt);

    btn.append(res, body, side);
    btn.addEventListener("click", () => startDownload(opt));
    return btn;
  }

  /* ---------- Prepare -> status -> file ---------- */
  async function startDownload(opt) {
    if (state.busy || !state.analysis) return;
    clearMessages();
    lockUI(true);
    showProcessing("Connecting to upstream server...");
    try {
      const { job_id } = await api("/api/prepare", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: state.analysis.url,
          kind: opt.kind,
          format_selector: opt.format_selector || null,
        }),
      });
      await pollJob(job_id);
    } catch (err) {
      showError(err.message);
    } finally {
      hideProcessing();
      lockUI(false);
    }
  }

  async function pollJob(jobId) {
    let failures = 0;
    for (;;) {
      let s;
      try {
        s = await api(`/api/status/${jobId}`);
        failures = 0;
      } catch (err) {
        if (++failures >= 5) throw err;
        await sleep(900);
        continue;
      }
      setStage(s.stage);
      if (s.status === "error") throw new Error(s.error || "The download failed.");
      if (s.status === "ready") {
        await sleep(350);
        triggerDownload(jobId);
        showNotice("Your download has started. Check your downloads folder.", true);
        return;
      }
      await sleep(600);
    }
  }

  function triggerDownload(jobId) {
    const a = document.createElement("a");
    a.href = `/api/file/${encodeURIComponent(jobId)}`;
    a.download = "";
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  /* ---------- Input wiring ---------- */
  function setUrl(text) {
    el.input.value = text;
    renderBadge(detectPlatform(text), !!text.trim());
  }

  el.form.addEventListener("submit", (e) => { e.preventDefault(); analyze(); });

  el.input.addEventListener("input", () => renderBadge(detectPlatform(el.input.value), !!el.input.value.trim()));

  // Keyboard / mobile paste: populate, detect, and advance instantly.
  el.input.addEventListener("paste", (e) => {
    const text = ((e.clipboardData || window.clipboardData)?.getData("text") || "").trim();
    if (!text) return;
    e.preventDefault();
    setUrl(text);
    analyze();
  });

  // One-click clipboard button.
  el.paste.addEventListener("click", async () => {
    clearMessages();
    try {
      const text = ((await navigator.clipboard.readText()) || "").trim();
      if (!text) { showError("Your clipboard is empty. Copy a link first."); return; }
      setUrl(text);
      analyze();
    } catch {
      showError("Clipboard access was blocked. Long-press the box and choose Paste instead.");
      el.input.focus();
    }
  });

  el.toggle.addEventListener("click", (e) => {
    const btn = e.target.closest(".seg-btn");
    if (!btn || state.busy || btn.dataset.mode === state.mode) return;
    setMode(btn.dataset.mode);
    // If a result is already showing, refresh it for the new mode.
    if (state.analysis && el.input.value.trim()) analyze();
  });

  buildKinetic();
})();
