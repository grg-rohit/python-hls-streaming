#python-hls-streaming\app\main.py
"""FastAPI API for S3-backed original video uploads, progressive HLS and workers."""
from __future__ import annotations
import hmac
import logging
from contextlib import asynccontextmanager
import mimetypes
import os
import posixpath
import re
import threading
from base64 import b64decode
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, Body
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from app.upload_limits import UploadLimitsMiddleware

from app import repository, s3_storage, benchmark_service, ai_benchmark_service
from app.ffmpeg_service import FULL_LADDER, ai_upscale_targets, build_quality_ladder, sort_qualities

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT / "app" / ".env")
PUBLIC_DIR = PROJECT_ROOT / "public"
PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
log = logging.getLogger("hls.api")
ALLOWED_MODES = {"auto", "cpu", "gpu", "hybrid"}
SLUG_RE = re.compile(r"^[\w-]{1,180}$", re.UNICODE)
JOB_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
QUALITY_RE = re.compile(r"^(240|360|480|720|1080|1440|2160)p$")
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        imported = await run_in_threadpool(_sync_existing_originals)
        log.info("HLS API v5.1 started; %d video(s) registered (%d imported)", len(await run_in_threadpool(repository.list_videos)), imported)
    except Exception:
        log.exception("S3 startup synchronization failed")
        raise
    yield


