#python-hls-streaming\app\repository.py
"""Single-API S3 metadata repository for videos and worker jobs.

Catalog/queue JSON objects use an in-process lock. Run exactly one API replica
and one Uvicorn process until this repository is moved to transactional storage.
"""
from __future__ import annotations
import copy
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from app import s3_storage

META_PREFIX = "_app"
CATALOG_KEY = f"{META_PREFIX}/catalog.json"
QUEUE_KEY = f"{META_PREFIX}/queue.json"
_LOCK = threading.RLock()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds")


def _bucket() -> str:
    return s3_storage.streams_bucket()


def _job_key(job_id: str) -> str:
    return f"{META_PREFIX}/jobs/{job_id}.json"


def _read_catalog() -> dict[str, Any]:
    value = s3_storage.get_json(_bucket(), CATALOG_KEY, default=None)
    if value is None:
        return {"version": 1, "videos": {}}
    if value.get("version") != 1 or not isinstance(value.get("videos"), dict):
        raise RuntimeError("S3 catalog metadata has an unsupported or invalid format")
    return value


def _write_catalog(catalog: dict[str, Any]) -> None:
    s3_storage.put_json(_bucket(), CATALOG_KEY, catalog)


def _read_queue() -> list[str]:
    value = s3_storage.get_json(_bucket(), QUEUE_KEY, default=None)
    if value is None:
        return []
    ids = value.get("job_ids", [])
    if not isinstance(ids, list):
        raise RuntimeError("S3 worker queue metadata is invalid")
    return [str(item) for item in ids if isinstance(item, str)]


def _write_queue(job_ids: list[str]) -> None:
    s3_storage.put_json(_bucket(), QUEUE_KEY, {"version": 1, "job_ids": list(dict.fromkeys(job_ids))})


def _set_video(video: dict[str, Any]) -> None:
    catalog = _read_catalog()
    catalog["videos"][video["slug"]] = video
    _write_catalog(catalog)


def get_video(slug: str) -> dict[str, Any] | None:
    video = _read_catalog()["videos"].get(slug)
    return copy.deepcopy(video) if video else None


def list_videos() -> list[dict[str, Any]]:
    videos = [copy.deepcopy(value) for value in _read_catalog()["videos"].values()]
    return sorted(videos, key=lambda item: str(item.get("filename", "")).casefold())


def upsert_video(slug: str, filename: str, original_key: str, size_bytes: int) -> dict[str, Any]:
    with _LOCK:
        catalog = _read_catalog()
        existing = catalog["videos"].get(slug)
        now = timestamp()
        if existing:
            existing.update(filename=filename, original_key=original_key, size_bytes=int(size_bytes), updated_at=now)
            video = existing
        else:
            video = {
                "slug": slug, "filename": filename, "original_key": original_key,
                "size_bytes": int(size_bytes), "has_streams": False, "qualities": [],
                "stream_prefix": "", "quality_prefixes": {}, "source_width": None,
                "source_height": None, "created_at": now, "updated_at": now,
                "latest_job": None,
            }
            catalog["videos"][slug] = video
        _write_catalog(catalog)
        return copy.deepcopy(video)


def set_legacy_streams(slug: str, qualities: list[str], stream_prefix: str = "") -> None:
    with _LOCK:
        catalog = _read_catalog()
        video = catalog["videos"].get(slug)
        if not video:
            return
        prefix = stream_prefix.strip("/")
        video["has_streams"] = bool(qualities)
        video["qualities"] = list(qualities)
        video["stream_prefix"] = prefix
        video["quality_prefixes"] = {quality: prefix for quality in qualities}
        video["updated_at"] = timestamp()
        _write_catalog(catalog)


def get_job(job_id: str) -> dict[str, Any] | None:
    return s3_storage.get_json(_bucket(), _job_key(job_id), default=None)


def _save_job(job: dict[str, Any]) -> None:
    s3_storage.put_json(_bucket(), _job_key(str(job["id"])), job)


def _update_latest_video_job(job: dict[str, Any]) -> None:
    catalog = _read_catalog()
    video = catalog["videos"].get(str(job["slug"]))
    if not video:
        raise RuntimeError(f"Video metadata disappeared for job {job['id']}")
    video["latest_job"] = copy.deepcopy(job)
    video["updated_at"] = timestamp()
    _write_catalog(catalog)


