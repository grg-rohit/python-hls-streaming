"""Single-process S3 repository: a mutation commits one authoritative catalog.

Run one API process per metadata bucket. V1 queue/job records are imported on
access; the next mutation writes v2. Stop old API/workers before upgrading.
"""
from __future__ import annotations
import copy
import hmac
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from app import s3_storage

META_PREFIX = '_app'
CATALOG_KEY = f'{META_PREFIX}/catalog.json'
QUEUE_KEY = f'{META_PREFIX}/queue.json'
_LOCK = threading.RLock()


def utc_now():
    return datetime.now(timezone.utc)


def timestamp(value=None):
    return (value or utc_now()).isoformat(timespec='seconds')


def _bucket():
    return s3_storage.streams_bucket()


def _job_key(job_id):
    return f'{META_PREFIX}/jobs/{job_id}.json'


def _read_catalog():
    catalog = s3_storage.get_json(_bucket(), CATALOG_KEY, default=None)
    if catalog is None:
        return {'version': 2, 'videos': {}, 'jobs': {}}
    if catalog.get('version') not in {1, 2} or not isinstance(catalog.get('videos'), dict):
        raise RuntimeError('Unsupported or invalid S3 catalog')
    if catalog['version'] == 2:
        if not isinstance(catalog.get('jobs'), dict):
            raise RuntimeError('Invalid catalog jobs')
        return catalog
    # Recover jobs written to the old queue before a failed catalog update.
    queue = s3_storage.get_json(_bucket(), QUEUE_KEY, default={})
    ids = set(queue.get('job_ids', []))
    embedded = {}
    for video in catalog['videos'].values():
        job = video.get('latest_job')
        if job and job.get('id'):
            embedded[job['id']] = job
            ids.add(job['id'])
    jobs = {}
    for job_id in ids:
        saved = s3_storage.get_json(_bucket(), _job_key(job_id), default=None)
        latest = embedded.get(job_id)
        # Completion in v1 could commit in the catalog before the job file.
        job = latest if latest and (latest.get('status') == 'ready' or not saved or
                                   latest.get('updated_at', '') > saved.get('updated_at', '')) else saved
        if not job or job.get('slug') not in catalog['videos']:
            continue
        job = copy.deepcopy(job)
        if job.get('status') == 'processing':
            job.update(status='queued', lease_expires_at=None)
        job.pop('lease_token', None)
        jobs[job_id] = job
    for slug, video in catalog['videos'].items():
        active = [j for j in jobs.values() if j['slug'] == slug and j['status'] == 'queued']
        preferred = (video.get('latest_job') or {}).get('id')
        active.sort(key=lambda j: (j['id'] != preferred, j.get('created_at', ''), j['id']))
        if active:
            video['latest_job'] = copy.deepcopy(active[0])
            for duplicate in active[1:]:
                duplicate.update(status='failed', error='Duplicate retired during migration', lease_expires_at=None)
        elif preferred in jobs:
            video['latest_job'] = copy.deepcopy(jobs[preferred])
    catalog.update(version=2, jobs=jobs)
    return catalog


def _write_catalog(catalog):
    s3_storage.put_json(_bucket(), CATALOG_KEY, catalog)


def _commit_job(catalog, job):
    catalog['jobs'][job['id']] = job
    video = catalog['videos'][job['slug']]
    video['latest_job'] = copy.deepcopy(job)
    video['updated_at'] = job['updated_at']
    _write_catalog(catalog)


@contextmanager
def video_transaction():
    """Serialize check/upload/register and check/delete against job creation."""
    with _LOCK:
        yield


def get_video(slug):
    with _LOCK:
        return copy.deepcopy(_read_catalog()['videos'].get(slug))


def list_videos():
    with _LOCK:
        return sorted(copy.deepcopy(list(_read_catalog()['videos'].values())), key=lambda v: v['filename'].casefold())


def _new_video(slug, filename, original_key, size_bytes):
    return dict(slug=slug, filename=filename, original_key=original_key, size_bytes=int(size_bytes),
                has_streams=False, qualities=[], stream_prefix='', quality_prefixes={}, source_width=None,
                source_height=None, created_at=timestamp(), updated_at=timestamp(), latest_job=None)


def upsert_video(slug, filename, original_key, size_bytes):
    with _LOCK:
        catalog = _read_catalog()
        video = catalog['videos'].setdefault(slug, _new_video(slug, filename, original_key, size_bytes))
        video.update(filename=filename, original_key=original_key, size_bytes=int(size_bytes), updated_at=timestamp())
        _write_catalog(catalog)
        return copy.deepcopy(video)