app = FastAPI(title="HLS Streaming Server", version="5.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(PUBLIC_DIR), check_dir=False), name="static")


def _validate_slug(slug: str) -> str:
    if not SLUG_RE.fullmatch(slug) or slug in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid video slug")
    return slug


def _safe_filename(name: str | None) -> str:
    raw = (name or "upload.mp4").replace("\\", "/").split("/")[-1].strip().replace("\x00", "")
    return raw or "upload.mp4"


def _worker_authorized(request: Request) -> None:
    expected = os.getenv("WORKER_SHARED_TOKEN", "")
    supplied = request.headers.get("x-worker-token", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Worker authentication is not configured")
    if not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="Invalid worker token")


def _lease_token(request: Request) -> str:
    token = request.headers.get("x-job-lease", "")
    if not JOB_ID_RE.fullmatch(token):
        raise HTTPException(status_code=409, detail="A valid job lease is required")
    return token


_upload_lock = threading.Lock()
_upload_slugs: set[str] = set()


def _app_auth_enabled() -> bool:
    return bool(os.getenv("APP_USERNAME") and os.getenv("APP_PASSWORD"))


@app.middleware("http")
async def optional_basic_auth(request: Request, call_next):
    path = request.url.path
    if os.getenv("REQUIRE_APP_AUTH", "false").lower() == "true" and not _app_auth_enabled() and path != "/health" and not path.startswith("/api/worker/"):
        return JSONResponse({"detail": "Application authentication is not configured"}, status_code=503)
    if _app_auth_enabled() and path != "/health" and not path.startswith("/api/worker/"):
        auth = request.headers.get("authorization", "")
        valid = False
        if auth.startswith("Basic "):
            try:
                username, password = b64decode(auth.split(" ", 1)[1]).decode("utf-8").split(":", 1)
                valid = hmac.compare_digest(username, os.environ["APP_USERNAME"]) and hmac.compare_digest(password, os.environ["APP_PASSWORD"])
            except (ValueError, UnicodeDecodeError):
                pass
        if not valid:
            return JSONResponse({"detail": "Authentication required"}, status_code=401, headers={"WWW-Authenticate": 'Basic realm="HLS Streaming"'})
    return await call_next(request)


@app.middleware("http")
async def response_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/api/") or path.endswith(".m3u8"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    if path.endswith(".m3u8"):
        response.headers["Content-Type"] = "application/vnd.apple.mpegurl"
    elif path.endswith(".ts"):
        response.headers["Content-Type"] = "video/mp2t"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


def _master_details(object_key: str) -> list[str] | None:
    try:
        result = s3_storage.get_object(s3_storage.streams_bucket(), object_key)
        try:
            body = result["Body"].read().decode("utf-8", errors="replace")
        finally:
            result["Body"].close()
        qualities = re.findall(r'NAME="([^\"]+)"', body)
        return qualities or None
    except Exception as exc:
        log.debug("No master playlist at %s: %s", object_key, exc)
        return None


def _discover_existing_stream(slug: str) -> tuple[str, list[str]] | None:
    legacy = _master_details(f"{slug}/master.m3u8")
    if legacy:
        return "", legacy
    candidates = [item for item in s3_storage.list_files(s3_storage.streams_bucket(), prefix=f"{slug}/jobs/") if str(item.get("Key", "")).endswith("/master.m3u8")]
    candidates.sort(key=lambda item: str(item.get("LastModified") or ""), reverse=True)
    for item in candidates:
        key = str(item["Key"])
        qualities = _master_details(key)
        if qualities:
            prefix = key[len(slug) + 1:-len("/master.m3u8")]
            return prefix, qualities
    return None


def _sync_existing_originals() -> int:
    records = s3_storage.list_files(s3_storage.originals_bucket(), prefix="videos/")
    imported = 0
    for item in records:
        key = str(item.get("Key", ""))
        parts = key.split("/", 2)
        if len(parts) != 3 or parts[0] != "videos" or not parts[2] or key.endswith("/"):
            continue
        slug, relative_filename = parts[1], parts[2]
        if not SLUG_RE.fullmatch(slug):
            continue
        filename = Path(relative_filename).name
        if repository.import_legacy_video(slug, filename, key, int(item.get("Size", 0))):
            imported += 1
            existing = _discover_existing_stream(slug)
            if existing:
                prefix, qualities = existing
                repository.set_legacy_streams(slug, qualities, prefix)
                log.info("Recovered HLS output for %s: %s", slug, prefix or "legacy root")
    return imported


def _stream_url(slug: str, object_path: str = "master.m3u8") -> str:
    return f"/api/stream/{quote(slug, safe='')}/{quote(object_path.lstrip('/'), safe='/')}"


def _ai_targets_for_video(video: dict, scale: int = 2) -> list[str]:
    width, height = video.get("source_width"), video.get("source_height")
    if not width or not height:
        return []
    try:
        return ai_upscale_targets(int(width), int(height), scale)
    except (TypeError, ValueError):
        return []


def _video_payload(video: dict) -> dict:
    job = video.get("latest_job") or None
    job_status = job.get("status") if job else None
    qualities = sort_qualities(list(video.get("qualities") or []))
    has_streams = bool(video.get("has_streams") and qualities)
    source_width = video.get("source_width")
    source_height = video.get("source_height")
    native_required = [item["name"] for item in build_quality_ladder(int(source_width), int(source_height))] if source_width and source_height else []
    native_complete = bool(native_required) and set(native_required).issubset(set(qualities))
    if job_status in {"queued", "processing"}:
        status = job_status
    elif native_complete or (job_status == "ready" and has_streams):
        status = "ready"
    elif job_status == "failed":
        status = "failed"
    elif has_streams:
        status = "ready"
    else:
        status = "original_only"
    ai_targets_by_scale = {
        str(scale): [quality for quality in _ai_targets_for_video(video, scale) if quality not in qualities]
        for scale in (2, 3, 4)
    }
    ai_targets = ai_targets_by_scale["2"]
    available_ai_targets = any(ai_targets_by_scale.values())
    return {
        "slug": video["slug"],
        "filename": video["filename"],
        "title": Path(video["filename"]).stem,
        "size": int(video.get("size_bytes", 0)),
        "has_source": bool(video.get("original_key")),
        "original_url": f"/api/original/{quote(video['slug'], safe='')}",
        "has_streams": has_streams,
        "can_play": bool(video.get("original_key")),
        "status": status,
        "qualities": qualities,
        "manifest_url": _stream_url(video["slug"], "master.m3u8") if has_streams else None,
        "source_width": source_width,
        "source_height": source_height,
        "source_quality": f"{min(int(source_width), int(source_height))}p" if source_width and source_height else None,
        "native_qualities": native_required,
        "native_complete": native_complete,
        "highest_hls_quality": qualities[-1] if qualities else None,
        "ai_upscale_available": bool(available_ai_targets),
        "ai_upscale_targets": ai_targets,
        "ai_upscale_targets_by_scale": ai_targets_by_scale,
        "job_id": job.get("id") if job else None,
        "job_status": job_status,
        "job_type": job.get("job_type") if job else None,
        "job_options": job.get("options", {}) if job else {},
        "job_error": job.get("error") if job else None,
        "updated_at": video.get("updated_at"),
    }


@app.get("/", include_in_schema=False)
async def index():
    page = PUBLIC_DIR / "index.html"
    if not page.is_file():
        raise HTTPException(status_code=500, detail="public/index.html is missing")
    return FileResponse(page)


@app.get("/health")
async def health():
    return {"status": "ok", "version": "5.1.0", "metadata_store": "s3"}


@app.get("/api/videos")
def list_videos():
    return {"videos": [_video_payload(video) for video in repository.list_videos()]}


@app.get("/api/stream-info/{slug}")
def stream_info(slug: str):
    _validate_slug(slug)
    video = repository.get_video(slug)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return _video_payload(video)


@app.get("/api/original/{slug}")
def stream_original(slug: str, request: Request):
    """Proxy the original from S3, including byte-range support for seeking."""
    _validate_slug(slug)
    video = repository.get_video(slug)
    if not video or not video.get("original_key"):
        raise HTTPException(status_code=404, detail="Original video not found")
    filename = str(video.get("filename") or "video.mp4")
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    byte_range = request.headers.get("range")
    try:
        result = s3_storage.get_object(s3_storage.originals_bucket(), str(video["original_key"]), byte_range=byte_range)
        body = result["Body"]
        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=60",
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(filename, safe='')}"
        }
        if result.get("ContentLength") is not None:
            headers["Content-Length"] = str(result["ContentLength"])
        status_code = 200
        if result.get("ContentRange"):
            headers["Content-Range"] = result["ContentRange"]
            status_code = 206
        def original_chunks():
            try:
                yield from body.iter_chunks(chunk_size=1024 * 1024)
            finally:
                body.close()
        return StreamingResponse(original_chunks(), status_code=status_code, media_type=content_type, headers=headers)
    except Exception as exc:
        log.exception("Could not stream original object for %s", slug)
        raise HTTPException(status_code=502, detail="Unable to retrieve original video from storage") from exc