def create_job(slug: str, mode: str = "auto", job_type: str = "hls", options: dict[str, Any] | None = None) -> dict[str, Any] | None:
    with _LOCK:
        catalog = _read_catalog()
        video = catalog["videos"].get(slug)
        if not video:
            return None
        latest = video.get("latest_job") or {}
        if latest.get("status") in {"queued", "processing"}:
            # Do not silently return a different type of in-flight job.
            if latest.get("job_type") == job_type and latest.get("mode") == mode and (latest.get("options") or {}) == (options or {}):
                return copy.deepcopy(latest)
            return None
        now = timestamp()
        job = {
            "id": str(uuid.uuid4()), "slug": slug, "job_type": job_type, "mode": mode,
            "options": copy.deepcopy(options or {}),
            "required_capability": "gpu" if mode == "gpu" or (options or {}).get("device") == "cuda" else "cpu",
            "status": "queued", "attempts": 0, "error": None, "created_at": now,
            "updated_at": now, "started_at": None, "completed_at": None,
            "lease_expires_at": None, "qualities": [],
        }
        _save_job(job)
        _write_queue(_read_queue() + [job["id"]])
        video["latest_job"] = copy.deepcopy(job)
        video["updated_at"] = now
        _write_catalog(catalog)
        return copy.deepcopy(job)


def latest_job(slug: str) -> dict[str, Any] | None:
    video = get_video(slug)
    return copy.deepcopy(video.get("latest_job")) if video else None


def claim_next_job(stale_minutes: int = 1440, capabilities: list[str] | None = None) -> dict[str, Any] | None:
    """Claim a compatible queued job, or reclaim an expired worker lease."""
    worker_capabilities = set(capabilities or ["cpu"])
    if not worker_capabilities.intersection({"cpu", "gpu"}):
        worker_capabilities.add("cpu")
    with _LOCK:
        queue_ids = _read_queue()
        retained: list[str] = []
        now = utc_now()
        expiry_text = timestamp(now + timedelta(minutes=max(10, stale_minutes)))
        for position, job_id in enumerate(queue_ids):
            job = get_job(job_id)
            if not job or job.get("status") not in {"queued", "processing"}:
                continue
            options = job.get("options") or {}
            required = str(job.get("required_capability") or ("gpu" if job.get("mode") == "gpu" or options.get("device") == "cuda" else "cpu"))
            if required not in worker_capabilities:
                retained.append(job_id)
                continue
            if job.get("status") == "processing":
                try:
                    expired = not job.get("lease_expires_at") or datetime.fromisoformat(job["lease_expires_at"]) <= now
                except (TypeError, ValueError):
                    expired = True
                if not expired:
                    retained.append(job_id)
                    continue
            job.update(required_capability=required, status="processing", started_at=timestamp(now), updated_at=timestamp(now), lease_expires_at=expiry_text, attempts=int(job.get("attempts", 0)) + 1, error=None)
            _save_job(job)
            _update_latest_video_job(job)
            video = get_video(str(job["slug"]))
            if not video:
                raise RuntimeError(f"Original video metadata not found for job {job_id}")
            job["original_key"] = video["original_key"]
            job["filename"] = video["filename"]
            job["existing_qualities"] = list(video.get("qualities") or [])
            job["quality_prefixes"] = dict(video.get("quality_prefixes") or {})
            job["source_width"] = video.get("source_width")
            job["source_height"] = video.get("source_height")
            # Keep processing jobs in the queue document so another worker can
            # reclaim them once the lease expires after a worker/node failure.
            retained.append(job_id)
            _write_queue(retained + queue_ids[position + 1:])
            return job
        _write_queue(retained)
        return None


def heartbeat_job(job_id: str, lease_minutes: int = 1440) -> dict[str, Any] | None:
    with _LOCK:
        job = get_job(job_id)
        if not job or job.get("status") != "processing":
            return None
        now = utc_now()
        job["updated_at"] = timestamp(now)
        job["lease_expires_at"] = timestamp(now + timedelta(minutes=max(10, lease_minutes)))
        _save_job(job)
        _update_latest_video_job(job)
        return job


def publish_quality(job_id: str, quality: str, qualities: list[str], source_width: int, source_height: int) -> dict[str, Any] | None:
    """Commit one quality only after its playlist, segments and master exist in S3."""
    with _LOCK:
        job = get_job(job_id)
        if not job or job.get("status") != "processing":
            return None
        slug = str(job["slug"])
        catalog = _read_catalog()
        video = catalog["videos"].get(slug)
        if not video:
            raise RuntimeError(f"Video metadata disappeared for job {job_id}")
        current = set(video.get("qualities") or [])
        current.add(quality)
        # The worker sends the full manifest order; merge it with any other
        # qualities published by previous jobs to avoid accidentally dropping them.
        current.update(str(value) for value in qualities)
        ordered = _quality_sort(list(current))
        prefixes = dict(video.get("quality_prefixes") or {})
        prefixes[quality] = f"jobs/{job_id}"
        # Only the newly completed quality comes from this job. Other qualities
        # may live under an earlier HLS or AI job prefix and must keep that mapping.
        video.update(
            has_streams=True,
            qualities=ordered,
            stream_prefix=f"jobs/{job_id}",
            quality_prefixes=prefixes,
            source_width=int(source_width),
            source_height=int(source_height),
            updated_at=timestamp(),
        )
        job["qualities"] = ordered
        job["updated_at"] = timestamp()
        job["source_width"] = int(source_width)
        job["source_height"] = int(source_height)
        video["latest_job"] = copy.deepcopy(job)
        _write_catalog(catalog)
        _save_job(job)
        return copy.deepcopy(job)


