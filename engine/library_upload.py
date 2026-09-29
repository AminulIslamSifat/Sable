"""Upload job state for the study library.

An *upload job* tracks the ingest half of a document upload:

    staged -> converting -> extracting -> persisting -> indexing -> done

Only the job state and the cooperative cancel flag live here. The pipeline
coroutine that actually drives those phases lives in
``server/api/routes/study.py``, because it needs that module's library writers,
topic store and catalog — importing them back from here would be circular.
Keeping the state separate is what lets the status endpoint read a job without
touching the route module.

Job files live in ``library/upload_jobs/<upload-id>.json`` and staged bytes in
``library/staging/<upload-id><ext>``. Both are transient: a job that reaches a
terminal state has its staged bytes removed, and a sweep on job creation
reclaims anything an earlier crash left behind.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from engine.config import LIBRARY_STAGING_DIR, LIBRARY_UPLOAD_JOBS_DIR

logger = logging.getLogger("sable.study.upload")

_JOB_VERSION = 1

# Staged bytes older than this are assumed abandoned (the server died
# mid-upload) and are reclaimed on the next job creation. Deliberately
# generous — a slow link on a 100 MB file is not a crash.
_STAGING_TTL = 6 * 3600

# Terminal jobs stay visible this long so the sidebar can show a final "done"
# or "cancelled" row before the entry disappears.
_TERMINAL_TTL = 120

# Percent the *ingest* half of the bar has reached when each phase begins.
# Indexing owns 60..100: the status endpoint folds the index job's own percent
# into that tail so a row shows one number for the whole upload instead of
# resetting when ingest hands off.
PHASE_START = {
    "converting": 5,
    "extracting": 30,
    "persisting": 50,
    "indexing": 60,
}
INGEST_CEILING = 60

_TERMINAL = ("done", "error", "cancelled")

# Cooperative cancellation. A background job has no request to cancel, so
# stopping one means setting a flag the pipeline polls between phases and
# hands to the index job as its own should_stop.
_cancel_requests: set[str] = set()

# Uploads with a pipeline actually executing in THIS process. A job file that
# says "running" with no entry here belongs to a process that died.
_active: set[str] = set()


def _valid_id(upload_id: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{32}", upload_id or ""))


# ── Liveness + cancellation ──────────────────────────────────────────────────


def is_live(upload_id: str) -> bool:
    return upload_id in _active


def mark_active(upload_id: str) -> None:
    """Register a job as executing. Called BEFORE the task is spawned, so a
    status poll landing in between never downgrades a fresh job to errored."""
    _active.add(upload_id)


def mark_inactive(upload_id: str) -> None:
    _active.discard(upload_id)


def is_cancelled(upload_id: str) -> bool:
    return upload_id in _cancel_requests


def request_cancel(upload_id: str) -> bool:
    """Flag a job for cancellation. Returns whether it was actually running."""
    if not upload_id:
        return False
    _cancel_requests.add(upload_id)
    return upload_id in _active


def clear_cancel(upload_id: str) -> None:
    _cancel_requests.discard(upload_id)


# ── Paths ────────────────────────────────────────────────────────────────────


def _jobs_dir() -> Path:
    LIBRARY_UPLOAD_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    return LIBRARY_UPLOAD_JOBS_DIR


def _job_path(upload_id: str) -> Path:
    return _jobs_dir() / f"{upload_id}.json"


def staging_path(upload_id: str, ext: str) -> Path:
    LIBRARY_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    return LIBRARY_STAGING_DIR / f"{upload_id}{ext}"


# ── Job state ────────────────────────────────────────────────────────────────


def _empty_job(upload_id: str) -> dict[str, Any]:
    return {
        "version": _JOB_VERSION,
        "upload_id": upload_id,
        "doc_id": "",
        "filename": "",
        "ext": "",
        "size": 0,
        "title": "",
        "pages": 0,
        "group": {},
        "index_mode": "",
        "ocr": "",
        "status": "staged",   # staged | running | done | error | cancelled
        "phase": "",          # converting | extracting | persisting | indexing
        "percent": 0,
        "error": "",
        "created": time.time(),
        "updated": time.time(),
    }


def load_job(upload_id: str) -> dict[str, Any] | None:
    if not _valid_id(upload_id):
        return None
    path = _job_path(upload_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    base = _empty_job(upload_id)
    base.update(data)
    base["upload_id"] = upload_id
    return base


def save_job(job: dict[str, Any]) -> None:
    upload_id = str(job.get("upload_id") or "")
    if not _valid_id(upload_id):
        return
    job["updated"] = time.time()
    target = _job_path(upload_id)
    tmp = target.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
    except Exception:
        logger.warning("could not persist upload job %s", upload_id, exc_info=True)


def create_job(
    upload_id: str,
    *,
    filename: str,
    ext: str,
    size: int,
    group: dict[str, Any],
    index_mode: str,
    ocr: str,
) -> dict[str, Any]:
    _sweep_stale_staging()
    job = _empty_job(upload_id)
    job.update({
        "filename": filename,
        "ext": ext,
        "size": size,
        "group": group,
        "index_mode": index_mode,
        "ocr": ocr,
    })
    save_job(job)
    return job


def set_phase(upload_id: str, phase: str) -> None:
    """Move the job into a new ingest phase, snapping the bar to its start."""
    job = load_job(upload_id)
    if not job:
        return
    job["status"] = "running"
    job["phase"] = phase
    job["percent"] = PHASE_START.get(phase, job.get("percent", 0))
    save_job(job)


def set_doc_id(upload_id: str, doc_id: str, title: str, pages: int) -> None:
    job = load_job(upload_id)
    if not job:
        return
    job["doc_id"] = doc_id
    job["title"] = title
    job["pages"] = pages
    save_job(job)


def finish(upload_id: str, status: str, *, error: str = "") -> None:
    job = load_job(upload_id)
    if not job:
        return
    job["status"] = status
    job["phase"] = ""
    job["error"] = error
    if status == "done":
        job["percent"] = 100
    save_job(job)


def discard_staging(upload_id: str) -> None:
    """Remove any staged bytes for this upload, whatever the extension."""
    try:
        if not LIBRARY_STAGING_DIR.exists():
            return
        for p in LIBRARY_STAGING_DIR.glob(f"{upload_id}.*"):
            p.unlink(missing_ok=True)
    except Exception:
        logger.warning("staging cleanup failed for %s", upload_id, exc_info=True)


def delete_job(upload_id: str) -> None:
    try:
        _job_path(upload_id).unlink(missing_ok=True)
    except Exception:
        pass


def _sweep_stale_staging() -> None:
    try:
        if not LIBRARY_STAGING_DIR.exists():
            return
        cutoff = time.time() - _STAGING_TTL
        for p in LIBRARY_STAGING_DIR.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
            except Exception:
                continue
    except Exception:
        pass


# ── Queries ──────────────────────────────────────────────────────────────────


def list_jobs() -> list[dict[str, Any]]:
    """Jobs worth showing: everything, minus terminal ones past their TTL.

    Expired entries are deleted rather than merely skipped, so the job
    directory cannot grow without bound.
    """
    out: list[dict[str, Any]] = []
    cutoff = time.time() - _TERMINAL_TTL
    try:
        paths = sorted(_jobs_dir().glob("*.json"))
    except Exception:
        return out
    for path in paths:
        job = load_job(path.stem)
        if not job:
            continue
        if str(job.get("status") or "") in _TERMINAL:
            if float(job.get("updated") or 0) < cutoff:
                delete_job(path.stem)
                continue
        out.append(job)
    out.sort(key=lambda j: float(j.get("created") or 0), reverse=True)
    return out


def status_for(upload_id: str, *, index_percent: int | None = None) -> dict[str, Any]:
    """Compact status for the sidebar row.

    ``index_percent`` (0-100) folds the indexing pass into the tail of the bar
    so the row shows one number for the whole upload instead of resetting to
    zero when ingest hands off to indexing.
    """
    job = load_job(upload_id)
    if not job:
        job = _empty_job(upload_id)

    status = str(job.get("status") or "staged")
    phase = str(job.get("phase") or "")

    percent = int(job.get("percent") or 0)
    if status == "running" and phase == "indexing" and index_percent is not None:
        span = 100 - INGEST_CEILING
        percent = INGEST_CEILING + int(max(0, min(100, index_percent)) * span / 100)
    if status == "done":
        percent = 100

    # A job file can claim "running" while nothing drives it — the server was
    # restarted mid-upload. Reporting that as running would show a bar that
    # never moves. There is nothing to resume from either (the staged bytes may
    # be gone), so surface it as an error instead of a permanent spinner.
    if status == "running" and not is_live(upload_id):
        status = "error"

    return {
        "upload_id": job.get("upload_id"),
        "doc_id": job.get("doc_id") or "",
        "filename": job.get("filename") or "",
        "title": job.get("title") or job.get("filename") or "",
        "size": job.get("size") or 0,
        "status": status,
        "phase": phase if status == "running" else "",
        "percent": max(0, min(100, percent)),
        "error": job.get("error") or "",
        "cancel_requested": is_cancelled(upload_id),
        "live": is_live(upload_id),
        "updated": job.get("updated") or 0,
    }