@app.get("/api/stream/{slug}/{object_path:path}")
def stream_hls_file(slug: str, object_path: str):
    _validate_slug(slug)
    object_path = object_path.lstrip("/")
    parts = object_path.split("/")
    if not object_path or "\\" in object_path or any(part in {"", ".", ".."} for part in parts) or Path(object_path).suffix.lower() not in {".m3u8", ".ts"}:
        raise HTTPException(status_code=400, detail="Invalid HLS path")
    video = repository.get_video(slug)
    if not video or not video.get("has_streams"):
        raise HTTPException(status_code=404, detail="HLS stream not found")

    quality_prefixes = video.get("quality_prefixes") or {}
    if object_path == "master.m3u8":
        storage_prefix = str(video.get("stream_prefix") or "").strip("/")
    else:
        quality = parts[0]
        if not QUALITY_RE.fullmatch(quality):
            raise HTTPException(status_code=404, detail="Unknown rendition")
        if quality not in (video.get("qualities") or []):
            raise HTTPException(status_code=404, detail="Rendition is not available")
        # Empty prefix is valid for imported legacy streams.
        storage_prefix = str(quality_prefixes.get(quality, video.get("stream_prefix") or "")).strip("/")
    object_key = f"{slug}/{storage_prefix + '/' if storage_prefix else ''}{object_path}"
    extension = Path(object_path).suffix.lower()
    try:
        result = s3_storage.get_object(s3_storage.streams_bucket(), object_key)
        body = result["Body"]
        if extension == ".m3u8":
            try:
                playlist = body.read().decode("utf-8")
            finally:
                body.close()
            playlist_dir = posixpath.dirname(object_path)
            rewritten: list[str] = []
            for line in playlist.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    rewritten.append(line)
                    continue
                if stripped.startswith("/") or "://" in stripped or "\\" in stripped:
                    raise HTTPException(status_code=400, detail="Unsupported HLS playlist reference")
                resolved = posixpath.normpath(posixpath.join(playlist_dir, stripped))
                if resolved == ".." or resolved.startswith("../") or resolved.startswith("/"):
                    raise HTTPException(status_code=400, detail="Invalid HLS playlist reference")
                rewritten.append(_stream_url(slug, resolved))
            return Response(content="\n".join(rewritten) + "\n", media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})
        headers = {"Cache-Control": "public, max-age=31536000, immutable"}
        if result.get("ContentLength") is not None:
            headers["Content-Length"] = str(result["ContentLength"])
        def segment_chunks():
            try:
                yield from body.iter_chunks(chunk_size=1024 * 1024)
            finally:
                body.close()
        return StreamingResponse(segment_chunks(), media_type="video/mp2t", headers=headers)
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Failed to proxy S3 object %s", object_key)
        raise HTTPException(status_code=502, detail="Unable to retrieve HLS object from storage") from exc