def _quality_sort(names: list[str]) -> list[str]:
    order = {"240p": 0, "360p": 1, "480p": 2, "720p": 3, "1080p": 4, "1440p": 5, "2160p": 6}
    return sorted(set(names), key=lambda name: order.get(name, 999))


def complete_job(job_id: str, qualities: list[str]) -> dict[str, Any] | None:
    with _LOCK:
        job = get_job(job_id)
        if not job or job.get("status") != "processing":
            return None
        now_text = timestamp()
        job.update(status="ready", error=None, updated_at=now_text, completed_at=now_text, lease_expires_at=None)
        catalog = _read_catalog()
        video = catalog["videos"].get(str(job["slug"]))
        if not video:
            raise RuntimeError(f"Video metadata disappeared for completed job {job_id}")
        all_qualities = _quality_sort(list(set(video.get("qualities") or []) | set(qualities)))
        video["has_streams"] = bool(all_qualities)
        video["qualities"] = all_qualities
        video["stream_prefix"] = f"jobs/{job_id}"
        # Keep prior quality-prefix mappings; AI jobs add only new higher rungs.
        prefixes = dict(video.get("quality_prefixes") or {})
        for q in qualities:
            prefixes.setdefault(q, f"jobs/{job_id}")
        video["quality_prefixes"] = prefixes
        job["qualities"] = all_qualities
        video["latest_job"] = copy.deepcopy(job)
        video["updated_at"] = now_text
        _write_catalog(catalog)
        _save_job(job)
        _write_queue([item for item in _read_queue() if item != job_id])
        return copy.deepcopy(job)


def fail_job(job_id: str, error: str) -> dict[str, Any] | None:
    with _LOCK:
        job = get_job(job_id)
        if not job:
            return None
        catalog = _read_catalog()
        video = catalog["videos"].get(str(job["slug"]))
        latest = (video or {}).get("latest_job") or {}
        if latest.get("id") == job_id and latest.get("status") == "ready":
            recovered = copy.deepcopy(latest)
            _save_job(recovered)
            _write_queue([item for item in _read_queue() if item != job_id])
            return recovered
        if job.get("status") == "ready":
            _write_queue([item for item in _read_queue() if item != job_id])
            return job
        now_text = timestamp()
        job.update(status="failed", error=(error or "Unknown worker error")[:8000], updated_at=now_text, completed_at=now_text, lease_expires_at=None)
        _save_job(job)
        if video:
            video["latest_job"] = copy.deepcopy(job)
            video["updated_at"] = now_text
            _write_catalog(catalog)
        _write_queue([item for item in _read_queue() if item != job_id])
        return job


def active_job_for_slug(slug: str) -> dict[str, Any] | None:
    job = latest_job(slug)
    return job if job and job.get("status") in {"queued", "processing"} else None


def remove_video(slug: str) -> None:
    with _LOCK:
        catalog = _read_catalog()
        catalog["videos"].pop(slug, None)
        _write_catalog(catalog)
        queue = _read_queue()
        remove_ids: set[str] = set()
        for item in s3_storage.list_files(_bucket(), prefix=f"{META_PREFIX}/jobs/"):
            key = item.get("Key", "")
            job = s3_storage.get_json(_bucket(), key, default=None)
            if job and job.get("slug") == slug:
                remove_ids.add(str(job.get("id", "")))
                s3_storage.delete_object(_bucket(), key)
        _write_queue([job_id for job_id in queue if job_id not in remove_ids])


def clear_streams(slug: str) -> None:
    with _LOCK:
        catalog = _read_catalog()
        video = catalog["videos"].get(slug)
        if not video:
            return
        video.update(has_streams=False, qualities=[], stream_prefix="", quality_prefixes={}, latest_job=None, updated_at=timestamp())
        _write_catalog(catalog)
        queue = _read_queue()
        remove_ids: set[str] = set()
        for item in s3_storage.list_files(_bucket(), prefix=f"{META_PREFIX}/jobs/"):
            key = item.get("Key", "")
            job = s3_storage.get_json(_bucket(), key, default=None)
            if job and job.get("slug") == slug:
                remove_ids.add(str(job.get("id", "")))
                s3_storage.delete_object(_bucket(), key)
        _write_queue([job_id for job_id in queue if job_id not in remove_ids])


def import_legacy_video(slug: str, filename: str, original_key: str, size_bytes: int) -> bool:
    with _LOCK:
        catalog = _read_catalog()
        if slug in catalog["videos"]:
            return False
        now = timestamp()
        catalog["videos"][slug] = {
            "slug": slug, "filename": filename, "original_key": original_key,
            "size_bytes": int(size_bytes), "has_streams": False, "qualities": [],
            "stream_prefix": "", "quality_prefixes": {}, "source_width": None,
            "source_height": None, "created_at": now, "updated_at": now, "latest_job": None,
        }
        _write_catalog(catalog)
        return True
