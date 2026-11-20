(() => {
  "use strict";
  const $ = (selector) => document.querySelector(selector);
  const library = $("#video-library");
  const video = $("#video-player");
  const qualitySelect = $("#quality-select");
  const convertButton = $("#convert-button");
  const aiButton = $("#ai-button");
  const state = { videos: [], activeSlug: null, activeQuality: "auto", hls: null, loadingUrl: null, pollTimer: null, busy: false };

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  }
  function humanSize(bytes) {
    if (!bytes) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes, index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
    return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
  }
  function statusLabel(item) {
    if (item.job_status === "processing") return "PROCESSING";
    if (item.job_status === "queued") return "QUEUED";
    if (item.status === "ready") return "READY";
    if (item.job_status === "failed") return "PARTIAL / FAILED";
    return "ORIGINAL READY";
  }
  function showNotice(message, kind = "info") {
    const node = $("#notice");
    node.textContent = message;
    node.className = `notice ${kind}`;
    node.style.display = message ? "block" : "none";
  }
  function renderLibrary() {
    const items = state.videos;
    $("#video-count").textContent = String(items.length);
    if (!items.length) {
      library.innerHTML = '<div class="empty-state"><div class="empty-glyph">▧</div><strong>Your library is empty</strong><span>Upload a video to start building your stream library.</span></div>';
      return;
    }
    library.innerHTML = items.map((item) => {
      const selected = item.slug === state.activeSlug ? " selected" : "";
      const q = (item.qualities || []).map((quality) => `<span class="quality-tag">${escapeHtml(quality)}</span>`).join("");
      const dimension = item.source_width && item.source_height ? `${item.source_width} × ${item.source_height}` : "Resolution pending";
      return `<button class="video-card${selected}" data-slug="${escapeHtml(item.slug)}" type="button">
        <span class="card-art"><span class="file-glyph">▶</span><span class="card-status ${item.job_status === "failed" ? "status-warn" : ""}">${escapeHtml(statusLabel(item))}</span></span>
        <span class="card-main"><span class="card-title">${escapeHtml(item.title)}</span><span class="card-details">${escapeHtml(dimension)} · ${escapeHtml(humanSize(item.size))}</span><span class="quality-tags">${q || '<span class="muted-tag">Original only</span>'}</span></span>
        <span class="card-arrow">↗</span></button>`;
    }).join("");
    library.querySelectorAll("[data-slug]").forEach((button) => button.addEventListener("click", () => selectVideo(button.dataset.slug)));
  }
  function selectedVideo() {
    return state.videos.find((item) => item.slug === state.activeSlug) || null;
  }
  function setQualityOptions(item) {
    const qualities = item?.qualities || [];
    const previous = state.activeQuality;
    const originalLabel = item?.source_quality ? `Original · ${item.source_quality}` : "Original source";
    qualitySelect.innerHTML = `<option value="auto">Auto · HLS master</option><option value="original">${escapeHtml(originalLabel)}</option>` + qualities.map((q) => `<option value="${escapeHtml(q)}">${escapeHtml(q)}</option>`).join("");
    qualitySelect.disabled = !item;
    if (previous === "original" || (previous !== "auto" && qualities.includes(previous))) {
      qualitySelect.value = previous;
    } else {
      qualitySelect.value = "auto";
      state.activeQuality = "auto";
    }
    $("#quality-hint").textContent = qualities.length ? `${qualities.length} rendition${qualities.length === 1 ? "" : "s"} currently available. More may appear while processing.` : "Original playback is available while HLS is being prepared.";
  }
  function destroyHls() {
    if (state.hls) { state.hls.destroy(); state.hls = null; }
  }
  function sourceFor(item, quality) {
    if (quality === "original" || !item.qualities?.length) return item.original_url;
    if (quality === "auto") return item.manifest_url;
    return `/api/stream/${encodeURIComponent(item.slug)}/${encodeURIComponent(quality)}/playlist.m3u8`;
  }
  function loadPlayback(item, quality = "auto", preservePosition = true) {
    if (!item) return;
    const url = sourceFor(item, quality);
    if (state.loadingUrl === url) return;
    const currentTime = preservePosition ? (Number.isFinite(video.currentTime) ? video.currentTime : 0) : 0;
    const wasPaused = video.paused;
    const volume = video.volume;
    const muted = video.muted;
    destroyHls();
    state.loadingUrl = url;
    video.pause();
    video.removeAttribute("src");
    video.load();
    video.volume = volume;
    video.muted = muted;
    const restore = () => {
      if (currentTime > 0 && Number.isFinite(video.duration)) {
        try { video.currentTime = Math.min(currentTime, Math.max(0, video.duration - 0.25)); } catch (_) { /* metadata may not be seekable yet */ }
      }
      if (!wasPaused) video.play().catch(() => {});
    };
    video.addEventListener("loadedmetadata", restore, { once: true });
    if (window.Hls && window.Hls.isSupported() && url.includes(".m3u8")) {
      const hls = new window.Hls({ enableWorker: true, lowLatencyMode: false, backBufferLength: 30 });
      state.hls = hls;
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(window.Hls.Events.ERROR, (_event, data) => {
        if (data.fatal) showNotice(`Playback error: ${data.details || data.type}`, "error");
      });
    } else {
      video.src = url;
      video.load();
    }
    $("#player-placeholder").classList.add("hidden");
    $("#player-state").textContent = quality === "original" || !item.qualities?.length ? "ORIGINAL" : (quality === "auto" ? "HLS AUTO" : quality.toUpperCase());
    state.activeQuality = quality;
  }
  function updateSidePanel(item) {
    if (!item) {
      $("#playing-title").textContent = "No video selected";
      $("#playing-subtitle").textContent = "Original and HLS playback";
      $("#player-state").textContent = "IDLE";
      convertButton.disabled = true;
      aiButton.disabled = true;
      $("#ai-panel").hidden = true;
      $("#process-description").textContent = "Select a video to view processing options.";
      return;
    }
    $("#playing-title").textContent = item.title;
    $("#playing-subtitle").textContent = `${item.source_width && item.source_height ? `${item.source_width} × ${item.source_height}` : "Resolution not inspected yet"} · ${humanSize(item.size)}`;
    const running = ["queued", "processing"].includes(item.job_status);
    const allDone = item.status === "ready";
    convertButton.disabled = running || allDone || state.busy;
    convertButton.textContent = item.has_streams ? "Retry missing HLS" : "Generate HLS";
    const highest = item.qualities?.[item.qualities.length - 1];
    const nativeNote = highest && item.source_quality && parseInt(item.source_quality, 10) > parseInt(highest, 10) ? ` Original ${item.source_quality} remains available above the configured HLS ladder.` : "";
    $("#process-description").textContent = running ? "A worker is processing this video. Completed qualities are already playable." : (item.has_streams ? `${item.qualities.length} quality level(s) are available.${nativeNote}` : "Generate native-quality HLS renditions without upscaling the source.");
    const scale = $("#ai-scale").value;
    const aiTargets = item.ai_upscale_targets_by_scale?.[scale] || item.ai_upscale_targets || [];
    $("#ai-panel").hidden = !item.ai_upscale_available || running || state.busy;
    $("#ai-description").textContent = aiTargets.length ? `Optional FrameLift enhancement can add: ${aiTargets.join(", ")}.` : "No additional configured quality remains at this scale. Try a higher scale or keep the existing qualities.";
    aiButton.disabled = running || !aiTargets.length || state.busy;
  }
  async function selectVideo(slug) {
    const item = state.videos.find((entry) => entry.slug === slug);
    if (!item) return;
    state.activeSlug = slug;
    state.activeQuality = "auto";
    state.loadingUrl = null;
    renderLibrary();
    setQualityOptions(item);
    updateSidePanel(item);
    loadPlayback(item, "auto", false);
    showNotice(item.job_error ? `Last processing attempt: ${item.job_error}` : "", item.job_error ? "error" : "info");
  }
  async function refreshVideos() {
    try {
      const response = await fetch("/api/videos", { cache: "no-store" });
      if (!response.ok) throw new Error(`API returned ${response.status}`);
      const data = await response.json();
      const previousActive = state.videos.find((item) => item.slug === state.activeSlug) || null;
      state.videos = data.videos || [];
      renderLibrary();
      const active = selectedVideo();
      if (active) {
        const fresh = active;
        const before = previousActive?.qualities?.join(",") || "";
        const after = fresh.qualities?.join(",") || "";
        setQualityOptions(fresh);
        updateSidePanel(fresh);
        // Do not interrupt current playback when another quality is published.
        // The new rendition appears in the quality selector immediately.
        if (before !== after) showNotice(`New quality available: ${fresh.qualities.filter((q) => !(active.qualities || []).includes(q)).join(", ")}`, "success");
      }
    } catch (error) {
      showNotice(`Could not refresh the video library: ${error.message}`, "error");
    }
  }
  async function requestJob(jobType) {
    const item = selectedVideo();
    if (!item || state.busy) return;
    state.busy = true;
    updateSidePanel(item);
    const button = jobType === "hls" ? convertButton : aiButton;
    button.disabled = true;
    const previousText = button.textContent;
    button.textContent = jobType === "hls" ? "Queueing…" : "Queueing AI…";
    try {
      const scale = $("#ai-scale").value;
      const params = new URLSearchParams({ job_type: jobType, mode: "auto" });
      if (jobType === "ai_upscale") params.set("scale", scale);
      const response = await fetch(`/api/convert/${encodeURIComponent(item.slug)}?${params}`, { method: "POST" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
      showNotice(data.message || (data.already_ready ? "Requested qualities are already available." : "Job queued. The video will update as each quality completes."), data.already_ready ? "info" : "success");
      await refreshVideos();
    } catch (error) {
      showNotice(error.message, "error");
    } finally {
      state.busy = false;
      button.textContent = previousText;
      updateSidePanel(selectedVideo());
    }
  }

  $("#upload-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = $("#video-file").files[0];
    if (!file || state.busy) return;
    state.busy = true;
    const button = $("#upload-button");
    const progress = $("#upload-progress");
    button.disabled = true;
    progress.hidden = false;
    $("#upload-status").textContent = `Uploading ${file.name}…`;
    try {
      const body = new FormData();
      body.append("file", file);
      const response = await fetch("/api/upload", { method: "POST", body });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `Upload failed (${response.status})`);
      $("#upload-status").textContent = "Upload complete · Original is ready";
      $("#video-file").value = "";
      $("#file-label").textContent = "Choose file";
      await refreshVideos();
      if (data.slug) await selectVideo(data.slug);
      showNotice("Original uploaded successfully. You can play it now or generate HLS renditions.", "success");
    } catch (error) {
      $("#upload-status").textContent = error.message;
      showNotice(error.message, "error");
    } finally {
      state.busy = false;
      button.disabled = false;
      window.setTimeout(() => { progress.hidden = true; }, 3200);
    }
  });
  $("#video-file").addEventListener("change", (event) => {
    const file = event.target.files[0];
    $("#file-label").textContent = file ? (file.name.length > 22 ? `${file.name.slice(0, 19)}…` : file.name) : "Choose file";
  });
  $("#refresh-button").addEventListener("click", refreshVideos);
  convertButton.addEventListener("click", () => requestJob("hls"));
  aiButton.addEventListener("click", () => requestJob("ai_upscale"));
  qualitySelect.addEventListener("change", () => {
    const item = selectedVideo();
    if (!item) return;
    const quality = qualitySelect.value;
    const wasPlaying = !video.paused;
    state.activeQuality = quality;
    loadPlayback(item, quality, true);
    if (wasPlaying) video.play().catch(() => {});
  });
  video.addEventListener("error", () => {
    if (state.loadingUrl) showNotice("The selected stream could not be played. Try another quality or the original.", "error");
  });
  refreshVideos();
  state.pollTimer = window.setInterval(refreshVideos, 5000);
})();