def _benchmark_authorized(request: Request) -> None:
    """Benchmarks consume CPU/S3 resources; never expose without explicit opt-in."""
    expected = os.getenv("BENCHMARK_ADMIN_TOKEN", "")
    if os.getenv("BENCHMARK_ENABLED", "false").lower() != "true" or not expected:
        raise HTTPException(status_code=503, detail="Benchmarks are disabled. Configure BENCHMARK_ENABLED=true and BENCHMARK_ADMIN_TOKEN.")
    supplied = request.headers.get("x-benchmark-token", "")
    if not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="Invalid benchmark access token")


@app.get("/api/benchmark/config")
def benchmark_config():
    return {"enabled": os.getenv("BENCHMARK_ENABLED", "false").lower() == "true" and bool(os.getenv("BENCHMARK_ADMIN_TOKEN")),
            "runtime": "API CPU and dedicated GPU worker", "ai_available": os.getenv("BENCHMARK_AI_ENABLED", "false").lower() == "true",
            "info": "CPU benchmarks run on the API; AI benchmarks require BENCHMARK_AI_ENABLED=true and a running CUDA worker."}


@app.post("/api/benchmark/start")
def benchmark_start(request: Request, payload: dict = Body(...)):
    _benchmark_authorized(request)
    slug = payload.get("slug", "")
    if not isinstance(slug, str):
        raise HTTPException(status_code=400, detail="Invalid video selection")
    _validate_slug(slug)
    video = repository.get_video(slug)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    try:
        if payload.get("mode") == "ai":
            if os.getenv("BENCHMARK_AI_ENABLED", "false").lower() != "true":
                raise ValueError("AI benchmarks are disabled: configure BENCHMARK_AI_ENABLED=true")
            return ai_benchmark_service.start(video, seconds=int(payload.get("seconds", 5)),
                scale=int(payload.get("scale", 2)), model=str(payload.get("model", "realesr-animevideov3")),
                tile=int(payload.get("tile", 384)), compare=payload.get("compare", False) is True,
                candidate_tile=int(payload.get("candidate_tile", 256)),
                comparison_rounds=int(payload.get("comparison_rounds", 1)),
                candidate_model=(str(payload["candidate_model"]) if payload.get("candidate_model") else None))
        seconds = int(payload.get("seconds", 5))
        quality = str(payload.get("quality", "480p"))
        presets = payload.get("presets", ["slow", "medium"])
        if not isinstance(presets, list) or not all(isinstance(p, str) for p in presets):
            raise ValueError("Invalid presets")
        return benchmark_service.start(video, seconds=seconds, quality=quality, presets=presets)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/benchmark/{job_id}")
def benchmark_status(job_id: str, request: Request):
    _benchmark_authorized(request)
    if not JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="Invalid benchmark ID")
    result = ai_benchmark_service.get(job_id) or benchmark_service.get(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Benchmark not found (reports are held on this API instance)")
    return result


@app.post("/api/worker/ai-benchmark/claim")
def ai_benchmark_claim(request: Request, payload: dict = Body(...)):
    _worker_authorized(request)
    if os.getenv("BENCHMARK_AI_ENABLED", "false").lower() != "true":
        return {"job": None}
    claim_id = str(payload.get("claim_id", ""))
    if not JOB_ID_RE.fullmatch(claim_id):
        raise HTTPException(status_code=422, detail="Invalid claim ID")
    return {"job": ai_benchmark_service.claim(claim_id)}


@app.post("/api/worker/ai-benchmark/{job_id}/complete")
def ai_benchmark_complete(job_id: str, request: Request, payload: dict = Body(...)):
    _worker_authorized(request)
    if not JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="Invalid benchmark ID")
    try:
        return ai_benchmark_service.finish(job_id, str(payload.get("claim_id", "")),
            results=payload.get("results"), error=payload.get("error"))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/upload")
