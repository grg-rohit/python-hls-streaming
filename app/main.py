#python-hls-streaming\app\main.py

"""
FastAPI application — multi-video HLS streaming server.

Endpoints
---------
GET    /                          -> Player page
GET    /health                    -> Health status
GET    /api/videos                -> List all videos
GET    /api/stream-info/{slug}    -> Stream information
GET    /api/stream/{slug}/{path}  -> Private B2 HLS proxy
POST   /api/upload                -> Upload and convert video
POST   /api/convert/{slug}        -> Manually trigger conversion
GET    /api/convert/{slug}/status -> Conversion status
DELETE /api/videos/{slug}         -> Delete video
GET    /static/...                -> Frontend assets
"""

from __future__ import annotations

import logging
import mimetypes
import posixpath
import shutil
import tempfile
import threading

from pathlib import Path
from typing import Any
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from app import b2_storage
from app.ffmpeg_service import (
    STREAMS_DIR,
    VIDEO_EXTENSIONS,
    convert,
    slugify,
)


# ---------------------------------------------------------------------------
# Logging and paths
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / "app" / ".env")

PUBLIC_DIR = PROJECT_ROOT / "public"

PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
STREAMS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Conversion status
# ---------------------------------------------------------------------------

_conversion_status: dict[str, dict[str, Any]] = {}

_status_lock = threading.Lock()
_conversion_lock = threading.Lock()


# ---------------------------------------------------------------------------
# B2 stream helpers
# ---------------------------------------------------------------------------

def _remote_stream_info(slug: str) -> tuple[bool, list[str]]:
    """Check whether a completed HLS stream exists in B2."""

    files = b2_storage.list_files(
        b2_storage.streams_bucket(),
        prefix=f"{slug}/",
    )

    keys = {item["Key"] for item in files}

    master_exists = f"{slug}/master.m3u8" in keys

    qualities = []

    for key in keys:
        parts = key.split("/")

        if (
            len(parts) == 3
            and parts[0] == slug
            and parts[2] == "playlist.m3u8"
        ):
            qualities.append(parts[1])

    qualities.sort(
        key=lambda name: (
            int(name.rstrip("p"))
            if name.rstrip("p").isdigit()
            else 0
        )
    )

    return master_exists and bool(qualities), qualities


def _get_status(slug: str) -> dict[str, Any]:
    with _status_lock:
        current = _conversion_status.get(slug)

    if current and current["status"] in (
        "pending",
        "converting",
        "error",
    ):
        return dict(current)

    try:
        ready, qualities = _remote_stream_info(slug)

        if ready:
            status = {
                "status": "ready",
                "qualities": qualities,
                "error": None,
            }
        else:
            status = {
                "status": "pending",
                "qualities": [],
                "error": None,
            }

    except Exception as exc:
        log.exception(
            "Could not check B2 stream status for %s",
            slug,
        )

        status = {
            "status": "error",
            "qualities": [],
            "error": str(exc),
        }

    with _status_lock:
        _conversion_status[slug] = status

    return dict(status)


def _set_status(
    slug: str,
    status: str,
    error: str | None = None,
):
    qualities = []

    if status == "ready":
        _, qualities = _remote_stream_info(slug)

    with _status_lock:
        _conversion_status[slug] = {
            "status": status,
            "qualities": qualities,
            "error": error,
        }


# ---------------------------------------------------------------------------
# Original video helpers
# ---------------------------------------------------------------------------

def _original_prefix(slug: str) -> str:
    return f"videos/{slug}/"


def _find_original(slug: str) -> dict | None:
    files = b2_storage.list_files(
        b2_storage.originals_bucket(),
        prefix=_original_prefix(slug),
    )

    files = [
        item
        for item in files
        if not item["Key"].endswith("/")
    ]

    if not files:
        return None

    return sorted(
        files,
        key=lambda item: item["Key"],
    )[0]


def _list_original_videos() -> list[dict]:
    files = b2_storage.list_files(
        b2_storage.originals_bucket(),
        prefix="videos/",
    )

    videos = []

    for item in files:
        key = item["Key"]
        parts = key.split("/", 2)

        if len(parts) != 3 or not parts[2]:
            continue

        videos.append({
            "slug": parts[1],
            "filename": parts[2],
            "title": Path(parts[2]).stem,
            "size": item.get("Size", 0),
        })

    return videos


