(() => {
  "use strict";
  const $ = (selector) => document.querySelector(selector);
  const library = $("#video-library");
  const video = $("#video-player");
  const qualitySelect = $("#quality-select");
  const convertButton = $("#convert-button");
  const aiButton = $("#ai-button");
  const fullscreenButton = $("#fullscreen-button");
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
    fullscreenButton.disabled = false;
    state.activeQuality = "auto";
    state.loadingUrl = null;
    renderLibrary();
    setQualityOptions(item);
    updateSidePanel(item);
    loadPlayback(item, "auto", false);
    // A card click is a user-initiated request to watch, not just select.
    video.play().catch(() => { /* Browser may require a second click on play. */ });
    showNotice(item.job_error ? `Last processing attempt: ${item.job_error}` : "", item.job_error ? "error" : "info");
  }
  async function refreshVideos() {
    try {
      const response = await fetch("/api/videos", { cache: "no-store" });
      if (!response.ok) throw new Error(`API returned ${response.status}`);
      const data = await response.json();
      const previousActive = state.videos.find((item) => item.slug === state.activeSlug) || null;
      state.videos = data.videos || [];
      updateBenchmarkVideos();
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
        if (before !== after) {
          const added = (fresh.qualities || []).filter((q) => !(previousActive?.qualities || []).includes(q));
          if (added.length) showNotice(`New quality available: ${added.join(", ")}`, "success");
        }
        // Auto initially falls back to the original. Switch to HLS when the
        // first rendition becomes playable without discarding playback position.
        if (state.activeQuality === "auto" && !previousActive?.has_streams && fresh.has_streams && fresh.manifest_url) {
          loadPlayback(fresh, "auto", true);
        }
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

  function postVideoWithProgress(file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload");
      xhr.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable && event.total > 0) {
          onProgress(Math.min(100, Math.round((event.loaded / event.total) * 100)));
        }
      });
      xhr.addEventListener("load", () => {
        let data;
        try { data = JSON.parse(xhr.responseText); } catch (_) { data = {}; }
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new Error(data.detail || `Upload failed (${xhr.status})`));
      });
      xhr.addEventListener("error", () => reject(new Error("Network error while uploading. Please try again.")));
      xhr.addEventListener("abort", () => reject(new Error("Upload cancelled.")));
      const body = new FormData();
      body.append("file", file);
      xhr.send(body);
    });
  }
  $("#upload-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = $("#video-file").files[0];
    if (!file || state.busy) return;
    state.busy = true;
    const button = $("#upload-button");
    const progress = $("#upload-progress");
    const fill = $("#upload-progress-fill");
    const meter = $("#upload-meter");
    const status = $("#upload-status");
    button.disabled = true;
    progress.hidden = false;
    fill.style.width = "0%";
    meter.setAttribute("aria-valuenow", "0");
    status.textContent = `Uploading ${file.name} · 0%`;
    try {
      const data = await postVideoWithProgress(file, (percent) => {
        fill.style.width = `${percent}%`;
        meter.setAttribute("aria-valuenow", String(percent));
        status.textContent = percent === 100 ? "Upload transferred · Saving video…" : `Uploading · ${percent}%`;
      });
      $("#video-file").value = "";
      $("#file-label").textContent = "Choose file";
      await refreshVideos();
      if (data.slug) await selectVideo(data.slug);
      showNotice("Original uploaded successfully. You can play it now or generate HLS renditions.", "success");
    } catch (error) {
      showNotice(error.message, "error");
    } finally {
      state.busy = false;
      button.disabled = false;
      progress.hidden = true;
      fill.style.width = "0%";
    }
  });
  $("#video-file").addEventListener("change", (event) => {
    const file = event.target.files[0];
    $("#file-label").textContent = file ? (file.name.length > 22 ? `${file.name.slice(0, 19)}…` : file.name) : "Choose file";
  });
  $("#refresh-button").addEventListener("click", refreshVideos);
  fullscreenButton.addEventListener("click", async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else if ($("#player-frame").requestFullscreen) await $("#player-frame").requestFullscreen();
      else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen();
    } catch (_) { showNotice("Fullscreen is unavailable in this browser.", "error"); }
  });
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
  // Benchmarks require an explicit admin token and run outside production renditions.
  const benchmarkForm = $("#benchmark-form");
  const benchmarkRun = $("#benchmark-run");
  const benchmarkVideo = $("#benchmark-video");
  let benchmarkJob = null;
  let benchmarkTimer = null;
  let benchmarkGeneration = 0;
  let benchmarkJobMode = null;
  let benchmarkBusy = false;
  let benchmarkEnabled = false;
  let aiBenchmarkEnabled = false;
  const benchmarkMode = $("#benchmark-mode");
  function setBenchmarkMode() {
    // A result belongs only to the mode which created it. Stop polling when switching.
    benchmarkGeneration += 1;
    if (benchmarkTimer) { clearInterval(benchmarkTimer); benchmarkTimer = null; }
    benchmarkJob = null;
    benchmarkJobMode = null;
    benchmarkBusy = false;
    $("#benchmark-results").hidden = true;
    $("#benchmark-results").innerHTML = "";
    $("#benchmark-progress").textContent = "";
    const ai = benchmarkMode.value === "ai";
    $("#benchmark-description").textContent = ai
      ? "Test FrameLift AI enhancement on a short isolated sample using your GPU worker. See measured processing speed, GPU memory and output resolution. Your original and published videos are never changed."
      : "Compare CPU x264 encoding speed, output size and SSIM on a short sample. Your original and published videos are never changed.";
    document.querySelectorAll(".ai-benchmark-only").forEach((node) => { node.hidden = !ai; });
    document.querySelectorAll(".cpu-benchmark-only").forEach((node) => { node.hidden = ai; });
    benchmarkRun.disabled = !benchmarkEnabled || (ai && !aiBenchmarkEnabled);
    $("#benchmark-state").textContent = benchmarkEnabled ? (ai && !aiBenchmarkEnabled ? "UNAVAILABLE" : "READY") : "DISABLED";
    benchmarkRun.textContent = ai ? "Run AI benchmark ↗" : "Run CPU benchmark ↗";
    const fifteen = $("#benchmark-seconds").querySelector('option[value="15"]');
    fifteen.disabled = ai;
    if (ai && $("#benchmark-seconds").value === "15") $("#benchmark-seconds").value = "10";
    $("#benchmark-capability").textContent = ai
      ? (aiBenchmarkEnabled ? "AI benchmark is queued for a CUDA worker. Processing is isolated from published videos." : "AI benchmarks require BENCHMARK_AI_ENABLED=true on API and GPU worker.")
      : "CPU benchmark uses the API container and compares x264 presets.";
  }
  benchmarkMode.addEventListener("change", setBenchmarkMode);
  const benchmarkCompare = $("#benchmark-compare");
  const candidateTileLabel = $("#candidate-tile-label");
  const comparisonRoundsLabel = $("#comparison-rounds-label");
  function syncCandidateTile() {
    candidateTileLabel.hidden = benchmarkMode.value !== "ai" || benchmarkCompare.value !== "yes";
    $("#candidate-model-label").hidden = benchmarkMode.value !== "ai" || benchmarkCompare.value !== "models";
    comparisonRoundsLabel.hidden = benchmarkMode.value !== "ai" || benchmarkCompare.value === "no";
  }
  benchmarkCompare.addEventListener("change", syncCandidateTile);
  benchmarkMode.addEventListener("change", syncCandidateTile);
  syncCandidateTile();
  function updateBenchmarkVideos() {
    const current = benchmarkVideo.value;
    benchmarkVideo.innerHTML = '<option value="">Select a video</option>' + state.videos.map((item) => `<option value="${escapeHtml(item.slug)}">${escapeHtml(item.title || item.filename || item.slug)}</option>`).join("");
    if (state.videos.some((item) => item.slug === current)) benchmarkVideo.value = current;
    else if (state.activeSlug && state.videos.some((item) => item.slug === state.activeSlug)) benchmarkVideo.value = state.activeSlug;
  }
  async function loadBenchmarkConfig() {
    try {
      const response = await fetch("/api/benchmark/config", { cache: "no-store" });
      if (!response.ok) throw new Error("Benchmark API is unavailable");
      const data = await response.json();
      benchmarkEnabled = Boolean(data.enabled);
      aiBenchmarkEnabled = Boolean(data.ai_available);
      setBenchmarkMode();
    } catch (error) {
      benchmarkRun.disabled = true;
      $("#benchmark-capability").textContent = error.message;
    }
  }
  function renderBenchmark(job) {
    if (job.mode !== benchmarkMode.value) return;
    const progress = $("#benchmark-progress");
    const resultNode = $("#benchmark-results");
    $("#benchmark-state").textContent = job.status.toUpperCase();
    progress.textContent = `${job.status.toUpperCase()} · ${job.stage || "Awaiting worker"}`;
    if (job.status === "running" || job.status === "queued") return;
    benchmarkBusy = false;
    benchmarkRun.disabled = !benchmarkEnabled || (benchmarkMode.value === "ai" && !aiBenchmarkEnabled);
    if (benchmarkTimer) { clearInterval(benchmarkTimer); benchmarkTimer = null; }
    const report = job.results;
    if (!report) {
      progress.textContent = `Benchmark failed: ${job.error || "No report available"}`;
      return;
    }
    if (job.mode === "ai" && report.comparison) {
      const baseline = report.baseline;
      const candidate = report.candidate;
      const tableRow = (title, item) => `<tr><th>${title}</th><td>${escapeHtml(String(item.model || report.model))} / ${escapeHtml(String(item.tile))}</td><td>${Number(item.elapsed_seconds).toFixed(2)}s</td><td>${escapeHtml(String(item.peak_pytorch_vram_mb))} MB</td><td>${escapeHtml(String(item.gpu_sampling?.gpu_utilization_percent?.mean ?? "Unavailable"))}%</td></tr>`;
      resultNode.hidden = false;
      resultNode.innerHTML = `<h3 class="benchmark-result-heading">AI quality comparison · ${escapeHtml(job.slug)} · ${job.seconds}s</h3>
        <p class="benchmark-muted">${escapeHtml(report.model)} · ${report.scale}× · ${escapeHtml(report.gpu_name)}</p>
        <table><thead><tr><th>Run</th><th>Model / Tile</th><th>Time</th><th>Peak torch VRAM</th><th>Mean GPU use</th></tr></thead><tbody>${tableRow("Baseline",baseline)}${tableRow("Candidate",candidate)}</tbody></table>
        <p>Comparison: <strong>${Number(report.comparison_rounds || 1) === 2 ? "ABBA repeated (4 runs)" : "AB (2 runs)"}</strong>. ${report.speed_status === "inconclusive" ? "Speed result is preliminary." : "Compare speed and quality together."}</p>
        <p>Candidate processing time: <strong>${Math.abs(Number(report.candidate_speedup_percent)).toFixed(1)}% ${report.candidate_speedup_percent >= 0 ? "less" : "more"}</strong> than baseline.</p>
        <p>Enhanced-output SSIM: ${report.output_ssim == null ? "Unavailable" : Number(report.output_ssim).toFixed(6)} (similarity, not proof of AI fidelity).</p>
        <p class="benchmark-muted">${escapeHtml(report.note || "")} ${escapeHtml(report.output_ssim_error || "")}</p>
        ${report.preview_frames ? `<section class="quality-viewer" aria-label="Real output quality comparison">
          <h3>Visual quality — real enhanced frames</h3>
          <p class="benchmark-muted">Compare aligned frames. In 100% detail, drag the image to inspect other areas; both models move together. The slider reveals the candidate on the right.</p>
          <div class="quality-controls"><button type="button" class="quality-choice" data-quality-view="overview">Full frame</button><button type="button" class="quality-choice" data-quality-view="detail">100% detail</button></div>
          <div class="quality-comparison" style="--reveal:50%" aria-label="Aligned enhanced images; drag to pan in detail mode">
            <img class="quality-base" src="${report.preview_frames.overview_a}" alt="Baseline enhanced output"/>
            <div class="quality-overlay"><img src="${report.preview_frames.overview_b}" alt="Candidate enhanced output"/></div>
            <div class="quality-separator" aria-hidden="true"></div>
            <span class="quality-label quality-left">Baseline: ${escapeHtml(baseline.model)}</span><span class="quality-label quality-right">Candidate: ${escapeHtml(candidate.model)}</span>
          </div>
          <label class="quality-range-label">Reveal candidate <input type="range" min="0" max="100" value="50" class="quality-range" aria-label="Comparison reveal position"/></label>
          <p class="benchmark-muted quality-pan-info">Full-frame previews are fitted to the viewport. Select 100% detail to drag across the image. This is a subjective comparison, not a ground-truth quality score.</p>
        </section>` : `<p class="benchmark-muted">Visual previews unavailable: ${escapeHtml(report.preview_error || "No preview generated")}</p>`}`;
      if (report.preview_frames) {
        const viewer = resultNode.querySelector(".quality-comparison");
        const overlay = viewer.querySelector(".quality-overlay img");
        const base = viewer.querySelector(".quality-base");
        const info = resultNode.querySelector(".quality-pan-info");
        const slider = resultNode.querySelector(".quality-range");
        const panAvailable = Boolean(report.preview_frames.pan_a && report.preview_frames.pan_b);
        let detailMode = false;
        let position = { x: 0, y: 0 };
        let drag = null;
        let imageWidth = 0;
        let imageHeight = 0;
        function panLimits() {
          const rect = viewer.getBoundingClientRect();
          return { width: rect.width, height: rect.height,
            minX: Math.min(0, rect.width - imageWidth), minY: Math.min(0, rect.height - imageHeight) };
        }
        function applyPan() {
          if (!detailMode || !panAvailable) return;
          const limits = panLimits();
          position.x = Math.max(limits.minX, Math.min(0, position.x));
          position.y = Math.max(limits.minY, Math.min(0, position.y));
          const transform = `translate3d(${position.x}px, ${position.y}px, 0)`;
          base.style.transform = transform;
          overlay.style.transform = transform;
        }
        function centerPan() {
          const limits = panLimits();
          position = { x: Math.min(0, (limits.width - imageWidth) / 2),
                       y: Math.min(0, (limits.height - imageHeight) / 2) };
          applyPan();
        }
        function updatePanGeometry() {
          if (!detailMode || !panAvailable || !base.naturalWidth) return;
          // Images use native CSS pixel dimensions, independent of devicePixelRatio.
          imageWidth = base.naturalWidth;
          imageHeight = base.naturalHeight;
          base.style.width = overlay.style.width = `${imageWidth}px`;
          base.style.height = overlay.style.height = `${imageHeight}px`;
          centerPan();
        }
        slider.addEventListener("input", (event) => viewer.style.setProperty("--reveal", `${event.target.value}%`));
        function setQualityView(detail) {
          detailMode = detail;
          drag = null;
          viewer.classList.toggle("quality-pan-enabled", detail && panAvailable);
          viewer.classList.toggle("quality-fixed-detail", detail && !panAvailable);
          base.style.transform = overlay.style.transform = "";
          base.style.width = overlay.style.width = "";
          base.style.height = overlay.style.height = "";
          const a = detail ? (panAvailable ? report.preview_frames.pan_a : report.preview_frames.detail_a) : report.preview_frames.overview_a;
          const b = detail ? (panAvailable ? report.preview_frames.pan_b : report.preview_frames.detail_b) : report.preview_frames.overview_b;
          base.src = a;
          overlay.src = b;
          info.textContent = detail
            ? (panAvailable
              ? `Drag to inspect any area of the same enhanced frame. ${report.preview_frames.pan_native ? "Native-pixel detail." : `Preview capped at ${report.preview_frames.pan_preview_width}px wide; source is larger.`} Slider still reveals the candidate.`
              : `Only the center crop is available for this benchmark; re-run to enable full-frame panning. ${report.preview_frames.pan_error || ""}`)
            : "Full-frame previews are fitted to the viewport. Select 100% detail to drag across the image.";
          resultNode.querySelectorAll("[data-quality-view]").forEach(button => {
            button.classList.toggle("active", (button.dataset.qualityView === "detail") === detail);
            button.setAttribute("aria-pressed", String((button.dataset.qualityView === "detail") === detail));
          });
          if (detail && panAvailable) {
            if (base.complete && base.naturalWidth) updatePanGeometry();
            else base.addEventListener("load", updatePanGeometry, { once: true });
          }
        }
        resultNode.querySelectorAll("[data-quality-view]").forEach(button => button.addEventListener("click", () => setQualityView(button.dataset.qualityView === "detail")));
        viewer.addEventListener("pointerdown", event => {
          if (!detailMode || !panAvailable || event.button !== 0) return;
          drag = { pointerId: event.pointerId, startX: event.clientX, startY: event.clientY,
                   imageX: position.x, imageY: position.y };
          viewer.setPointerCapture(event.pointerId);
          viewer.classList.add("quality-dragging");
          event.preventDefault();
        });
        viewer.addEventListener("pointermove", event => {
          if (!drag || drag.pointerId !== event.pointerId) return;
          position.x = drag.imageX + event.clientX - drag.startX;
          position.y = drag.imageY + event.clientY - drag.startY;
          applyPan();
        });
        function stopDrag(event) {
          if (drag && drag.pointerId === event.pointerId) {
            drag = null;
            viewer.classList.remove("quality-dragging");
            if (viewer.hasPointerCapture(event.pointerId)) viewer.releasePointerCapture(event.pointerId);
          }
        }
        viewer.addEventListener("pointerup", stopDrag);
        viewer.addEventListener("pointercancel", stopDrag);
        viewer.addEventListener("lostpointercapture", () => { drag = null; viewer.classList.remove("quality-dragging"); });
        // The preview is recreated on each benchmark result. No long-lived listeners.
        setQualityView(false);
      }
      return;
    }
    if (job.mode === "ai") {
      resultNode.hidden = false;
      resultNode.innerHTML = `<h3 class="benchmark-result-heading">AI enhancement · ${escapeHtml(job.slug)} · ${escapeHtml(job.seconds)}s sample</h3>
        <table><tbody>
        <tr><th>Model</th><td>${escapeHtml(report.model)}</td></tr>
        <tr><th>GPU</th><td>${escapeHtml(report.gpu_name)}</td></tr>
        <tr><th>Input → Output</th><td>${report.input_width}×${report.input_height} → ${report.output_width}×${report.output_height}</td></tr>
        <tr><th>AI pipeline runtime</th><td>${Number(report.ai_pipeline_seconds).toFixed(2)}s</td></tr>
        <tr><th>Processed frames/sec (estimated)</th><td>${report.processing_fps ?? "—"}</td></tr>
        <tr><th>Speed vs real-time</th><td>${report.speed_vs_realtime ?? "—"}×</td></tr>
        <tr><th>Peak PyTorch reserved VRAM</th><td>${report.peak_pytorch_vram_mb} MB</td></tr>
        <tr><th>Output file</th><td>${humanSize(report.output_bytes)}</td></tr>
        <tr><th>Source download</th><td>${report.source_download_seconds}s</td></tr>
        <tr><th>Sample preparation</th><td>${report.sample_preparation_seconds}s</td></tr>
        <tr><th colspan="2">GPU performance audit</th></tr>
        <tr><th>Model-forward wall time</th><td>${report.stage_trace?.model_forward?.module_forward_seconds == null ? "Not measured" : Number(report.stage_trace.model_forward.module_forward_seconds).toFixed(2) + "s"}</td></tr>
        <tr><th>Model-forward calls</th><td>${report.stage_trace?.model_forward?.module_forward_calls ?? "—"}</td></tr>
        <tr><th>Explicit CPU ↔ GPU transfers</th><td>${report.stage_trace?.explicit_tensor_transfers?.explicit_transfer_seconds == null ? "Not measured" : Number(report.stage_trace.explicit_tensor_transfers.explicit_transfer_seconds).toFixed(2) + "s"} (${report.stage_trace?.explicit_tensor_transfers?.explicit_transfer_calls ?? 0} calls)</td></tr>
        <tr><th>Mean GPU utilization</th><td>${report.stage_trace?.gpu_sampling?.gpu_utilization_percent?.mean == null ? "Unavailable" : report.stage_trace.gpu_sampling.gpu_utilization_percent.mean + "%"}</td></tr>
        <tr><th>Peak GPU utilization</th><td>${report.stage_trace?.gpu_sampling?.gpu_utilization_percent?.peak == null ? "Unavailable" : report.stage_trace.gpu_sampling.gpu_utilization_percent.peak + "%"}</td></tr>
        <tr><th>Peak device memory (nvidia-smi)</th><td>${report.stage_trace?.gpu_sampling?.device_memory_mb?.peak == null ? "Unavailable" : report.stage_trace.gpu_sampling.device_memory_mb.peak + " MB"}</td></tr>
        <tr><th>Model loading time</th><td>${report.stage_trace?.model_load_seconds == null ? "Not exposed by FrameLift" : report.stage_trace.model_load_seconds + "s"}</td></tr>
        <tr><th>Decode time/frame</th><td>${report.stage_trace?.source_decode_seconds_per_frame == null ? "Not exposed by FrameLift" : report.stage_trace.source_decode_seconds_per_frame + "s"}</td></tr>
        <tr><th>Output encoding time/frame</th><td>${report.stage_trace?.output_encode_seconds_per_frame == null ? "Not exposed by FrameLift" : report.stage_trace.output_encode_seconds_per_frame + "s"}</td></tr>
        <tr><th>Other / unattributed time</th><td>${report.stage_trace?.unattributed_pipeline_seconds == null ? "—" : report.stage_trace.unattributed_pipeline_seconds + "s"} (not necessarily encoding)</td></tr>
        <tr><th>Potential redundant resize</th><td>${report.resizing_audit ? (report.resizing_audit.likely_post_model_resizing ? "Possible — native " + report.resizing_audit.model_native_scale + "× model, requested " + report.resizing_audit.requested_scale + "×" : "Not indicated by model-scale check") : "Not audited"}</td></tr>
        </tbody></table><p class="benchmark-muted">${escapeHtml(report.note)} Sample preparation and source download are excluded from AI pipeline runtime. Results are measurements from the assigned CUDA worker, not estimates of AWS costs or AI visual fidelity. Model-forward and transfer hooks may slow the benchmark. The third-party FrameLift adapter cannot separately report decode, model-load, or encode time without internal instrumentation.</p>`;
      return;
    }
    const encodeResults = (report.results || []).filter((r) => r.type === "hls");
    const best = encodeResults.length ? encodeResults.reduce((a, b) => a.elapsed_seconds < b.elapsed_seconds ? a : b) : null;
    const rows = encodeResults.map((r) => `<tr><td>${escapeHtml(r.preset)}</td><td>${escapeHtml(r.quality)}</td><td>${Number(r.elapsed_seconds).toFixed(2)} s</td><td>${Number(r.processing_fps_relative_to_video).toFixed(2)}×</td><td>${humanSize(r.output_bytes)}</td><td>${r.ssim_vs_scaled_source == null ? "N/A" : Number(r.ssim_vs_scaled_source).toFixed(5)}</td></tr>`).join("");
    const errors = (report.errors || []).map((e) => `<p>${escapeHtml(e.test)}: ${escapeHtml(e.error)}</p>`).join("");
    resultNode.hidden = false;
    resultNode.innerHTML = `<h3 class="benchmark-result-heading">${escapeHtml(job.slug)} · ${escapeHtml(job.quality)} · ${escapeHtml(job.seconds)}s sample</h3>
      <p class="benchmark-muted">${best ? `Fastest tested encoder: ${escapeHtml(best.preset)} (${Number(best.elapsed_seconds).toFixed(2)}s). This is a speed result, not a recommendation to lower quality.` : "No encoding result was produced."}</p>
      <table><thead><tr><th>Preset</th><th>Quality</th><th>Encode time</th><th>Speed vs video</th><th>Output</th><th>SSIM*</th></tr></thead><tbody>${rows || '<tr><td colspan="6">No successful test</td></tr>'}</tbody></table>
      <p class="benchmark-muted">Source download: ${job.download_seconds ?? "—"}s · Sample preparation: ${report.sample_preparation_seconds ?? "—"}s. *SSIM compares encoded output with a resized source, not AI enhancement quality. Actual AWS cost and GPU utilization are not measured.</p>${errors ? `<div class="notice error">${errors}</div>` : ""}`;
  }
  async function refreshBenchmark() {
    if (!benchmarkJob) return;
    const jobId = benchmarkJob;
    const generation = benchmarkGeneration;
    try {
      const response = await fetch(`/api/benchmark/${encodeURIComponent(jobId)}`, { headers: { "X-Benchmark-Token": $("#benchmark-token").value }, cache: "no-store" });
      const data = await response.json();
      if (generation !== benchmarkGeneration || benchmarkJob !== jobId || benchmarkJobMode !== benchmarkMode.value) return;
      if (!response.ok) throw new Error(data.detail || `Benchmark status failed (${response.status})`);
      renderBenchmark(data);
    } catch (error) {
      if (generation !== benchmarkGeneration) return;
      benchmarkBusy = false;
      if (benchmarkTimer) { clearInterval(benchmarkTimer); benchmarkTimer = null; }
      benchmarkRun.disabled = false;
      $("#benchmark-progress").textContent = error.message;
    }
  }
  benchmarkForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!benchmarkEnabled || benchmarkBusy || (benchmarkMode.value === "ai" && !aiBenchmarkEnabled)) return;
    const generation = benchmarkGeneration;
    const submittedMode = benchmarkMode.value;
    benchmarkBusy = true;
    benchmarkRun.disabled = true;
    $("#benchmark-results").hidden = true;
    $("#benchmark-progress").textContent = "Submitting benchmark…";
    const presets = [...benchmarkForm.querySelectorAll('input[name="benchmark-preset"]:checked')].map((e) => e.value);
    if (benchmarkMode.value === "cpu" && !presets.length) { $("#benchmark-progress").textContent = "Select at least one encoder preset."; benchmarkRun.disabled = false; return; }
    try {
      const response = await fetch("/api/benchmark/start", {
        method: "POST", headers: { "Content-Type": "application/json", "X-Benchmark-Token": $("#benchmark-token").value },
        body: JSON.stringify({ mode: benchmarkMode.value, slug: benchmarkVideo.value, seconds: Number($("#benchmark-seconds").value), quality: $("#benchmark-quality").value, presets, model: $("#benchmark-model").value, scale: Number($("#benchmark-scale").value), tile: Number($("#benchmark-tile").value), compare: benchmarkMode.value === "ai" && benchmarkCompare.value !== "no", candidate_model: benchmarkCompare.value === "models" ? $("#benchmark-candidate-model").value : null, candidate_tile: benchmarkCompare.value === "models" ? (Number($("#benchmark-tile").value) || 384) : Number($("#benchmark-candidate-tile").value), comparison_rounds: Number($("#benchmark-comparison-rounds").value) })
      });
      const data = await response.json();
      if (generation !== benchmarkGeneration) return;
      if (!response.ok) throw new Error(data.detail || `Benchmark request failed (${response.status})`);
      benchmarkJobMode = submittedMode;
      benchmarkJob = data.id;
      renderBenchmark(data);
      benchmarkTimer = setInterval(refreshBenchmark, 2000);
      refreshBenchmark();
    } catch (error) {
      if (generation !== benchmarkGeneration) return;
      benchmarkBusy = false;
      $("#benchmark-progress").textContent = error.message;
      benchmarkRun.disabled = false;
    }
  });
  setBenchmarkMode();
  loadBenchmarkConfig();
  refreshVideos();
  state.pollTimer = window.setInterval(refreshVideos, 5000);
})();