def upload_video(file: UploadFile = File(...)):
    filename = _safe_filename(file.filename)
    from app.ffmpeg_service import VIDEO_EXTENSIONS, slugify
    extension = Path(filename).suffix.lower()
    if extension not in VIDEO_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{extension}'. Allowed: {', '.join(VIDEO_EXTENSIONS)}")
    slug = _validate_slug(slugify(filename))
    with _upload_lock:
        if slug in _upload_slugs:
            raise HTTPException(status_code=409, detail="This video is already being uploaded")
        _upload_slugs.add(slug)
    try:
        if repository.get_video(slug):
            raise HTTPException(status_code=409, detail=f"A video with slug '{slug}' already exists")
        max_bytes = int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024**3)))
        object_key = f"videos/{slug}/{filename}"
        try:
            file.file.seek(0, 2)
            size = int(file.file.tell())
            file.file.seek(0)
            if size <= 0:
                raise HTTPException(status_code=400, detail="Uploaded file is empty")
            if max_bytes > 0 and size > max_bytes:
                raise HTTPException(status_code=413, detail=f"Upload exceeds {max_bytes} bytes")
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            s3_storage.upload_fileobj(file.file, s3_storage.originals_bucket(), object_key, content_type=content_type)
            repository.upsert_video(slug, filename, object_key, size)
        except HTTPException:
            raise
        except Exception as exc:
            log.exception("Original upload or metadata save failed")
            raise HTTPException(status_code=502, detail="Could not store original video and metadata in S3") from exc
        finally:
            file.file.close()
        return JSONResponse({"slug": slug, "filename": filename, "status": "original_only", "original_url": f"/api/original/{slug}", "message": "Original uploaded and is playable. Request standard HLS processing when ready."}, status_code=201)

    finally:
        with _upload_lock:
            _upload_slugs.discard(slug)


@app.post("/api/convert/{slug}")
def request_conversion(slug: str, mode: str = "auto", job_type: str = "hls", scale: int = 2, tile: int | None = None):
    _validate_slug(slug)
    if mode not in ALLOWED_MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {sorted(ALLOWED_MODES)}")
    if job_type not in {"hls", "ai_upscale"}:
        raise HTTPException(status_code=400, detail="job_type must be 'hls' or 'ai_upscale'")
    if scale not in {2, 3, 4}:
        raise HTTPException(status_code=400, detail="AI scale must be 2, 3, or 4")
    if tile is not None and tile != 0 and not 32 <= tile <= 2048:
        raise HTTPException(status_code=400, detail="AI tile must be 0 (auto) or between 32 and 2048")
    video = repository.get_video(slug)
    if not video:
        raise HTTPException(status_code=404, detail="Original video not found")

    options: dict = {}
    if job_type == "hls":
        if video.get("has_streams") and video.get("qualities") and not (video.get("source_width") and video.get("source_height")):
            return JSONResponse({"slug": slug, "status": "ready", "qualities": video["qualities"], "already_ready": True, "message": "Existing HLS renditions are already registered; no duplicate job was created."}, status_code=200)
        if video.get("source_width") and video.get("source_height"):
            required = [item["name"] for item in build_quality_ladder(int(video["source_width"]), int(video["source_height"]))]
            if not required:
                raise HTTPException(status_code=422, detail="Source is below the lowest configured 240p HLS rendition. The original remains playable without upscaling.")
            missing = [quality for quality in required if quality not in (video.get("qualities") or [])]
            if not missing:
                return JSONResponse({"slug": slug, "status": "ready", "qualities": video["qualities"], "already_ready": True, "message": "All native-quality HLS renditions are already available; no duplicate job was created."}, status_code=200)
            options = {"target_qualities": missing}
        else:
            options = {}
    else:
        if not video.get("source_width") or not video.get("source_height"):
            raise HTTPException(status_code=409, detail="Run standard HLS processing once first. The worker will inspect the source resolution; AI options appear after that.")
        source_edge = min(int(video["source_width"]), int(video["source_height"]))
        if source_edge > 720:
            raise HTTPException(status_code=409, detail=f"AI upscaling is not offered for this source ({source_edge}p). Its native resolution is already above 720p.")
        targets = ai_upscale_targets(int(video["source_width"]), int(video["source_height"]), scale)
        missing_targets = [quality for quality in targets if quality not in (video.get("qualities") or [])]
        if not missing_targets:
            return JSONResponse({"slug": slug, "status": "ready", "qualities": video.get("qualities") or [], "already_ready": True, "message": "All configured AI-upscaled target qualities are already available."}, status_code=200)
        try:
            effective_tile = tile if tile is not None else int(os.getenv("AI_TILE_SIZE", "384"))
            ai_strength = float(os.getenv("AI_STRENGTH", "0.45"))
        except ValueError as exc:
            raise HTTPException(status_code=500, detail="AI_TILE_SIZE and AI_STRENGTH must be numeric") from exc
        if effective_tile != 0 and not 32 <= effective_tile <= 2048:
            raise HTTPException(status_code=500, detail="AI_TILE_SIZE must be 0 or between 32 and 2048")
        if not 0.0 <= ai_strength <= 1.0:
            raise HTTPException(status_code=500, detail="AI_STRENGTH must be between 0 and 1")
        model = os.getenv("AI_MODEL_NAME", "realesr-animevideov3")
        allowed_models = {"realesr-animevideov3", "realesrgan-x4plus", "realesrnet-x4plus", "realesrgan-x4plus-anime-6B"}
        if model not in allowed_models:
            raise HTTPException(status_code=500, detail=f"AI_MODEL_NAME must be one of {sorted(allowed_models)}")
        required_device = os.getenv("AI_REQUIRED_DEVICE", "auto").lower()
        if required_device not in {"auto", "cpu", "cuda"}:
            raise HTTPException(status_code=500, detail="AI_REQUIRED_DEVICE must be auto, cpu or cuda")
        profile = os.getenv("AI_PROFILE", "none")
        if profile not in {"none", "minimal", "soft_camera", "old_tv", "heavy_noise"}:
            raise HTTPException(status_code=500, detail="AI_PROFILE is not supported")
        options = {
            "scale": scale, "tile": effective_tile, "model": model,
            "device": required_device, "ai_strength": ai_strength,
            "profile": profile,
            "interpolate": os.getenv("AI_INTERPOLATE", "false").lower() in {"1", "true", "yes", "on"},
            "target_qualities": missing_targets,
        }

    job = repository.create_job(slug, mode=mode, job_type=job_type, options=options)
    if not job:
        active = repository.active_job_for_slug(slug)
        detail = f"A {active.get('job_type')} job is already {active.get('status')} for this video." if active else "A matching job could not be created."
        raise HTTPException(status_code=409, detail=detail)
    return JSONResponse({"slug": slug, "job_id": job["id"], "job_type": job["job_type"], "options": job.get("options", {}), "status": job["status"], "message": "Processing job queued. Available qualities will appear progressively."}, status_code=202)