def import_legacy_video(slug, filename, original_key, size_bytes):
    with _LOCK:
        catalog = _read_catalog()
        if slug in catalog['videos']:
            return False
        catalog['videos'][slug] = _new_video(slug, filename, original_key, size_bytes)
        _write_catalog(catalog)
        return True


def set_legacy_streams(slug, qualities, stream_prefix=''):
    with _LOCK:
        catalog = _read_catalog()
        video = catalog['videos'].get(slug)
        if not video:
            return
        prefix = stream_prefix.strip('/')
        video.update(has_streams=bool(qualities), qualities=list(qualities), stream_prefix=prefix,
                     quality_prefixes={q: prefix for q in qualities}, updated_at=timestamp())
        _write_catalog(catalog)


def get_job(job_id):
    with _LOCK:
        return copy.deepcopy(_read_catalog()['jobs'].get(job_id))


def job_routing(mode, job_type, options=None):
    """Production hybrid policy is opt-in; preserve standalone defaults."""
    options = copy.deepcopy(options or {})
    if os.getenv('HYBRID_DEPLOYMENT', 'false').lower() == 'true':
        if job_type == 'ai_upscale':
            options['device'] = 'cuda'
            return 'auto', options, 'gpu'
        return 'cpu', options, 'cpu'
    capability = 'gpu' if mode == 'gpu' or options.get('device') == 'cuda' else 'cpu'
    return mode, options, capability


def create_job(slug, mode='auto', job_type='hls', options=None):
    mode, options, capability = job_routing(mode, job_type, options)
    with _LOCK:
        catalog = _read_catalog()
        video = catalog['videos'].get(slug)
        if not video:
            return None
        latest = video.get('latest_job') or {}
        if latest.get('status') in {'queued', 'processing'}:
            latest_mode, latest_options, _ = job_routing(latest.get('mode', 'auto'), latest.get('job_type', 'hls'), latest.get('options'))
            matches = latest.get('job_type') == job_type and latest_mode == mode and latest_options == options
            return copy.deepcopy(latest) if matches else None
        now = timestamp()
        job = dict(id=str(uuid.uuid4()), slug=slug, job_type=job_type, mode=mode, options=copy.deepcopy(options or {}),
                   required_capability=capability,
                   status='queued', attempts=0, error=None, created_at=now, updated_at=now, started_at=None,
                   completed_at=None, lease_expires_at=None, qualities=[])
        _commit_job(catalog, job)
        return copy.deepcopy(job)


def latest_job(slug):
    video = get_video(slug)
    return video.get('latest_job') if video else None


def active_job_for_slug(slug):
    job = latest_job(slug)
    return job if job and job.get('status') in {'queued', 'processing'} else None


def _expired(job):
    try:
        return datetime.fromisoformat(job['lease_expires_at']) <= utc_now()
    except (KeyError, TypeError, ValueError):
        return True


def lease_matches(job, token, *, terminal=False):
    if not job or not token or not hmac.compare_digest(str(job.get('lease_token', '')), token):
        return False
    return (terminal and job['status'] in {'ready', 'failed'}) or (job['status'] == 'processing' and not _expired(job))


def _claim_payload(catalog, job):
    result = copy.deepcopy(job)
    video = catalog['videos'][job['slug']]
    for key in ('original_key', 'filename', 'quality_prefixes', 'source_width', 'source_height'):
        result[key] = copy.deepcopy(video.get(key))
    result['existing_qualities'] = list(video.get('qualities', []))
    return result


