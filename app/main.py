"""
FastAPI application — multi-video HLS streaming server.

Endpoints
─────────
GET  /                          → player page
GET  /health                    → { status: ok }
GET  /api/videos                → list all videos with stream status
GET  /api/stream-info/{slug}    → per-video manifest URL + qualities
POST /api/upload                → upload a video file (triggers auto-convert)
POST /api/convert/{slug}        → manually re-trigger conversion for a slug
GET  /api/convert/{slug}/status → { status: pending|converting|ready|error, ... }
GET  /streams/<slug>/...        → HLS playlists and TS segments
GET  /static/...                → frontend assets
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.ffmpeg_service import (
    VIDEOS_DIR,
    STREAMS_DIR,
    slugify,
    find_all_videos,
    find_video_by_slug,
    streams_ready,
    available_qualities,
    convert,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
PUBLIC_DIR:   Path = PROJECT_ROOT / "public"

# ---------------------------------------------------------------------------
# In-memory conversion status store
# slug → { status, error, qualities }
# status: "pending" | "converting" | "ready" | "error"
# ---------------------------------------------------------------------------
_conversion_status: dict[str, dict[str, Any]] = {}
_status_lock = threading.Lock()


def _get_status(slug: str) -> dict[str, Any]:
    with _status_lock:
        if slug not in _conversion_status:
            # Derive initial status from disk
            if streams_ready(slug):
                _conversion_status[slug] = {
                    "status": "ready",
                    "qualities": available_qualities(slug),
                    "error": None,
                }
            else:
                _conversion_status[slug] = {"status": "pending", "qualities": [], "error": None}
        return dict(_conversion_status[slug])


def _set_status(slug: str, status: str, error: str | None = None) -> None:
    with _status_lock:
        _conversion_status[slug] = {
            "status": status,
            "qualities": available_qualities(slug) if status == "ready" else [],
            "error": error,
        }


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="HLS Streaming Server", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Static mounts — must come before routes that catch-all
# ---------------------------------------------------------------------------
STREAMS_DIR.mkdir(parents=True, exist_ok=True)
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
PUBLIC_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/streams", StaticFiles(directory=str(STREAMS_DIR), check_dir=False), name="streams")
app.mount("/static",  StaticFiles(directory=str(PUBLIC_DIR),  check_dir=False), name="static")


# ---------------------------------------------------------------------------
# HLS MIME + no-cache middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def hls_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.endswith(".m3u8"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"]        = "no-cache"
        response.headers["Expires"]       = "0"
        response.headers["Content-Type"]  = "application/vnd.apple.mpegurl"
    elif path.endswith(".ts"):
        response.headers["Content-Type"]  = "video/mp2t"
    return response


# ---------------------------------------------------------------------------
# Background conversion worker
# ---------------------------------------------------------------------------
def _run_conversion(slug: str, source: Path, mode: str = "auto") -> None:
    """Called in a background thread. Updates _conversion_status when done."""
    _set_status(slug, "converting")
    log.info(f"[{slug}] Conversion started (mode={mode})")
    try:
        convert(source=source, slug=slug, clean=True, resume=False, mode=mode)
        _set_status(slug, "ready")
        log.info(f"[{slug}] Conversion complete")
    except Exception as exc:
        _set_status(slug, "error", error=str(exc))
        log.error(f"[{slug}] Conversion failed: {exc}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    f = PUBLIC_DIR / "index.html"
    if not f.exists():
        raise HTTPException(500, "index.html not found")
    return FileResponse(str(f))


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ── Video library ─────────────────────────────────────────────────────────

@app.get("/api/videos")
async def list_videos() -> JSONResponse:
    """
    Return all videos in videos/ with their stream status.
    Also includes videos that only have streams (source deleted).
    """
    videos: list[dict] = []

    # Videos with source files
    seen_slugs: set[str] = set()
    for video in find_all_videos():
        slug   = slugify(video.name)
        status = _get_status(slug)
        seen_slugs.add(slug)
        videos.append({
            "slug":       slug,
            "filename":   video.name,
            "title":      video.stem,
            "has_source": True,
            "status":     status["status"],
            "qualities":  status["qualities"],
            "manifest_url": f"/streams/{slug}/master.m3u8" if status["status"] == "ready" else None,
        })

    # Slugs that have streams but source was deleted
    from app.ffmpeg_service import STREAMS_DIR as SD
    if SD.exists():
        for d in sorted(SD.iterdir()):
            if d.is_dir() and d.name not in seen_slugs and (d / "master.m3u8").exists():
                slug = d.name
                videos.append({
                    "slug":       slug,
                    "filename":   None,
                    "title":      slug.replace("-", " ").title(),
                    "has_source": False,
                    "status":     "ready",
                    "qualities":  available_qualities(slug),
                    "manifest_url": f"/streams/{slug}/master.m3u8",
                })

    return JSONResponse({"videos": videos})


@app.get("/api/stream-info/{slug}")
async def stream_info(slug: str) -> JSONResponse:
    status = _get_status(slug)
    return JSONResponse({
        "slug":         slug,
        "manifest_url": f"/streams/{slug}/master.m3u8",
        "qualities":    status["qualities"],
        "streams_ready": status["status"] == "ready",
        "status":       status["status"],
    })


# ── Upload ────────────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload_video(file: UploadFile = File(...)) -> JSONResponse:
    """
    Accept a video file upload, save it to videos/, and automatically
    trigger HLS conversion in a background thread.
    """
    filename = file.filename or "upload.mp4"
    ext      = Path(filename).suffix.lower()

    from app.ffmpeg_service import VIDEO_EXTENSIONS
    if ext not in VIDEO_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type '{ext}'. Allowed: {', '.join(VIDEO_EXTENSIONS)}")

    dest = VIDEOS_DIR / filename
    log.info(f"Receiving upload: {filename}")

    # Stream the upload to disk in 1 MB chunks to handle large files
    try:
        with dest.open("wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(500, f"Upload failed: {exc}")

    log.info(f"Saved {filename} ({dest.stat().st_size / 1_048_576:.1f} MB)")

    slug = slugify(filename)
    _set_status(slug, "pending")

    # Auto-trigger conversion in a background thread
    thread = threading.Thread(
        target=_run_conversion,
        args=(slug, dest, "auto"),
        daemon=True,
        name=f"convert-{slug}",
    )
    thread.start()

    return JSONResponse({
        "slug":     slug,
        "filename": filename,
        "status":   "converting",
        "message":  "Upload complete. Conversion started automatically.",
    }, status_code=202)


# ── Conversion control ────────────────────────────────────────────────────

@app.post("/api/convert/{slug}")
async def trigger_convert(slug: str, mode: str = "auto") -> JSONResponse:
    """
    Manually trigger (or re-trigger) conversion for a slug.
    Useful for re-encoding after changing quality settings.
    """
    source = find_video_by_slug(slug)
    if source is None:
        raise HTTPException(404, f"No source video found for slug '{slug}'")

    current = _get_status(slug)
    if current["status"] == "converting":
        return JSONResponse({"slug": slug, "status": "converting", "message": "Already converting."})

    _set_status(slug, "pending")
    thread = threading.Thread(
        target=_run_conversion,
        args=(slug, source, mode),
        daemon=True,
        name=f"convert-{slug}",
    )
    thread.start()

    return JSONResponse({"slug": slug, "status": "converting", "message": "Conversion started."}, status_code=202)


@app.get("/api/convert/{slug}/status")
async def conversion_status(slug: str) -> JSONResponse:
    status = _get_status(slug)
    return JSONResponse({
        "slug":      slug,
        "status":    status["status"],
        "qualities": status["qualities"],
        "error":     status["error"],
        "manifest_url": f"/streams/{slug}/master.m3u8" if status["status"] == "ready" else None,
    })


# ── Delete video ──────────────────────────────────────────────────────────

@app.delete("/api/videos/{slug}")
async def delete_video(slug: str, delete_source: bool = False) -> JSONResponse:
    """
    Delete the HLS streams for a slug.
    Optionally also deletes the source file (delete_source=true).
    """
    import shutil as _shutil
    sd = STREAMS_DIR / slug
    if sd.exists():
        _shutil.rmtree(sd)

    if delete_source:
        source = find_video_by_slug(slug)
        if source and source.exists():
            source.unlink()

    with _status_lock:
        _conversion_status.pop(slug, None)

    return JSONResponse({"slug": slug, "deleted": True})


# ---------------------------------------------------------------------------
# Startup banner
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_banner() -> None:
    videos  = find_all_videos()
    ready   = [slugify(v.name) for v in videos if streams_ready(slugify(v.name))]
    log.info("=" * 55)
    log.info("  HLS Streaming Server v2  (multi-video)")
    log.info("  Player   : http://localhost:8000")
    log.info("  Health   : http://localhost:8000/health")
    log.info("  Videos   : http://localhost:8000/api/videos")
    log.info(f"  Sources  : {len(videos)} video(s) in videos/")
    log.info(f"  Ready    : {len(ready)} stream(s) converted")
    if not videos:
        log.warning("  Upload a video at http://localhost:8000 to get started")
    log.info("=" * 55)