@app.get("/api/convert/{slug}/status")
def conversion_status(slug: str):
    _validate_slug(slug)
    video = repository.get_video(slug)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return _video_payload(video)


@app.delete("/api/videos/{slug}")
def delete_video(slug: str, delete_source: bool = False):
    with repository.video_transaction():
        _validate_slug(slug)
        video = repository.get_video(slug)
        if not video:
            raise HTTPException(status_code=404, detail="Video not found")
        if repository.active_job_for_slug(slug):
            raise HTTPException(status_code=409, detail="Cannot delete a video while a worker is processing it")
        try:
            s3_storage.delete_prefix(s3_storage.streams_bucket(), f"{slug}/")
            if delete_source:
                s3_storage.delete_prefix(s3_storage.originals_bucket(), f"videos/{slug}/")
                repository.remove_video(slug)
            else:
                repository.clear_streams(slug)
        except Exception as exc:
            log.exception("Could not delete video %s", slug)
            raise HTTPException(status_code=502, detail="Could not delete video objects/metadata from S3") from exc
        return {"slug": slug, "deleted_streams": True, "source_deleted": delete_source}


# Authenticated worker API: S3 credentials are never exposed to the browser.
@app.post("/api/worker/jobs/claim")
def worker_claim(request: Request, payload: dict = Body(...)):
    _worker_authorized(request)
    capabilities = payload.get("capabilities", ["cpu"]) if isinstance(payload, dict) else ["cpu"]
    if not isinstance(capabilities, list):
        raise HTTPException(status_code=400, detail="capabilities must be an array")
    capabilities = [str(value).lower() for value in capabilities if str(value).lower() in {"cpu", "gpu"}]
    if not capabilities:
        capabilities = ["cpu"]
    stale_minutes = int(os.getenv("WORKER_JOB_STALE_MINUTES", "60"))
    claim_id = payload.get("claim_id", "")
    if not isinstance(claim_id, str) or not JOB_ID_RE.fullmatch(claim_id):
        raise HTTPException(status_code=400, detail="A UUID claim_id is required")
    job = repository.claim_next_job(stale_minutes=max(1, stale_minutes), capabilities=capabilities, claim_id=claim_id)
    return {"job": job}


