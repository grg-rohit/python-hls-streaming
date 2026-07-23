/**
 * upload.js — drag-and-drop / file-picker upload handler
 *
 * Flow
 * ────
 * 1. User drops or picks a video file
 * 2. XMLHttpRequest uploads to POST /api/upload (shows progress bar)
 * 3. Server saves the file, starts conversion in a background thread
 * 4. UI transitions to "Converting…" state
 * 5. Polls /api/convert/<slug>/status every 2 seconds
 * 6. On "ready" → refreshes the library grid
 */

(function () {
  "use strict";

  const zone        = document.getElementById("upload-zone");
  const fileInput   = document.getElementById("file-input");
  const progressWrap= document.getElementById("upload-progress-wrap");
  const uploadBar   = document.getElementById("upload-bar");
  const uploadPct   = document.getElementById("upload-percent");
  const uploadName  = document.getElementById("upload-filename");
  const uploadStatus= document.getElementById("upload-status-text");

  const ALLOWED_EXT = new Set([".mp4", ".mkv", ".mov", ".avi", ".webm"]);

  // ── Drop zone interactions ────────────────────────────────────────────────
  zone.addEventListener("click", () => fileInput.click());
  zone.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") fileInput.click(); });

  zone.addEventListener("dragover", e => {
    e.preventDefault();
    zone.classList.add("drag-over");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
  zone.addEventListener("drop", e => {
    e.preventDefault();
    zone.classList.remove("drag-over");
    const file = e.dataTransfer?.files?.[0];
    if (file) handleFile(file);
  });

  fileInput.addEventListener("change", () => {
    const file = fileInput.files?.[0];
    if (file) handleFile(file);
    fileInput.value = "";   // reset so same file can be re-picked
  });

  // ── File validation + upload ───────────────────────────────────────────────
  function handleFile(file) {
    const ext = "." + file.name.split(".").pop().toLowerCase();
    if (!ALLOWED_EXT.has(ext)) {
      alert(`Unsupported file type: ${ext}\nAllowed: ${[...ALLOWED_EXT].join(", ")}`);
      return;
    }

    showProgress(file.name);
    uploadFile(file);
  }

  function showProgress(filename) {
    uploadName.textContent  = filename;
    uploadPct.textContent   = "0%";
    uploadStatus.textContent = "Uploading…";
    setBarWidth(0);
    zone.classList.add("hidden");
    progressWrap.classList.remove("hidden");
  }

  function setBarWidth(pct) {
    uploadBar.style.width = pct + "%";
  }

  function setUploadState(text, pct) {
    uploadStatus.textContent = text;
    if (pct !== undefined) {
      uploadPct.textContent = pct + "%";
      setBarWidth(pct);
    }
  }

  function resetUploadZone() {
    progressWrap.classList.add("hidden");
    zone.classList.remove("hidden");
    setBarWidth(0);
    uploadPct.textContent    = "0%";
    uploadStatus.textContent = "Uploading…";
    uploadName.textContent   = "";
  }

  function uploadFile(file) {
    const formData = new FormData();
    formData.append("file", file, file.name);

    const xhr = new XMLHttpRequest();

    xhr.upload.addEventListener("progress", e => {
      if (!e.lengthComputable) return;
      const pct = Math.round((e.loaded / e.total) * 100);
      setUploadState("Uploading…", pct);
    });

    xhr.addEventListener("load", () => {
      if (xhr.status === 200 || xhr.status === 202) {
        let data;
        try { data = JSON.parse(xhr.responseText); } catch { data = {}; }

        setUploadState("Upload complete — converting…", 100);
        uploadBar.classList.add("bar-converting");

        // Refresh library immediately (shows "Converting…" card)
        if (window.hlsPlayer) window.hlsPlayer.loadLibrary();

        // After 3 seconds, reset the upload zone and let the library card
        // take over showing conversion progress
        setTimeout(() => {
          resetUploadZone();
          uploadBar.classList.remove("bar-converting");
        }, 3000);

      } else {
        let msg = "Upload failed";
        try { msg = JSON.parse(xhr.responseText).detail || msg; } catch {}
        setUploadState("Error: " + msg, 0);
        uploadBar.classList.add("bar-error");
        setTimeout(() => {
          resetUploadZone();
          uploadBar.classList.remove("bar-error");
        }, 4000);
      }
    });

    xhr.addEventListener("error", () => {
      setUploadState("Network error — upload failed.", 0);
      setTimeout(resetUploadZone, 4000);
    });

    xhr.addEventListener("abort", () => {
      setUploadState("Upload cancelled.", 0);
      setTimeout(resetUploadZone, 2000);
    });

    xhr.open("POST", "/api/upload");
    xhr.send(formData);
  }

})();
