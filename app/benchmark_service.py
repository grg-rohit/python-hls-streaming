"""Explicit opt-in, bounded, isolated CPU benchmark launcher.

This executes the project's existing benchmark script against an S3 original.
Jobs are in-memory and single-instance by design; use a durable queue before
running multiple API replicas. Does not alter the production HLS ladder.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from app import s3_storage

ROOT = Path(__file__).resolve().parents[1]
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="benchmark")
_LOCK = threading.RLock()
_JOBS: dict[str, dict] = {}
_ACTIVE: str | None = None
_ALLOWED_PRESETS = {"slow", "medium", "fast"}
_ALLOWED_QUALITIES = {"240p", "360p", "480p", "720p", "1080p", "1440p", "2160p"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def start(video: dict, *, seconds: int, quality: str, presets: list[str]) -> dict:
    global _ACTIVE
    if not 2 <= seconds <= 15:
        raise ValueError("Benchmark duration must be between 2 and 15 seconds")
    if quality not in _ALLOWED_QUALITIES:
        raise ValueError("Unsupported target quality")
    if not 1 <= len(presets) <= 3 or len(set(presets)) != len(presets) or any(p not in _ALLOWED_PRESETS for p in presets):
        raise ValueError("Choose one to three unique presets: slow, medium, fast")
    if int(video.get("size_bytes") or 0) > int(os.getenv("BENCHMARK_MAX_SOURCE_BYTES", str(2 * 1024**3))):
        raise ValueError("Video exceeds benchmark source download limit")
    with _LOCK:
        if _ACTIVE and _JOBS.get(_ACTIVE, {}).get("status") in ("queued", "running"):
            raise RuntimeError("Another benchmark is currently running")
        job_id = str(uuid.uuid4())
        _JOBS[job_id] = {"id": job_id, "slug": video["slug"], "status": "queued", "created_at": _now(),
                         "seconds": seconds, "quality": quality, "presets": presets, "results": None, "error": None}
        _ACTIVE = job_id
        _EXECUTOR.submit(_execute, job_id, video["original_key"])
        # Keep only a limited number of finished reports in memory.
        finished = [j for j in _JOBS.values() if j["status"] in ("completed", "failed")]
        for old in sorted(finished, key=lambda j: j["created_at"])[:-20]:
            _JOBS.pop(old["id"], None)
        return dict(_JOBS[job_id])


def get(job_id: str) -> dict | None:
    with _LOCK:
        result = _JOBS.get(job_id)
        return json.loads(json.dumps(result)) if result else None


def _execute(job_id: str, original_key: str) -> None:
    global _ACTIVE
    try:
        with _LOCK:
            job = _JOBS[job_id]
            job.update(status="running", started_at=_now(), stage="Downloading source from S3")
            params = {k: job[k] for k in ("seconds", "quality", "presets")}
        with tempfile.TemporaryDirectory(prefix="hls-ui-benchmark-") as folder:
            source = Path(folder) / "source.video"
            report = Path(folder) / "result.json"
            started = time.perf_counter()
            s3_storage.download_file(s3_storage.originals_bucket(), original_key, source)
            download_seconds = round(time.perf_counter() - started, 3)
            with _LOCK:
                job.update(stage="Preparing and encoding benchmark clips", download_seconds=download_seconds)
            cmd = [sys.executable, str(ROOT / "tools" / "benchmark_pipeline.py"), str(source),
                   "--seconds", str(params["seconds"]), "--quality", params["quality"],
                   "--presets", *params["presets"], "--report", str(report)]
            started = time.perf_counter()
            proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=900, check=False)
            processing_seconds = round(time.perf_counter() - started, 3)
            if report.exists():
                output = json.loads(report.read_text(encoding="utf-8"))
            else:
                raise RuntimeError(f"Benchmark could not generate a report: {proc.stderr[-700:]}")
            with _LOCK:
                job.update(status="completed" if proc.returncode == 0 and not output.get("errors") else "failed",
                           stage="Finished", completed_at=_now(), processing_seconds=processing_seconds,
                           results=output, error=("One or more test cases failed" if output.get("errors") else None))
    except Exception as exc:
        with _LOCK:
            _JOBS[job_id].update(status="failed", stage="Failed", completed_at=_now(), error=str(exc)[:700])
    finally:
        with _LOCK:
            if _ACTIVE == job_id:
                _ACTIVE = None