@app.post("/api/worker/jobs/{job_id}/heartbeat")
def worker_heartbeat(job_id: str, request: Request):
    _worker_authorized(request)
    if not JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID")
    stale_minutes = int(os.getenv("WORKER_JOB_STALE_MINUTES", "60"))
    job = repository.heartbeat_job(job_id, stale_minutes, lease_token=_lease_token(request))
    if not job:
        raise HTTPException(status_code=409, detail="Job is no longer processing")
    return {"job_id": job_id, "status": "processing"}


@app.post("/api/worker/jobs/{job_id}/quality-ready")
def worker_quality_ready(job_id: str, request: Request, payload: dict = Body(...)):
    """Publish a finished rendition in the catalog without waiting for other rungs."""
    _worker_authorized(request)
    if not JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID")
    job = repository.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not repository.lease_matches(job, _lease_token(request), terminal=True):
        raise HTTPException(status_code=409, detail="Job lease is no longer owned by this worker")
    quality = payload.get("quality") if isinstance(payload, dict) else None
    qualities = payload.get("qualities", []) if isinstance(payload, dict) else []
    try:
        width, height = int(payload["source_width"]), int(payload["source_height"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="source_width and source_height are required integers")
    if width <= 0 or height <= 0:
        raise HTTPException(status_code=400, detail="Source dimensions must be positive")
    if not isinstance(quality, str) or not QUALITY_RE.fullmatch(quality):
        raise HTTPException(status_code=400, detail="Invalid quality label")
    if not isinstance(qualities, list) or not all(isinstance(value, str) and QUALITY_RE.fullmatch(value) for value in qualities):
        raise HTTPException(status_code=400, detail="qualities must be an array of valid quality labels")
    slug = str(job["slug"])
    job_prefix = f"{slug}/{job['output_prefix']}"
    bucket = s3_storage.streams_bucket()
    if not s3_storage.object_exists(bucket, f"{job_prefix}/{quality}/playlist.m3u8"):
        raise HTTPException(status_code=409, detail="Rendition playlist was not uploaded")
    if not s3_storage.object_exists(bucket, f"{job_prefix}/master.m3u8"):
        raise HTTPException(status_code=409, detail="Progressive master playlist was not uploaded")
    result = repository.publish_quality(job_id, quality, qualities, width, height, lease_token=_lease_token(request))
    if not result:
        raise HTTPException(status_code=409, detail="Job is no longer processing")
    return {"job_id": job_id, "status": "processing", "quality": quality, "qualities": result.get("qualities", [])}


@app.post("/api/worker/jobs/{job_id}/complete")
def worker_complete(job_id: str, request: Request, payload: dict = Body(...)):
    _worker_authorized(request)
    if not JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID")
    job = repository.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not repository.lease_matches(job, _lease_token(request), terminal=True):
        raise HTTPException(status_code=409, detail="Job lease is no longer owned by this worker")
    qualities = payload.get("qualities", []) if isinstance(payload, dict) else []
    if not isinstance(qualities, list) or not qualities or not all(isinstance(q, str) and QUALITY_RE.fullmatch(q) for q in qualities):
        raise HTTPException(status_code=400, detail="Worker must provide a non-empty qualities array")
    master_key = f"{job['slug']}/{job['output_prefix']}/master.m3u8"
    if not s3_storage.object_exists(s3_storage.streams_bucket(), master_key):
        raise HTTPException(status_code=409, detail="Uploaded HLS master playlist was not found in S3")
    result = repository.complete_job(job_id, qualities, lease_token=_lease_token(request))
    if not result:
        raise HTTPException(status_code=409, detail="Job could not be marked complete")
    log.info("Worker completed job %s; qualities now available: %s", job_id, qualities)
    return {"job_id": job_id, "status": "ready", "qualities": result.get("qualities", [])}


@app.post("/api/worker/jobs/{job_id}/fail")
def worker_fail(job_id: str, request: Request, payload: dict = Body(...)):
    _worker_authorized(request)
    if not JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID")
    error = str(payload.get("error", "Worker reported a failure")) if isinstance(payload, dict) else "Worker reported a failure"
    result = repository.fail_job(job_id, error, lease_token=_lease_token(request))
    if not result:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, "status": result["status"]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)


app.add_middleware(UploadLimitsMiddleware)