def claim_next_job(stale_minutes=60, capabilities=None, *, claim_id):
    with _LOCK:
        catalog = _read_catalog()
        # A lost response returns the original lease, never claims another job.
        for job in catalog['jobs'].values():
            if job.get('claim_id') == claim_id:
                return _claim_payload(catalog, job) if job['status'] == 'processing' and not _expired(job) else None
        supported = set(capabilities or ['cpu'])
        for job in sorted(catalog['jobs'].values(), key=lambda j: (j['created_at'], j['id'])):
            if job['status'] not in {'queued', 'processing'}:
                continue
            if job['status'] == 'processing' and not _expired(job):
                continue
            # Normalize pending legacy jobs only when hybrid routing is enabled.
            if os.getenv('HYBRID_DEPLOYMENT', 'false').lower() == 'true':
                mode, options, capability = job_routing(job.get('mode', 'auto'), job.get('job_type', 'hls'), job.get('options'))
            else:
                mode, options, capability = job.get('mode', 'auto'), job.get('options', {}), job.get('required_capability', 'cpu')
            if capability not in supported:
                continue
            job.update(mode=mode, options=options, required_capability=capability)
            token = str(uuid.uuid4())
            now = utc_now()
            job.update(status='processing', started_at=timestamp(now), updated_at=timestamp(now), error=None,
                       lease_expires_at=timestamp(now + timedelta(minutes=max(1, stale_minutes))),
                       attempts=int(job.get('attempts', 0)) + 1, lease_token=token, claim_id=claim_id,
                       output_prefix=f"jobs/{job['id']}/attempts/{token}")
            _commit_job(catalog, job)
            return _claim_payload(catalog, job)
        return None  # Idle polling is read-only.


def heartbeat_job(job_id, lease_minutes=60, *, lease_token):
    with _LOCK:
        catalog = _read_catalog()
        job = catalog['jobs'].get(job_id)
        if not lease_matches(job, lease_token):
            return None
        now = utc_now()
        job.update(updated_at=timestamp(now), lease_expires_at=timestamp(now + timedelta(minutes=max(1, lease_minutes))))
        _commit_job(catalog, job)
        return copy.deepcopy(job)


def _quality_sort(names):
    return sorted(set(names), key=lambda q: int(q.rstrip('p')))


def publish_quality(job_id, quality, qualities, source_width, source_height, *, lease_token):
    with _LOCK:
        catalog = _read_catalog()
        job = catalog['jobs'].get(job_id)
        if not lease_matches(job, lease_token):
            return None
        video = catalog['videos'][job['slug']]
        # Add only the rendition verified by the API, never unverified labels.
        ordered = _quality_sort(list(video.get('qualities', [])) + [quality])
        prefixes = dict(video.get('quality_prefixes', {}))
        prefixes[quality] = job['output_prefix']
        video.update(has_streams=True, qualities=ordered, quality_prefixes=prefixes,
                     stream_prefix=job['output_prefix'], source_width=source_width, source_height=source_height)
        job.update(qualities=ordered, source_width=source_width, source_height=source_height, updated_at=timestamp())
        _commit_job(catalog, job)
        return copy.deepcopy(job)


def complete_job(job_id, qualities, *, lease_token):
    with _LOCK:
        catalog = _read_catalog()
        job = catalog['jobs'].get(job_id)
        if not lease_matches(job, lease_token, terminal=True) or job['status'] == 'failed':
            return None
        if job['status'] == 'ready':
            return copy.deepcopy(job) if set(qualities) == set(job['qualities']) else None
        video = catalog['videos'][job['slug']]
        if set(qualities) != set(video.get('qualities', [])) or not qualities:
            return None
        video['stream_prefix'] = job['output_prefix']
        job.update(status='ready', qualities=_quality_sort(qualities), error=None, updated_at=timestamp(),
                   completed_at=timestamp(), lease_expires_at=None)
        _commit_job(catalog, job)
        return copy.deepcopy(job)


def fail_job(job_id, error, *, lease_token):
    with _LOCK:
        catalog = _read_catalog()
        job = catalog['jobs'].get(job_id)
        if not lease_matches(job, lease_token, terminal=True):
            return None
        if job['status'] in {'ready', 'failed'}:
            return copy.deepcopy(job)
        job.update(status='failed', error=(error or 'Unknown worker error')[:8000], updated_at=timestamp(),
                   completed_at=timestamp(), lease_expires_at=None)
        _commit_job(catalog, job)
        return copy.deepcopy(job)


def _remove_jobs(catalog, slug):
    catalog['jobs'] = {key: job for key, job in catalog['jobs'].items() if job['slug'] != slug}


def remove_video(slug):
    with _LOCK:
        catalog = _read_catalog()
        catalog['videos'].pop(slug, None)
        _remove_jobs(catalog, slug)
        _write_catalog(catalog)


def clear_streams(slug):
    with _LOCK:
        catalog = _read_catalog()
        video = catalog['videos'].get(slug)
        if not video:
            return
        video.update(has_streams=False, qualities=[], stream_prefix='', quality_prefixes={}, latest_job=None, updated_at=timestamp())
        _remove_jobs(catalog, slug)
        _write_catalog(catalog)
