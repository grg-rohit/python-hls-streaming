/**
 * player.js — multi-video HLS player + library renderer
 *
 * Responsibilities
 * ────────────────
 * • Fetch /api/videos and render the video library grid
 * • Handle Play button → show player section, load per-video manifest
 * • Quality dropdown / badges / table — all in sync
 * • hls.js adaptive bitrate with manual override
 * • Error recovery (network + media)
 * • Hash-based routing: #slug loads that video directly on page load
 */

(function () {
  "use strict";

  // ── DOM refs ──────────────────────────────────────────────────────────────
  const video         = document.getElementById("video");
  const qualitySelect = document.getElementById("quality-select");
  const statusMode    = document.getElementById("status-mode");
  const statusPlaying = document.getElementById("status-playing");
  const statusStream  = document.getElementById("status-stream");
  const spinner       = document.getElementById("spinner");
  const errorOverlay  = document.getElementById("error-overlay");
  const errorMessage  = document.getElementById("error-message");
  const qualityBadges = document.getElementById("quality-badges");
  const qualityInfo   = document.getElementById("quality-info");
  const playerSection = document.getElementById("player-section");
  const playerTitle   = document.getElementById("player-title");
  const libraryGrid   = document.getElementById("library-grid");
  const libraryEmpty  = document.getElementById("library-empty");
  const backBtn       = document.getElementById("back-btn");
  const refreshBtn    = document.getElementById("refresh-btn");

  // ── State ─────────────────────────────────────────────────────────────────
  let currentSlug   = null;
  const qualityMap  = {};   // quality name → hls.js level index

  // ── Spinner ───────────────────────────────────────────────────────────────
  function showSpinner() { spinner.classList.remove("hidden"); }
  function hideSpinner() { spinner.classList.add("hidden"); }

  // ── Error overlay ─────────────────────────────────────────────────────────
  function showError(msg) {
    errorMessage.textContent = msg;
    errorOverlay.classList.remove("hidden");
    hideSpinner();
  }
  function clearError() { errorOverlay.classList.add("hidden"); }

  // ── Stream status chip ────────────────────────────────────────────────────
  function setStreamStatus(text, cls) {
    statusStream.textContent = text;
    statusStream.className   = "status-value " + (cls || "");
  }

  // ── Library rendering ─────────────────────────────────────────────────────
  async function loadLibrary() {
    try {
      const res  = await fetch("/api/videos");
      const data = await res.json();
      renderLibrary(data.videos || []);
    } catch {
      renderLibrary([]);
    }
  }

  function renderLibrary(videos) {
    // Clear existing cards (keep the empty message node)
    Array.from(libraryGrid.children).forEach(c => {
      if (c !== libraryEmpty) c.remove();
    });

    if (videos.length === 0) {
      libraryEmpty.classList.remove("hidden");
      return;
    }
    libraryEmpty.classList.add("hidden");

    videos.forEach(v => {
      const card = buildVideoCard(v);
      libraryGrid.appendChild(card);
    });
  }

  function buildVideoCard(v) {
    const card = document.createElement("div");
    card.className      = "video-card";
    card.dataset.slug   = v.slug;

    // Status badge
    const statusCls = {
      ready:      "badge-ready",
      converting: "badge-converting",
      pending:    "badge-pending",
      error:      "badge-error",
    }[v.status] || "badge-pending";

    const statusLabel = {
      ready:      "Ready",
      converting: "Converting…",
      pending:    "Pending",
      error:      "Error",
    }[v.status] || v.status;

    // Quality chips (only shown when ready)
    const qualityChips = v.status === "ready" && v.qualities.length
      ? v.qualities.slice().reverse()
          .map(q => `<span class="card-quality-chip">${q}</span>`)
          .join("")
      : "";

    card.innerHTML = `
      <div class="card-thumb">
        <span class="card-thumb-icon">&#127916;</span>
      </div>
      <div class="card-body">
        <p class="card-title" title="${escHtml(v.title)}">${escHtml(v.title)}</p>
        <div class="card-meta">
          <span class="card-badge ${statusCls}">${statusLabel}</span>
          <span class="card-qualities">${qualityChips}</span>
        </div>
        ${v.status === "converting" ? `
          <div class="card-converting">
            <div class="converting-spinner"></div>
            <span>Converting — this may take a few minutes…</span>
          </div>` : ""}
        ${v.status === "error" ? `
          <p class="card-error-msg">Conversion failed. Check server logs.</p>` : ""}
      </div>
      <div class="card-actions">
        ${v.status === "ready"
          ? `<button class="btn-play" data-slug="${v.slug}">&#9654; Play</button>`
          : ""}
        ${v.status === "ready" || v.has_source
          ? `<button class="btn-reconvert" data-slug="${v.slug}" title="Re-convert">&#8635;</button>`
          : ""}
      </div>`;

    // Poll for status updates if converting
    if (v.status === "converting" || v.status === "pending") {
      pollConversionStatus(v.slug, card);
    }

    // Play button
    card.querySelector(".btn-play")?.addEventListener("click", () => playVideo(v.slug, v.title));

    // Re-convert button
    card.querySelector(".btn-reconvert")?.addEventListener("click", async () => {
      if (!confirm(`Re-convert "${v.title}"? This will overwrite existing streams.`)) return;
      await fetch(`/api/convert/${v.slug}`, { method: "POST" });
      await loadLibrary();
    });

    return card;
  }

  function pollConversionStatus(slug, card) {
    const interval = setInterval(async () => {
      try {
        const res  = await fetch(`/api/convert/${slug}/status`);
        const data = await res.json();

        if (data.status === "ready" || data.status === "error") {
          clearInterval(interval);
          await loadLibrary();   // re-render entire library to reflect new state
          // If this is the currently playing video, refresh its quality info
          if (currentSlug === slug && data.status === "ready") {
            loadQualityInfo(slug, data.qualities);
          }
        }
      } catch {
        // network blip — keep polling
      }
    }, 2000);
  }

  // ── Play a video ──────────────────────────────────────────────────────────
  function playVideo(slug, title) {
    currentSlug = slug;

    // Update URL hash for bookmarkability
    window.location.hash = slug;

    // Show player, hide library
    playerSection.classList.remove("hidden");
    playerTitle.textContent = title || slug;
    playerSection.scrollIntoView({ behavior: "smooth", block: "start" });

    // Reset UI
    clearError();
    showSpinner();
    setStreamStatus("loading…", "status-checking");
    resetQualityMenu();

    // Destroy any existing hls instance
    if (window._hls) {
      window._hls.destroy();
      window._hls = null;
    }

    video.src = "";

    // Fetch stream info then init player
    initPlayer(slug);
  }

  async function initPlayer(slug) {
    let info;
    try {
      const res = await fetch(`/api/stream-info/${slug}`);
      info      = await res.json();
    } catch {
      showError("Could not reach the server.");
      return;
    }

    if (!info.streams_ready) {
      setStreamStatus("not ready", "status-error");
      showError("Streams not ready yet. Wait for conversion to finish, then click Play again.");
      return;
    }

    // Pre-populate quality UI from API
    prePopulateQuality(info.qualities);
    loadQualityInfo(slug, info.qualities);

    const manifestUrl = info.manifest_url;

    if (Hls.isSupported()) {
      initHls(manifestUrl, info.qualities);
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
      initNative(manifestUrl);
    } else {
      setStreamStatus("unsupported", "status-error");
      showError("HLS is not supported in this browser.");
    }
  }

  // ── hls.js ────────────────────────────────────────────────────────────────
  function initHls(manifestUrl, apiQualities) {
    const hls = new Hls({
      enableWorker:            true,
      lowLatencyMode:          false,
      startLevel:              -1,
      fragLoadingMaxRetry:     4,
      manifestLoadingMaxRetry: 3,
    });
    window._hls = hls;

    hls.loadSource(manifestUrl);
    hls.attachMedia(video);

    hls.on(Hls.Events.MANIFEST_PARSED, function () {
      remapQualityToHlsLevels(hls);
      setStreamStatus("ready", "status-ready");
      clearError();
      video.play().catch(() => {});
    });

    hls.on(Hls.Events.LEVEL_SWITCHED, () => updatePlayingStatus(hls));
    hls.on(Hls.Events.FRAG_BUFFERED,  () => hideSpinner());

    hls.on(Hls.Events.ERROR, (_e, data) => {
      if (!data.fatal) return;
      if (data.type === Hls.ErrorTypes.NETWORK_ERROR) { hls.startLoad(); return; }
      if (data.type === Hls.ErrorTypes.MEDIA_ERROR)   { hls.recoverMediaError(); return; }
      hls.destroy();
      showError("Stream error: " + data.details);
    });
  }

  function initNative(manifestUrl) {
    video.src = manifestUrl;
    setStreamStatus("native HLS", "status-ready");
    qualitySelect.disabled = true;
    statusMode.textContent = "Native";
    video.addEventListener("loadedmetadata", () => video.play().catch(() => {}));
  }

  // ── Quality menu ──────────────────────────────────────────────────────────
  function resetQualityMenu() {
    while (qualitySelect.options.length > 1) qualitySelect.remove(1);
    qualitySelect.value    = "-1";
    statusMode.textContent = "Auto";
    statusPlaying.textContent = "—";
    if (qualityBadges) qualityBadges.innerHTML = "";
    if (qualityInfo)   qualityInfo.innerHTML   = "";
    Object.keys(qualityMap).forEach(k => delete qualityMap[k]);
  }

  function prePopulateQuality(qualities) {
    while (qualitySelect.options.length > 1) qualitySelect.remove(1);
    [...qualities].sort((a, b) => (parseInt(b) || 0) - (parseInt(a) || 0))
      .forEach(name => {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        opt.dataset.qualityName = name;
        qualitySelect.appendChild(opt);
      });
    renderBadges(qualities);
  }

  function remapQualityToHlsLevels(hls) {
    hls.levels.forEach((lvl, idx) => {
      qualityMap[lvl.height + "p"] = idx;
    });
    Array.from(qualitySelect.options).forEach(opt => {
      if (opt.value === "-1") return;
      const name = opt.dataset.qualityName || opt.textContent;
      if (qualityMap[name] !== undefined) opt.value = String(qualityMap[name]);
    });
  }

  function renderBadges(qualities) {
    if (!qualityBadges) return;
    qualityBadges.innerHTML = "";
    [...qualities].sort((a, b) => (parseInt(b) || 0) - (parseInt(a) || 0))
      .forEach(name => {
        const b = document.createElement("span");
        b.className = "badge";
        b.textContent = name;
        b.dataset.quality = name;
        b.addEventListener("click", () => selectQualityByName(name));
        qualityBadges.appendChild(b);
      });
  }

  function setActiveBadge(name) {
    qualityBadges?.querySelectorAll(".badge").forEach(b =>
      b.classList.toggle("badge-active", b.dataset.quality === name)
    );
  }

  function loadQualityInfo(slug, qualities) {
    if (!qualityInfo) return;
    const bitrateMap = {
      "240p": "600 kbps",  "360p": "1,200 kbps", "480p": "2,500 kbps",
      "720p": "5,000 kbps","1080p": "9,000 kbps","1440p": "16,000 kbps","2160p": "28,000 kbps",
    };
    const resMap = {
      "240p": "426×240", "360p": "640×360", "480p": "854×480",
      "720p": "1280×720","1080p": "1920×1080","1440p": "2560×1440","2160p": "3840×2160",
    };
    if (!qualities.length) { qualityInfo.innerHTML = ""; return; }
    const rows = [...qualities].sort((a, b) => (parseInt(b)||0)-(parseInt(a)||0))
      .map(n => `
        <tr class="qi-row" data-quality="${n}">
          <td class="qi-name">${n}</td>
          <td class="qi-res">${resMap[n] || "—"}</td>
          <td class="qi-bitrate">${bitrateMap[n] || "—"}</td>
          <td><button class="qi-btn" data-quality="${n}">Select</button></td>
        </tr>`).join("");
    qualityInfo.innerHTML = `
      <table class="qi-table">
        <thead><tr><th>Quality</th><th>Resolution</th><th>Video bitrate</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    qualityInfo.querySelectorAll(".qi-btn").forEach(btn =>
      btn.addEventListener("click", () => selectQualityByName(btn.dataset.quality))
    );
  }

  function setActiveRow(name) {
    qualityInfo?.querySelectorAll(".qi-row").forEach(r =>
      r.classList.toggle("qi-row-active", r.dataset.quality === name)
    );
  }

  function selectQualityByName(name) {
    if (!window._hls) return;
    const idx = qualityMap[name];
    if (idx === undefined) return;
    window._hls.currentLevel = idx;
    window._hls.loadLevel    = idx;
    window._hls.nextLevel    = idx;
    qualitySelect.value       = String(idx);
    statusMode.textContent    = "Manual";
    statusPlaying.textContent = name;
    setActiveBadge(name);
    setActiveRow(name);
  }

  qualitySelect.addEventListener("change", function () {
    if (!window._hls) return;
    const val = this.value;
    if (val === "-1" || isNaN(parseInt(val))) {
      window._hls.currentLevel = -1;
      statusMode.textContent   = "Auto";
      setActiveBadge(null);
      setActiveRow(null);
    } else {
      const idx  = parseInt(val, 10);
      window._hls.currentLevel = idx;
      window._hls.loadLevel    = idx;
      window._hls.nextLevel    = idx;
      const name = (window._hls.levels[idx]?.height || "") + "p";
      statusMode.textContent    = "Manual";
      statusPlaying.textContent = name;
      setActiveBadge(name);
      setActiveRow(name);
    }
  });

  function updatePlayingStatus(hls) {
    const isAuto = parseInt(qualitySelect.value, 10) === -1 || qualitySelect.value === "-1";
    statusMode.textContent = isAuto ? "Auto" : "Manual";
    const lvl  = hls.currentLevel;
    if (lvl >= 0 && hls.levels[lvl]) {
      const name = hls.levels[lvl].height + "p";
      statusPlaying.textContent = name;
      setActiveBadge(name);
      setActiveRow(name);
    }
  }

  // ── Video element events ──────────────────────────────────────────────────
  video.addEventListener("waiting",  showSpinner);
  video.addEventListener("playing",  hideSpinner);
  video.addEventListener("canplay",  hideSpinner);
  video.addEventListener("stalled",  showSpinner);
  video.addEventListener("error", () => {
    if (video.error) showError("Video error: " + (video.error.message || "unknown"));
  });

  // ── Back button ───────────────────────────────────────────────────────────
  backBtn.addEventListener("click", () => {
    if (window._hls) { window._hls.destroy(); window._hls = null; }
    video.pause();
    video.src = "";
    playerSection.classList.add("hidden");
    window.location.hash = "";
    currentSlug = null;
  });

  // ── Refresh button ────────────────────────────────────────────────────────
  refreshBtn.addEventListener("click", loadLibrary);

  // ── Hash routing ──────────────────────────────────────────────────────────
  async function handleHash() {
    const slug = window.location.hash.replace("#", "").trim();
    if (!slug) return;
    // Find video title from library
    try {
      const res  = await fetch("/api/videos");
      const data = await res.json();
      const v    = (data.videos || []).find(x => x.slug === slug);
      if (v && v.status === "ready") playVideo(v.slug, v.title);
    } catch { /* ignore */ }
  }

  // ── Utility ───────────────────────────────────────────────────────────────
  function escHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  // ── Exposed API (used by upload.js) ──────────────────────────────────────
  window.hlsPlayer = { loadLibrary, pollConversionStatus };

  // ── Init ──────────────────────────────────────────────────────────────────
  loadLibrary().then(handleHash);

})();
