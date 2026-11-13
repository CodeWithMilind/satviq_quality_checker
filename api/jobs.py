"""In-memory batch-processing jobs for the Process Resume page.

Why this exists: ``POST /api/resumes/process`` only returns when
*every* file in the batch has finished, so the UI could show upload
progress (bytes sent) but had no way to know how many files the
server had actually processed while it waited — the bar sat at
100% on "processing on the server…" with no count.

A job is created by ``POST /api/resumes/process-async`` once the
uploads have been saved to disk. A background thread then processes
the files one by one (the exact same ``process_resume_file`` call
the synchronous endpoint makes) and updates the job after each
file. The frontend polls ``GET /api/resumes/jobs/{job_id}`` and
renders real counts: processed, remaining, percent, the file
currently being processed, and a per-file status list.

Jobs are deliberately in-memory and single-process, like the
existing ``ai_generation_in_progress`` guard: they describe work
happening in *this* server process right now. A finished job is
kept for a while so the final poll can still read its results;
old jobs are pruned on creation. Nothing here is simulated — every
count changes only when a file really starts / finishes.
"""

from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}

# Keep finished jobs readable for a while (the UI's final poll, a
# slow tab, a user looking back), then prune so a long-running
# server does not accumulate snapshots forever.
_KEEP_FINISHED = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_job(filenames: list[str]) -> dict[str, Any]:
    """Create a queued job for ``filenames`` (in upload order)."""
    job_id = str(uuid.uuid4())
    job: dict[str, Any] = {
        "job_id": job_id,
        "status": "queued",  # queued -> running -> done
        "total": len(filenames),
        "completed": 0,
        "succeeded": 0,
        "failed": 0,
        "current_index": None,  # 1-based position of the active file
        "current_filename": None,
        "files": [
            {"filename": name, "status": "pending", "resume_id": None,
             "success": None}
            for name in filenames
        ],
        "results": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    with _LOCK:
        _JOBS[job_id] = job
        _prune_locked()
    return snapshot(job_id)  # type: ignore[return-value]


def _prune_locked() -> None:
    finished = [j for j in _JOBS.values() if j["status"] == "done"]
    if len(finished) <= _KEEP_FINISHED:
        return
    finished.sort(key=lambda j: j["updated_at"])
    for job in finished[: len(finished) - _KEEP_FINISHED]:
        _JOBS.pop(job["job_id"], None)


def _mutate(job_id: str, fn) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        fn(job)
        job["updated_at"] = _now()


def start_file(job_id: str, index: int) -> None:
    """Mark file ``index`` (0-based) as the one now processing."""
    def _apply(job: dict[str, Any]) -> None:
        job["status"] = "running"
        job["current_index"] = index + 1
        entry = job["files"][index]
        entry["status"] = "processing"
        job["current_filename"] = entry["filename"]
    _mutate(job_id, _apply)


def finish_file(job_id: str, index: int, public_record: dict[str, Any]) -> None:
    """Record one finished file: bump the real processed count and
    append its public record to the job's results, in upload order
    (files are processed sequentially, so append order is upload
    order)."""
    def _apply(job: dict[str, Any]) -> None:
        entry = job["files"][index]
        entry["status"] = "done" if public_record.get("success") else "failed"
        entry["resume_id"] = public_record.get("resume_id")
        entry["success"] = bool(public_record.get("success"))
        job["results"].append(public_record)
        job["completed"] += 1
        if public_record.get("success"):
            job["succeeded"] += 1
        else:
            job["failed"] += 1
        if job["completed"] >= job["total"]:
            job["status"] = "done"
            job["current_index"] = None
            job["current_filename"] = None
    _mutate(job_id, _apply)


def finish_job(job_id: str) -> None:
    """Mark the job done even if the loop ended without a record
    for some file (defensive; the normal path finishes via the
    last ``finish_file``)."""
    def _apply(job: dict[str, Any]) -> None:
        job["status"] = "done"
        job["current_index"] = None
        job["current_filename"] = None
    _mutate(job_id, _apply)


def snapshot(job_id: str) -> dict[str, Any] | None:
    """A JSON-ready progress snapshot, or None for an unknown job.

    Counts the UI renders, all derived from the real counters:
    ``processed`` = files finished, ``remaining`` = files not yet
    finished (including the one currently processing),
    ``percent`` = processed / total as a whole number.
    """
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        snap = copy.deepcopy(job)
    total = snap["total"]
    completed = snap["completed"]
    snap["processed"] = completed
    snap["remaining"] = total - completed
    snap["percent"] = int(completed * 100 / total) if total else 100
    snap["is_done"] = snap["status"] == "done"
    return snap
