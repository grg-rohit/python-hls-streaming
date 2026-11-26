#python-hls-streaming\worker.py
"""S3-backed processing worker with progressive HLS publication and safe SIGTERM."""
from __future__ import annotations
import logging
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import uuid
import random
from pathlib import Path
from urllib.parse import quote
import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT / "app" / ".env")
WORKER_DATA_DIR = Path(os.getenv("WORKER_DATA_DIR", "worker_data"))
if not WORKER_DATA_DIR.is_absolute():
    WORKER_DATA_DIR = PROJECT_ROOT / WORKER_DATA_DIR
WORKER_DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("FRAMELIFT_WEIGHTS", str(WORKER_DATA_DIR / "framelift_weights"))
os.environ["HLS_STREAMS_DIR"] = str(WORKER_DATA_DIR / "streams")

from app import s3_storage  # noqa: E402
from app.ai_upscaler import upscale_video  # noqa: E402
from app.ffmpeg_service import (  # noqa: E402
    build_quality_ladder, convert, detect_gpu_encoder, probe_source,
    quality_specs_for_source, sort_qualities, stream_dir, write_master_playlist,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
log = logging.getLogger("hls.worker")
API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
WORKER_TOKEN = os.getenv("WORKER_SHARED_TOKEN", "")
POLL_SECONDS = max(1, int(os.getenv("WORKER_POLL_SECONDS", "5")))
HEARTBEAT_SECONDS = max(15, int(os.getenv("WORKER_HEARTBEAT_SECONDS", "30")))
DEFAULT_MODE = os.getenv("WORKER_DEFAULT_MODE", "auto")
WORKER_DEVICE = os.getenv("WORKER_DEVICE", "auto").lower()
STOP_REQUESTED = threading.Event()
ACTIVE_JOB = threading.Event()


def _handle_stop(signum, _frame):
    log.info("Received signal %s; will stop claiming jobs after the active job finishes", signum)
    STOP_REQUESTED.set()


def resolve_worker_device() -> tuple[str, list[str], str | None]:
    if WORKER_DEVICE not in {"auto", "cpu", "cuda"}:
        raise RuntimeError("WORKER_DEVICE must be auto, cpu or cuda")
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("PyTorch is missing. Install requirements-worker.txt and a CPU/CUDA PyTorch wheel") from exc
    cuda_available = bool(torch.cuda.is_available())
    if WORKER_DEVICE == "cuda" and not cuda_available:
        raise RuntimeError("WORKER_DEVICE=cuda but PyTorch cannot see an NVIDIA GPU")
    actual = "cuda" if cuda_available and WORKER_DEVICE != "cpu" else "cpu"
    capabilities = ["cpu"]
    gpu_name = None
    if actual == "cuda":
        capabilities.append("gpu")
        gpu_name = torch.cuda.get_device_name(0)
    configured = os.getenv("WORKER_CAPABILITIES", "").strip()
    if configured:
        requested = list(dict.fromkeys(part.strip().lower() for part in configured.split(",")))
        if not requested or any(part not in capabilities for part in requested):
            raise RuntimeError("WORKER_CAPABILITIES must be a subset of available capabilities: " + ",".join(capabilities))
        capabilities = requested
    return actual, capabilities, gpu_name


class CallbackDeliveryError(RuntimeError):
    """Transient callback delivery exhausted: leave job recoverable via lease expiry."""


class LeaseLostError(RuntimeError):
    """A newer attempt owns the job; this worker must stop publishing."""


class WorkerClient:
    def __init__(self, capabilities: list[str]) -> None:
        if not WORKER_TOKEN:
            raise RuntimeError("WORKER_SHARED_TOKEN is missing from worker environment")
        self.capabilities = capabilities
        self.session = requests.Session()
        self.session.headers.update({"X-Worker-Token": WORKER_TOKEN})
        self.lease_token: str | None = None
        self._pending_claim_id: str | None = None

    def _post(self, url: str, *, payload: dict | None = None, lease=False, attempts=5) -> dict:
        headers = {"X-Job-Lease": self.lease_token} if lease and self.lease_token else {}
        for attempt in range(attempts):
            try:
                response = self.session.post(url, json=payload, headers=headers, timeout=(10, 60), allow_redirects=False)
                if 300 <= response.status_code < 400:
                    raise RuntimeError("Worker API redirects are not allowed; configure the final API URL")
                if lease and response.status_code in {401, 403, 404, 409}:
                    raise LeaseLostError(f"Lease callback rejected ({response.status_code})")
                # Retry only temporary failures; never retry invalid payloads or permissions.
                if response.status_code not in {408, 425, 429, 500, 502, 503, 504}:
                    response.raise_for_status()
                    return response.json()
                response.raise_for_status()
            except LeaseLostError:
                raise
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
                if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code not in {408, 425, 429, 500, 502, 503, 504}:
                    raise
                if attempt == attempts - 1:
                    if lease:
                        raise CallbackDeliveryError("Callback delivery uncertain; job will be retried after lease expiry") from exc
                    raise
                time.sleep(min(12, 0.6 * (2 ** attempt)) + random.uniform(0, 0.25))
        raise RuntimeError("Request retries exhausted")

    def claim(self) -> dict | None:
        # Retain the claim_id across network errors so a lost claim response
        # cannot cause us to claim a second job while the first is processing.
        if self._pending_claim_id is None:
            self._pending_claim_id = str(uuid.uuid4())
        response = self._post(f"{API_BASE_URL}/api/worker/jobs/claim",
                              payload={"capabilities": self.capabilities, "claim_id": self._pending_claim_id})
        self._pending_claim_id = None
        job = response.get("job")
        self.lease_token = str(job["lease_token"]) if job else None
        return job

    def quality_ready(self, job_id: str, quality: str, qualities: list[str], source_width: int, source_height: int) -> None:
        self._post(f"{API_BASE_URL}/api/worker/jobs/{quote(job_id, safe='')}/quality-ready",
                   payload={"quality": quality, "qualities": qualities, "source_width": source_width, "source_height": source_height}, lease=True)

    def complete(self, job_id: str, qualities: list[str]) -> None:
        self._post(f"{API_BASE_URL}/api/worker/jobs/{quote(job_id, safe='')}/complete",
                   payload={"qualities": qualities}, lease=True)

    def heartbeat(self, job_id: str) -> None:
        self._post(f"{API_BASE_URL}/api/worker/jobs/{quote(job_id, safe='')}/heartbeat", lease=True, attempts=3)

    def fail(self, job_id: str, error: str) -> None:
        self._post(f"{API_BASE_URL}/api/worker/jobs/{quote(job_id, safe='')}/fail",
                   payload={"error": error[:8000]}, lease=True, attempts=3)

    def close(self) -> None:
        self.session.close()


def _upload_file(local_file: Path, object_key: str) -> None:
    suffix = local_file.suffix.lower()
    content_type = "application/vnd.apple.mpegurl" if suffix == ".m3u8" else ("video/mp2t" if suffix == ".ts" else None)
    s3_storage.upload_file(local_file, s3_storage.streams_bucket(), object_key, content_type)


def _upload_rendition(local_slug: str, object_prefix: str, quality: str) -> None:
    """Upload transport-stream segments first and the VOD playlist last."""
    directory = stream_dir(local_slug) / quality
    playlist = directory / "playlist.m3u8"
    segments = sorted(directory.glob("segment_*.ts"))
    if not playlist.is_file() or not segments:
        raise RuntimeError(f"Rendition {quality} is incomplete; playlist/segments missing")
    for segment in segments:
        relative = segment.relative_to(stream_dir(local_slug)).as_posix()
        _upload_file(segment, f"{object_prefix}/{relative}")
    _upload_file(playlist, f"{object_prefix}/{quality}/playlist.m3u8")


def _publish_master(local_slug: str, object_prefix: str, names: list[str], source_width: int, source_height: int) -> None:
    ordered = sort_qualities(names)
    specs = quality_specs_for_source(source_width, source_height, ordered)
    master_path = stream_dir(local_slug) / "master.m3u8"
    write_master_playlist(master_path, specs)
    _upload_file(master_path, f"{object_prefix}/master.m3u8")


def process_job(job: dict, client: WorkerClient, worker_device: str) -> None:
    job_id = str(job["id"])
    slug = str(job["slug"])
    job_type = str(job.get("job_type") or "hls")
    options = job.get("options") or {}
    if job_type not in {"hls", "ai_upscale"}:
        raise RuntimeError(f"Unsupported job type: {job_type}")
    local_slug = f"{slug}-{job_id[:8]}-{str(job['lease_token'])[:8]}"
    object_prefix = f"{slug}/{job['output_prefix']}"
    local_output = stream_dir(local_slug)
    if local_output.exists():
        shutil.rmtree(local_output)
    heartbeat_stop = threading.Event()
    lease_lost = threading.Event()

    def heartbeat_loop() -> None:
        while not heartbeat_stop.wait(HEARTBEAT_SECONDS):
            try:
                client.heartbeat(job_id)
            except LeaseLostError:
                lease_lost.set()
                log.error("Lease revoked for job %s; worker will stop before publication", job_id)
                return
            except Exception:
                log.warning("Could not renew lease for job %s", job_id, exc_info=True)

    thread = threading.Thread(target=heartbeat_loop, name=f"job-heartbeat-{job_id[:8]}", daemon=True)
    thread.start()
    ACTIVE_JOB.set()
    try:
        with tempfile.TemporaryDirectory(prefix=f"hls-job-{job_id[:8]}-", dir=WORKER_DATA_DIR) as temp_dir:
            source_path = Path(temp_dir) / Path(str(job.get("filename") or "source.mp4")).name
            log.info("Downloading original object %s", job["original_key"])
            s3_storage.download_file(s3_storage.originals_bucket(), str(job["original_key"]), source_path)
            source_width, source_height = probe_source(source_path)
            processing_source = source_path
            target_qualities = None
            if job_type == "ai_upscale":
                allowed_edge = min(source_width, source_height)
                if allowed_edge > 720:
                    raise RuntimeError(f"AI upscale refused: source is {allowed_edge}p; this feature is limited to sources at or below 720p")
                target_qualities = [str(value) for value in options.get("target_qualities", [])]
                target_qualities = [q for q in target_qualities if q not in (job.get("existing_qualities") or [])]
                if target_qualities:
                    enhanced_path = Path(temp_dir) / f"{source_path.stem}_framelift.mp4"
                    requested_device = str(options.get("device") or "auto").lower()
                    ai_device = worker_device if requested_device == "auto" else requested_device
                    ai_encoder = str(options.get("encoder") or os.getenv("AI_ENCODER") or ("nvenc" if ai_device == "cuda" else "cpu")).lower()
                    log.info("Starting optional FrameLift enhancement: source=%sp scale=%sx targets=%s device=%s", allowed_edge, options.get("scale", 2), ",".join(target_qualities), ai_device)
                    processing_source = upscale_video(
                        source_path, enhanced_path, scale=int(options.get("scale", 2)),
                        tile=int(options.get("tile", os.getenv("AI_TILE_SIZE", "384"))),
                        model=str(options.get("model") or os.getenv("AI_MODEL_NAME", "realesr-animevideov3")),
                        device=ai_device, half=options.get("half"), encoder=ai_encoder,
                        ai_strength=float(options.get("ai_strength", os.getenv("AI_STRENGTH", "0.45"))),
                        profile=str(options.get("profile") or os.getenv("AI_PROFILE", "none")),
                        interpolate=options.get("interpolate"),
                    )
                else:
                    log.info("All requested AI target qualities were already published; skipping FrameLift")

            mode = str(job.get("mode") or DEFAULT_MODE)
            if mode == "auto" and worker_device == "cpu":
                mode = "cpu"
            existing_names = list(job.get("existing_qualities") or [])
            if job_type == "hls":
                requested = options.get("target_qualities")
                if isinstance(requested, list):
                    target_qualities = [str(value) for value in requested if str(value) not in existing_names]
                elif job.get("source_width") and job.get("source_height"):
                    required = [item["name"] for item in build_quality_ladder(int(job["source_width"]), int(job["source_height"]))]
                    target_qualities = [value for value in required if value not in existing_names]
                else:
                    target_qualities = None
            published_names: list[str] = list(existing_names)
            # For HLS jobs, metadata is written as the very first variant becomes
            # available. For AI jobs, existing low renditions remain in place;
            # this job publishes only newly generated higher renditions.
            def on_quality_ready(quality: dict, completed_local: list[str]) -> None:
                name = str(quality["name"])
                if lease_lost.is_set():
                    raise LeaseLostError("Lease revoked during encoding")
                _upload_rendition(local_slug, object_prefix, name)
                combined = sort_qualities(published_names + completed_local)
                _publish_master(local_slug, object_prefix, combined, source_width, source_height)
                if lease_lost.is_set():
                    raise LeaseLostError("Lease revoked before quality publication")
                client.quality_ready(job_id, name, combined, source_width, source_height)
                if name not in published_names:
                    published_names.append(name)
                log.info("Progressively published %s for %s; now playable: %s", name, slug, ", ".join(combined))

            if target_qualities == []:
                # Lease recovery after all quality-ready callbacks but before the
                # final completion request: finish metadata without re-encoding.
                final_names = sort_qualities(published_names)
                if not final_names:
                    raise RuntimeError("Reclaimed job has no remaining or previously published renditions")
                _publish_master(local_slug, object_prefix, final_names, source_width, source_height)
                client.complete(job_id, final_names)
                log.info("Recovered completed outputs for job %s without re-encoding", job_id)
                return

            log.info("Encoding HLS renditions from %s (%s)", processing_source.name, mode)
            generated = convert(
                source=processing_source, slug=local_slug, clean=True, resume=False, mode=mode,
                quality_names=target_qualities, on_quality_ready=on_quality_ready,
            )
            all_qualities = sort_qualities(published_names + generated)
            # Reassert a final combined master. It points to each rendition by its
            # quality name; the API maps each quality to its own immutable S3 job prefix.
            _publish_master(local_slug, object_prefix, all_qualities, source_width, source_height)
            if lease_lost.is_set():
                raise LeaseLostError("Lease revoked before completion")
            client.complete(job_id, all_qualities)
            log.info("Job %s completed; all available qualities: %s", job_id, ", ".join(all_qualities))
    except Exception as exc:
        log.exception("Job %s failed", job_id)
        try:
            if not isinstance(exc, (LeaseLostError, CallbackDeliveryError)):
                client.fail(job_id, f"{type(exc).__name__}: {exc}")
        except Exception:
            log.exception("Could not report failure for job %s", job_id)
        raise
    finally:
        heartbeat_stop.set()
        thread.join(timeout=5)
        ACTIVE_JOB.clear()
        client.lease_token = None
        if local_output.exists():
            shutil.rmtree(local_output, ignore_errors=True)


def check_ai_benchmark(client: WorkerClient, worker_device: str) -> bool:
    if worker_device != "cuda" or os.getenv("BENCHMARK_AI_ENABLED", "false").lower() != "true":
        return False
    claim_id = str(uuid.uuid4())
    response = client._post(f"{API_BASE_URL}/api/worker/ai-benchmark/claim", payload={"claim_id": claim_id})
    job = response.get("job")
    if not job:
        return False
    try:
        from app.ai_benchmark_worker import execute
        report = execute(job)
        payload = {"claim_id": claim_id, "results": report}
    except Exception as exc:
        log.exception("AI benchmark failed")
        payload = {"claim_id": claim_id, "error": str(exc)[:700]}
    client._post(f"{API_BASE_URL}/api/worker/ai-benchmark/{job['id']}/complete", payload=payload)
    return True


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    try:
        worker_device, capabilities, gpu_name = resolve_worker_device()
        client = WorkerClient(capabilities)
    except Exception as exc:
        log.error("Worker configuration error: %s", exc)
        return 2
    log.info("Worker ready: API=%s poll=%ss device=%s capabilities=%s", API_BASE_URL, POLL_SECONDS, worker_device, ",".join(capabilities))
    if gpu_name:
        log.info("NVIDIA GPU detected: %s", gpu_name)
    try:
        encoder = detect_gpu_encoder()
    except Exception:
        encoder = None
    log.info("FFmpeg hardware encoder: %s", encoder or "none; CPU encoding")
    log.info("Scratch: %s; FrameLift cache: %s", WORKER_DATA_DIR, os.getenv("FRAMELIFT_WEIGHTS"))
    idle_seconds = POLL_SECONDS
    try:
        while not STOP_REQUESTED.is_set():
            try:
                if check_ai_benchmark(client, worker_device):
                    idle_seconds = POLL_SECONDS
                    continue
                job = client.claim()
                if job is None:
                    STOP_REQUESTED.wait(idle_seconds)
                    idle_seconds = min(60, max(POLL_SECONDS, idle_seconds * 2))
                    continue
                idle_seconds = POLL_SECONDS
                try:
                    process_job(job, client, worker_device)
                except Exception:
                    # The API has already received the failure where possible.
                    pass
            except requests.RequestException:
                log.exception("Could not communicate with API; retrying")
                STOP_REQUESTED.wait(max(POLL_SECONDS, 10))
            except Exception:
                log.exception("Unexpected worker error; retrying")
                STOP_REQUESTED.wait(max(POLL_SECONDS, 10))
    finally:
        client.close()
    log.info("Worker stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