# ---------------------------------------------------------------------------
# HLS upload helper
# ---------------------------------------------------------------------------

def _upload_hls_directory(slug: str):
    """
    Upload generated HLS files to B2.
    Upload master playlist last.
    """

    local_dir = STREAMS_DIR / slug

    if not local_dir.exists():
        raise FileNotFoundError(
            f"FFmpeg output directory not found: {local_dir}"
        )

    files = [
        path
        for path in local_dir.rglob("*")
        if path.is_file()
    ]

    master = local_dir / "master.m3u8"

    if not master.exists():
        raise RuntimeError(
            "FFmpeg did not generate master.m3u8"
        )

    files.sort(
        key=lambda path: path.name == "master.m3u8"
    )

    bucket = b2_storage.streams_bucket()

    for local_file in files:
        relative_path = (
            local_file
            .relative_to(local_dir)
            .as_posix()
        )

        object_key = f"{slug}/{relative_path}"

        extension = local_file.suffix.lower()

        if extension == ".m3u8":
            content_type = "application/vnd.apple.mpegurl"
        elif extension == ".ts":
            content_type = "video/mp2t"
        else:
            content_type, _ = mimetypes.guess_type(
                str(local_file)
            )

        log.info("Uploading HLS file: %s", object_key)

        b2_storage.upload_file(
            local_file,
            bucket,
            object_key,
            content_type=content_type,
        )

    log.info("[%s] HLS files uploaded to B2", slug)


# ---------------------------------------------------------------------------
# Conversion worker
# ---------------------------------------------------------------------------

def _run_conversion(
    slug: str,
    original_key: str,
    mode: str = "cpu",
):
    """
    Download original from private B2 storage,
    convert locally, upload HLS output to B2,
    then remove temporary files.
    """

    with _conversion_lock:
        _set_status(slug, "converting")

        log.info("[%s] Conversion started", slug)

        try:
            with tempfile.TemporaryDirectory(
                prefix=f"hls-{slug}-"
            ) as temp_dir:

                source_dir = Path(temp_dir) / "source"
                source_dir.mkdir(parents=True, exist_ok=True)

                filename = Path(original_key).name
                source = source_dir / filename

                log.info(
                    "[%s] Downloading original from B2",
                    slug,
                )

                b2_storage.download_file(
                    b2_storage.originals_bucket(),
                    original_key,
                    source,
                )

                local_output = STREAMS_DIR / slug

                if local_output.exists():
                    shutil.rmtree(local_output)

                log.info(
                    "[%s] Starting FFmpeg (%s)",
                    slug,
                    mode,
                )

                convert(
                    source=source,
                    slug=slug,
                    clean=True,
                    resume=False,
                    mode=mode,
                )

                _upload_hls_directory(slug)

                log.info(
                    "[%s] Conversion and B2 upload complete",
                    slug,
                )

            local_output = STREAMS_DIR / slug

            if local_output.exists():
                shutil.rmtree(local_output)

            _set_status(slug, "ready")

        except Exception as exc:
            log.exception("[%s] Conversion failed", slug)

            local_output = STREAMS_DIR / slug

            if local_output.exists():
                shutil.rmtree(
                    local_output,
                    ignore_errors=True,
                )

            _set_status(
                slug,
                "error",
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="HLS Streaming Server",
    version="3.1.1",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=[
        "GET",
        "HEAD",
        "POST",
        "DELETE",
        "OPTIONS",
    ],
    allow_headers=["*"],
)

app.mount(
    "/static",
    StaticFiles(
        directory=str(PUBLIC_DIR),
        check_dir=False,
    ),
    name="static",
)


# ---------------------------------------------------------------------------
# Private B2 HLS streaming
# ---------------------------------------------------------------------------

def _stream_url(slug: str, object_path: str) -> str:
    return (
        f"/api/stream/{quote(slug, safe='')}/"
        f"{quote(object_path, safe='/')}"
    )


