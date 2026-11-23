"""Single API-instance, opt-in AI benchmark dispatch to CUDA workers.

Not a durable queue; use Redis/DB for multiple API replicas or production workloads.
"""
from __future__ import annotations
import copy
import os
import threading
import time
import uuid
from datetime import datetime, timezone

_LOCK = threading.RLock()
_JOBS: dict[str, dict] = {}
_MAX_SECONDS = 10
_MAX_RUN_SECONDS = max(1800, int(os.getenv("BENCHMARK_AI_TIMEOUT_SECONDS", "14400")))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _stale(job):
    # Bound abandoned jobs; an old worker callback is rejected after expiration.
    if job["status"] == "running" and time.time() - job.get("claimed_epoch", time.time()) > _MAX_RUN_SECONDS:
        job.update(status="failed", stage="Timed out", error=f"GPU worker did not complete within {_MAX_RUN_SECONDS // 60} minutes", completed_at=_now())


def start(video, *, seconds, scale, model, tile, compare=False, candidate_tile=256, comparison_rounds=1, candidate_model=None):
    from app.ai_upscaler import SUPPORTED_MODELS, SUPPORTED_SCALES
    if not 2 <= seconds <= _MAX_SECONDS:
        raise ValueError("AI benchmark sample duration must be 2–10 seconds")
    if scale not in SUPPORTED_SCALES or model not in SUPPORTED_MODELS:
        raise ValueError("Unsupported AI scale or model")
    if tile != 0 and not 32 <= tile <= 2048:
        raise ValueError("Tile must be 0 or between 32 and 2048")
    if compare and (candidate_tile not in {128, 192, 256, 320, 384, 448, 512}):
        raise ValueError("Comparison needs a different candidate tile from the supported safe choices")
    if candidate_model is not None and candidate_model not in SUPPORTED_MODELS:
        raise ValueError("Unsupported candidate AI model")
    if compare and candidate_model == model:
        raise ValueError("Choose a different model for AI model comparison")
    if compare and candidate_model is None and candidate_tile == tile:
        raise ValueError("Candidate tile must differ for tile comparison")
    if comparison_rounds not in {1, 2}:
        raise ValueError("Comparison rounds must be 1 or 2")
    if comparison_rounds != 1 and not compare:
        raise ValueError("Repeated comparison requires comparison mode")
    if int(video.get("size_bytes") or 0) > int(os.getenv("BENCHMARK_MAX_SOURCE_BYTES", str(2 * 1024**3))):
        raise ValueError("Source exceeds benchmark download limit")
    with _LOCK:
        for existing in _JOBS.values():
            _stale(existing)
        if any(job["status"] in {"queued", "running"} for job in _JOBS.values()):
            raise RuntimeError("Another AI benchmark is already running")
        job_id = str(uuid.uuid4())
        _JOBS[job_id] = dict(id=job_id, slug=video["slug"], original_key=video["original_key"],
                             status="queued", stage="Waiting for GPU worker", created_at=_now(),
                             seconds=seconds, scale=scale, model=model, tile=tile, compare=bool(compare), candidate_tile=candidate_tile if compare else None, candidate_model=candidate_model if compare else None, comparison_rounds=comparison_rounds, results=None, error=None, mode="ai")
        finished = sorted((j for j in _JOBS.values() if j["status"] in {"failed", "completed"}), key=lambda j:j["created_at"])
        for old in finished[:-20]:
            _JOBS.pop(old["id"], None)
        return public(_JOBS[job_id])


def public(job):
    return copy.deepcopy({k:v for k,v in job.items() if k not in {"original_key", "claimed_epoch", "claim_id"}})


def get(job_id):
    with _LOCK:
        job = _JOBS.get(job_id)
        if job:
            _stale(job)
        return public(job) if job else None


def claim(claim_id):
    with _LOCK:
        for job in _JOBS.values():
            _stale(job)
            if job["status"] == "running" and job.get("claim_id") == claim_id:
                return copy.deepcopy(job)
        for job in _JOBS.values():
            if job["status"] == "queued":
                job.update(status="running", stage="GPU worker processing", started_at=_now(),
                           claimed_epoch=time.time(), claim_id=claim_id)
                return copy.deepcopy(job)
    return None


def finish(job_id, claim_id, *, results=None, error=None):
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job or job["status"] != "running" or job.get("claim_id") != claim_id:
            raise ValueError("Benchmark claim no longer valid")
        if error:
            job.update(status="failed", stage="Failed", error=str(error)[:700])
        else:
            job.update(status="completed", stage="Finished", results=results)
        job["completed_at"] = _now()
        return public(job)