@app.get("/api/stream/{slug}/{object_path:path}")
def stream_hls_file(
    slug: str,
    object_path: str,
):
    """
    Proxy private B2 HLS playlists and segments.

    The browser accesses this FastAPI server instead of B2.
    """

    # Restrict slug to a single safe path component.
    if (
        not slug
        or slug in (".", "..")
        or "/" in slug
        or "\\" in slug
    ):
        raise HTTPException(400, "Invalid video slug")

    object_path = object_path.lstrip("/")
    parts = object_path.split("/")

    if (
        not object_path
        or "\\" in object_path
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise HTTPException(400, "Invalid stream path")

    extension = Path(object_path).suffix.lower()

    if extension not in (".m3u8", ".ts"):
        raise HTTPException(
            400,
            "Unsupported stream file",
        )

    object_key = f"{slug}/{object_path}"

    try:
        result = b2_storage.get_object(
            b2_storage.streams_bucket(),
            object_key,
        )

        body = result["Body"]

        # ---------------------------------------------------
        # Playlist handling
        # ---------------------------------------------------

        if extension == ".m3u8":
            try:
                content = body.read().decode("utf-8")
            finally:
                body.close()

            playlist_dir = posixpath.dirname(object_path)
            rewritten_lines = []

            for line in content.splitlines():
                stripped = line.strip()

                if (
                    not stripped
                    or stripped.startswith("#")
                ):
                    rewritten_lines.append(line)
                    continue

                # HLS playlist entries should be relative paths.
                if (
                    stripped.startswith("/")
                    or "://" in stripped
                    or "\\" in stripped
                ):
                    raise HTTPException(
                        400,
                        "Unsupported playlist reference",
                    )

                resolved_path = posixpath.normpath(
                    posixpath.join(
                        playlist_dir,
                        stripped,
                    )
                )

                if (
                    resolved_path.startswith("../")
                    or resolved_path == ".."
                    or resolved_path.startswith("/")
                ):
                    raise HTTPException(
                        400,
                        "Invalid playlist reference",
                    )

                rewritten_lines.append(
                    _stream_url(slug, resolved_path)
                )

            return Response(
                content="\n".join(rewritten_lines) + "\n",
                media_type="application/vnd.apple.mpegurl",
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )

        # ---------------------------------------------------
        # TS segment handling
        # ---------------------------------------------------

        def file_iterator():
            try:
                yield from body.iter_chunks(
                    chunk_size=1024 * 1024
                )
            finally:
                body.close()

        headers = {
            "Cache-Control": (
                "public, max-age=31536000, immutable"
            ),
            "X-Content-Type-Options": "nosniff",
        }

        if result.get("ContentLength") is not None:
            headers["Content-Length"] = str(
                result["ContentLength"]
            )

        return StreamingResponse(
            file_iterator(),
            media_type="video/mp2t",
            headers=headers,
        )

    except HTTPException:
        raise

    except Exception:
        log.exception(
            "B2 streaming failed for object: %s",
            object_key,
        )

        raise HTTPException(
            502,
            "Unable to retrieve video from storage",
        )


# ---------------------------------------------------------------------------
# HLS headers
# ---------------------------------------------------------------------------

@app.middleware("http")
async def hls_headers(request: Request, call_next):
    response = await call_next(request)

    path = request.url.path

    if path.endswith(".m3u8"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Type"] = (
            "application/vnd.apple.mpegurl"
        )

    elif path.endswith(".ts"):
        response.headers["Content-Type"] = "video/mp2t"

    return response


# ---------------------------------------------------------------------------
# Basic routes
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def index():
    file_path = PUBLIC_DIR / "index.html"

    if not file_path.exists():
        raise HTTPException(
            500,
            "index.html not found",
        )

    return FileResponse(str(file_path))


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Video library
# ---------------------------------------------------------------------------

@app.get("/api/videos")
async def list_videos():
    try:
        originals = _list_original_videos()

        streams = b2_storage.list_files(
            b2_storage.streams_bucket()
        )

        stream_slugs = set()

        for item in streams:
            key = item["Key"]

            if "/" in key:
                stream_slugs.add(key.split("/", 1)[0])

        videos_by_slug = {}

        for video in originals:
            videos_by_slug[video["slug"]] = {
                **video,
                "has_source": True,
            }

        for slug in stream_slugs:
            if slug not in videos_by_slug:
                videos_by_slug[slug] = {
                    "slug": slug,
                    "filename": None,
                    "title": slug.replace("-", " ").title(),
                    "has_source": False,
                    "size": None,
                }

        videos = []

        for slug, video in videos_by_slug.items():
            status = _get_status(slug)

            videos.append({
                **video,
                "status": status["status"],
                "qualities": status["qualities"],
                "manifest_url": (
                    _stream_url(slug, "master.m3u8")
                    if status["status"] == "ready"
                    else None
                ),
            })

        videos.sort(
            key=lambda item: item["title"].lower()
        )

        return JSONResponse({"videos": videos})

    except Exception as exc:
        log.exception("Video listing failed")

        raise HTTPException(
            500,
            str(exc),
        )


# ---------------------------------------------------------------------------
# Stream information
# ---------------------------------------------------------------------------

@app.get("/api/stream-info/{slug}")
async def stream_info(slug: str):
    status = _get_status(slug)

    manifest_url = None

    if status["status"] == "ready":
        manifest_url = _stream_url(
            slug,
            "master.m3u8",
        )

    return JSONResponse({
        "slug": slug,
        "manifest_url": manifest_url,
        "qualities": status["qualities"],
        "streams_ready": status["status"] == "ready",
        "status": status["status"],
    })


# ---------------------------------------------------------------------------
# Upload video
# ---------------------------------------------------------------------------

@app.post("/api/upload")
async def upload_video(file: UploadFile = File(...)):
    filename = Path(
        file.filename or "upload.mp4"
    ).name

    extension = Path(filename).suffix.lower()

    if extension not in VIDEO_EXTENSIONS:
        raise HTTPException(
            400,
            (
                f"Unsupported file type '{extension}'. "
                f"Allowed: {', '.join(VIDEO_EXTENSIONS)}"
            ),
        )

    slug = slugify(filename)

    if _find_original(slug):
        raise HTTPException(
            409,
            f"A video with slug '{slug}' already exists.",
        )

    object_key = f"videos/{slug}/{filename}"

    log.info("Receiving upload: %s", filename)

    try:
        with tempfile.TemporaryDirectory(
            prefix="hls-upload-"
        ) as temp_dir:

            local_file = Path(temp_dir) / filename
            size = 0

            with local_file.open("wb") as output:
                while True:
                    chunk = await file.read(
                        1024 * 1024
                    )

                    if not chunk:
                        break

                    output.write(chunk)
                    size += len(chunk)

            if size == 0:
                raise HTTPException(
                    400,
                    "Uploaded file is empty.",
                )

            log.info(
                (
                    "Uploading original to private B2 bucket: "
                    "%s (%.1f MB)"
                ),
                filename,
                size / 1_048_576,
            )

            b2_storage.upload_file(
                local_file,
                b2_storage.originals_bucket(),
                object_key,
            )

        await file.close()

    except HTTPException:
        raise

    except Exception as exc:
        log.exception("Original upload failed")

        raise HTTPException(
            500,
            f"Upload to B2 failed: {exc}",
        )

    _set_status(slug, "pending")

    thread = threading.Thread(
        target=_run_conversion,
        args=(slug, object_key, "cpu"),
        daemon=True,
        name=f"convert-{slug}",
    )

    thread.start()

    return JSONResponse(
        {
            "slug": slug,
            "filename": filename,
            "status": "converting",
            "message": (
                "Original uploaded to B2. "
                "HLS conversion started."
            ),
        },
        status_code=202,
    )


# ---------------------------------------------------------------------------
# Manual conversion
# ---------------------------------------------------------------------------

@app.post("/api/convert/{slug}")
async def trigger_convert(
    slug: str,
    mode: str = "cpu",
):
    if mode not in ("cpu", "auto"):
        raise HTTPException(
            400,
            "Render supports cpu mode. Allowed: cpu, auto.",
        )

    original = _find_original(slug)

    if original is None:
        raise HTTPException(
            404,
            f"No original video found for slug '{slug}'",
        )

    current = _get_status(slug)

    if current["status"] == "converting":
        return JSONResponse({
            "slug": slug,
            "status": "converting",
            "message": "Already converting.",
        })

    _set_status(slug, "pending")

    thread = threading.Thread(
        target=_run_conversion,
        args=(slug, original["Key"], mode),
        daemon=True,
        name=f"convert-{slug}",
    )

    thread.start()

    return JSONResponse(
        {
            "slug": slug,
            "status": "converting",
            "message": "Conversion started.",
        },
        status_code=202,
    )


# ---------------------------------------------------------------------------
# Conversion status
# ---------------------------------------------------------------------------

@app.get("/api/convert/{slug}/status")
async def conversion_status(slug: str):
    status = _get_status(slug)

    manifest_url = None

    if status["status"] == "ready":
        manifest_url = _stream_url(
            slug,
            "master.m3u8",
        )

    return JSONResponse({
        "slug": slug,
        "status": status["status"],
        "qualities": status["qualities"],
        "error": status["error"],
        "manifest_url": manifest_url,
    })


# ---------------------------------------------------------------------------
# Delete video
# ---------------------------------------------------------------------------

@app.delete("/api/videos/{slug}")
async def delete_video(
    slug: str,
    delete_source: bool = False,
):
    try:
        b2_storage.delete_prefix(
            b2_storage.streams_bucket(),
            f"{slug}/",
        )

        if delete_source:
            b2_storage.delete_prefix(
                b2_storage.originals_bucket(),
                _original_prefix(slug),
            )

        with _status_lock:
            _conversion_status.pop(slug, None)

        return JSONResponse({
            "slug": slug,
            "deleted": True,
            "source_deleted": delete_source,
        })

    except Exception as exc:
        log.exception("Delete failed for %s", slug)

        raise HTTPException(
            500,
            str(exc),
        )


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_banner():
    try:
        originals = _list_original_videos()

        log.info("=" * 55)
        log.info("  HLS Streaming Server v3.1.1 - Backblaze B2")
        log.info(
            "  Original bucket : %s",
            b2_storage.originals_bucket(),
        )
        log.info(
            "  Streams bucket  : %s",
            b2_storage.streams_bucket(),
        )
        log.info(
            "  Originals       : %d",
            len(originals),
        )
        log.info("  Storage         : Private Backblaze B2")
        log.info("  Playback        : FastAPI HLS proxy")
        log.info("=" * 55)

    except Exception:
        log.exception("B2 startup check failed")
        raise



# from __future__ import annotations

# import logging
# import os
# import shutil
# import tempfile
# import threading

# from pathlib import Path
# from typing import Any

# from fastapi import (
#     FastAPI,
#     HTTPException,
#     Request,
#     UploadFile,
#     File,
# )
# from fastapi.responses import FileResponse, JSONResponse
# from fastapi.staticfiles import StaticFiles
# from fastapi.middleware.cors import CORSMiddleware

# from app import b2_storage
# from app.ffmpeg_service import (
#     STREAMS_DIR,
#     slugify,
#     convert,
#     VIDEO_EXTENSIONS,
# )


# # ---------------------------------------------------------------------------
# # Logging
# # ---------------------------------------------------------------------------
# logging.basicConfig(
#     level=logging.INFO,
#     format="%(asctime)s  %(levelname)-8s  %(message)s",
#     datefmt="%H:%M:%S",
# )
# log = logging.getLogger(__name__)

# # ---------------------------------------------------------------------------
# # Paths
# # ---------------------------------------------------------------------------
# PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
# PUBLIC_DIR:   Path = PROJECT_ROOT / "public"

# # ---------------------------------------------------------------------------
# # In-memory conversion status store
# # slug → { status, error, qualities }
# # status: "pending" | "converting" | "ready" | "error"
# # ---------------------------------------------------------------------------
# _conversion_status: dict[str, dict[str, Any]] = {}
# _status_lock = threading.Lock()


# def _get_status(slug: str) -> dict[str, Any]:
#     with _status_lock:
#         if slug not in _conversion_status:
#             # Derive initial status from disk
#             if streams_ready(slug):
#                 _conversion_status[slug] = {
#                     "status": "ready",
#                     "qualities": available_qualities(slug),
#                     "error": None,
#                 }
#             else:
#                 _conversion_status[slug] = {"status": "pending", "qualities": [], "error": None}
#         return dict(_conversion_status[slug])


# def _set_status(slug: str, status: str, error: str | None = None) -> None:
#     with _status_lock:
#         _conversion_status[slug] = {
#             "status": status,
#             "qualities": available_qualities(slug) if status == "ready" else [],
#             "error": error,
#         }


# # ---------------------------------------------------------------------------
# # App
# # ---------------------------------------------------------------------------
# app = FastAPI(title="HLS Streaming Server", version="2.0.0")

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_methods=["GET", "HEAD", "POST", "OPTIONS"],
#     allow_headers=["*"],
# )

# # ---------------------------------------------------------------------------
# # Static mounts — must come before routes that catch-all
# # ---------------------------------------------------------------------------
# STREAMS_DIR.mkdir(parents=True, exist_ok=True)
# VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
# PUBLIC_DIR.mkdir(parents=True, exist_ok=True)

# app.mount("/streams", StaticFiles(directory=str(STREAMS_DIR), check_dir=False), name="streams")
# app.mount("/static",  StaticFiles(directory=str(PUBLIC_DIR),  check_dir=False), name="static")


# # ---------------------------------------------------------------------------
# # HLS MIME + no-cache middleware
# # ---------------------------------------------------------------------------
# @app.middleware("http")
# async def hls_headers(request: Request, call_next):
#     response = await call_next(request)
#     path = request.url.path
#     if path.endswith(".m3u8"):
#         response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
#         response.headers["Pragma"]        = "no-cache"
#         response.headers["Expires"]       = "0"
#         response.headers["Content-Type"]  = "application/vnd.apple.mpegurl"
#     elif path.endswith(".ts"):
#         response.headers["Content-Type"]  = "video/mp2t"
#     return response


# # ---------------------------------------------------------------------------
# # Background conversion worker
# # ---------------------------------------------------------------------------
# def _run_conversion(slug: str, source: Path, mode: str = "auto") -> None:
#     """Called in a background thread. Updates _conversion_status when done."""
#     _set_status(slug, "converting")
#     log.info(f"[{slug}] Conversion started (mode={mode})")
#     try:
#         convert(source=source, slug=slug, clean=True, resume=False, mode=mode)
#         _set_status(slug, "ready")
#         log.info(f"[{slug}] Conversion complete")
#     except Exception as exc:
#         _set_status(slug, "error", error=str(exc))
#         log.error(f"[{slug}] Conversion failed: {exc}")


# # ---------------------------------------------------------------------------
# # Routes
# # ---------------------------------------------------------------------------

# @app.get("/", include_in_schema=False)
# async def index() -> FileResponse:
#     f = PUBLIC_DIR / "index.html"
#     if not f.exists():
#         raise HTTPException(500, "index.html not found")
#     return FileResponse(str(f))


# @app.get("/health")
# async def health() -> JSONResponse:
#     return JSONResponse({"status": "ok"})


# # ── Video library ─────────────────────────────────────────────────────────

# @app.get("/api/videos")
# async def list_videos() -> JSONResponse:
#     """
#     Return all videos in videos/ with their stream status.
#     Also includes videos that only have streams (source deleted).
#     """
#     videos: list[dict] = []

#     # Videos with source files
#     seen_slugs: set[str] = set()
#     for video in find_all_videos():
#         slug   = slugify(video.name)
#         status = _get_status(slug)
#         seen_slugs.add(slug)
#         videos.append({
#             "slug":       slug,
#             "filename":   video.name,
#             "title":      video.stem,
#             "has_source": True,
#             "status":     status["status"],
#             "qualities":  status["qualities"],
#             "manifest_url": f"/streams/{slug}/master.m3u8" if status["status"] == "ready" else None,
#         })

#     # Slugs that have streams but source was deleted
#     from app.ffmpeg_service import STREAMS_DIR as SD
#     if SD.exists():
#         for d in sorted(SD.iterdir()):
#             if d.is_dir() and d.name not in seen_slugs and (d / "master.m3u8").exists():
#                 slug = d.name
#                 videos.append({
#                     "slug":       slug,
#                     "filename":   None,
#                     "title":      slug.replace("-", " ").title(),
#                     "has_source": False,
#                     "status":     "ready",
#                     "qualities":  available_qualities(slug),
#                     "manifest_url": f"/streams/{slug}/master.m3u8",
#                 })

#     return JSONResponse({"videos": videos})


# @app.get("/api/stream-info/{slug}")
# async def stream_info(slug: str) -> JSONResponse:
#     status = _get_status(slug)
#     return JSONResponse({
#         "slug":         slug,
#         "manifest_url": f"/streams/{slug}/master.m3u8",
#         "qualities":    status["qualities"],
#         "streams_ready": status["status"] == "ready",
#         "status":       status["status"],
#     })


# # ── Upload ────────────────────────────────────────────────────────────────

# @app.post("/api/upload")
# async def upload_video(file: UploadFile = File(...)) -> JSONResponse:
#     """
#     Accept a video file upload, save it to videos/, and automatically
#     trigger HLS conversion in a background thread.
#     """
#     filename = file.filename or "upload.mp4"
#     ext      = Path(filename).suffix.lower()

#     from app.ffmpeg_service import VIDEO_EXTENSIONS
#     if ext not in VIDEO_EXTENSIONS:
#         raise HTTPException(400, f"Unsupported file type '{ext}'. Allowed: {', '.join(VIDEO_EXTENSIONS)}")

#     dest = VIDEOS_DIR / filename
#     log.info(f"Receiving upload: {filename}")

#     # Stream the upload to disk in 1 MB chunks to handle large files
#     try:
#         with dest.open("wb") as f:
#             while True:
#                 chunk = await file.read(1024 * 1024)
#                 if not chunk:
#                     break
#                 f.write(chunk)
#     except Exception as exc:
#         dest.unlink(missing_ok=True)
#         raise HTTPException(500, f"Upload failed: {exc}")

#     log.info(f"Saved {filename} ({dest.stat().st_size / 1_048_576:.1f} MB)")

#     slug = slugify(filename)
#     _set_status(slug, "pending")

#     # Auto-trigger conversion in a background thread
#     thread = threading.Thread(
#         target=_run_conversion,
#         args=(slug, dest, "auto"),
#         daemon=True,
#         name=f"convert-{slug}",
#     )
#     thread.start()

#     return JSONResponse({
#         "slug":     slug,
#         "filename": filename,
#         "status":   "converting",
#         "message":  "Upload complete. Conversion started automatically.",
#     }, status_code=202)


# # ── Conversion control ────────────────────────────────────────────────────

# @app.post("/api/convert/{slug}")
# async def trigger_convert(slug: str, mode: str = "auto") -> JSONResponse:
#     """
#     Manually trigger (or re-trigger) conversion for a slug.
#     Useful for re-encoding after changing quality settings.
#     """
#     source = find_video_by_slug(slug)
#     if source is None:
#         raise HTTPException(404, f"No source video found for slug '{slug}'")

#     current = _get_status(slug)
#     if current["status"] == "converting":
#         return JSONResponse({"slug": slug, "status": "converting", "message": "Already converting."})

#     _set_status(slug, "pending")
#     thread = threading.Thread(
#         target=_run_conversion,
#         args=(slug, source, mode),
#         daemon=True,
#         name=f"convert-{slug}",
#     )
#     thread.start()

#     return JSONResponse({"slug": slug, "status": "converting", "message": "Conversion started."}, status_code=202)


# @app.get("/api/convert/{slug}/status")
# async def conversion_status(slug: str) -> JSONResponse:
#     status = _get_status(slug)
#     return JSONResponse({
#         "slug":      slug,
#         "status":    status["status"],
#         "qualities": status["qualities"],
#         "error":     status["error"],
#         "manifest_url": f"/streams/{slug}/master.m3u8" if status["status"] == "ready" else None,
#     })


# # ── Delete video ──────────────────────────────────────────────────────────

# @app.delete("/api/videos/{slug}")
# async def delete_video(slug: str, delete_source: bool = False) -> JSONResponse:
#     """
#     Delete the HLS streams for a slug.
#     Optionally also deletes the source file (delete_source=true).
#     """
#     import shutil as _shutil
#     sd = STREAMS_DIR / slug
#     if sd.exists():
#         _shutil.rmtree(sd)

#     if delete_source:
#         source = find_video_by_slug(slug)
#         if source and source.exists():
#             source.unlink()

#     with _status_lock:
#         _conversion_status.pop(slug, None)

#     return JSONResponse({"slug": slug, "deleted": True})


# # ---------------------------------------------------------------------------
# # Startup banner
# # ---------------------------------------------------------------------------

# @app.on_event("startup")
# async def startup_banner() -> None:
#     videos  = find_all_videos()
#     ready   = [slugify(v.name) for v in videos if streams_ready(slugify(v.name))]
#     log.info("=" * 55)
#     log.info("  HLS Streaming Server v2  (multi-video)")
#     log.info("  Player   : http://localhost:8000")
#     log.info("  Health   : http://localhost:8000/health")
#     log.info("  Videos   : http://localhost:8000/api/videos")
#     log.info(f"  Sources  : {len(videos)} video(s) in videos/")
#     log.info(f"  Ready    : {len(ready)} stream(s) converted")
#     if not videos:
#         log.warning("  Upload a video at http://localhost:8000 to get started")
#     log.info("=" * 55)
