"""Persistence for processing records — PostgreSQL primary store.

Records live in PostgreSQL (schema V2, see ``api/db.py``): the full
record is stored verbatim in ``candidates.pipeline_record`` and
projected into the relational tables for querying. No JSON record
files are created or read anymore (the ``results/`` folder is legacy;
pre-migration records are imported once with
``scripts/import_to_postgres.py --import``). The uploaded original
files still live under ``uploads/`` — the database row references the
stored name, it does not hold the binary.

If PostgreSQL is not configured or not reachable, saves and reads
raise a clear error: silently falling back to JSON files would split
the data across two stores. The one exception is the test suite,
which runs disk-backed when ``SATVIQ_TEST_DISK_STORE=1``.

Keys starting with ``_`` are internal (e.g. the stored upload name) and
are stripped before any record is sent to the frontend, so internal
server paths are never exposed.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .pipeline import ensure_pipeline_trace

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UPLOADS_DIR = PROJECT_ROOT / "uploads"
RESULTS_DIR = PROJECT_ROOT / "results"

_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def ensure_dirs() -> None:
    # Uploads remain files on disk; record JSON files are no longer
    # written (PostgreSQL is the store) so results/ is not created.
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def _db_primary() -> bool:
    """True when record storage must go to PostgreSQL.

    SATVIQ_TEST_DISK_STORE=1 keeps the old file behavior for the
    automated test suite only — production always uses PostgreSQL.
    """
    import os
    if os.environ.get("SATVIQ_TEST_DISK_STORE") == "1":
        return False
    return True


_DB_CHECKED = False


def _require_db():
    """Import api.db and verify the connection once per process.

    Raises RuntimeError with a plain-language explanation when the
    database is not configured or not reachable — in DB-primary mode
    a down database must fail loudly, never silently split data.
    """
    global _DB_CHECKED
    from . import db
    if not _DB_CHECKED:
        status = db.check_connection()
        if not status["ok"]:
            raise RuntimeError(
                "PostgreSQL (SATVIQ primary store) is unavailable: "
                + status["detail"])
        logger.info("SATVIQ store: %s", status["detail"])
        _DB_CHECKED = True
    return db


def valid_resume_id(resume_id: str) -> bool:
    return bool(_ID_RE.match(resume_id or ""))


def _result_path(resume_id: str) -> Path:
    return RESULTS_DIR / f"{resume_id}.json"


def public(record: dict[str, Any]) -> dict[str, Any]:
    """Strip internal keys before a record leaves the server."""
    item = {k: v for k, v in record.items() if not k.startswith("_")}
    stored_name = record.get("_stored_name")
    item["has_original_file"] = bool(
        stored_name and (UPLOADS_DIR / stored_name).is_file())
    # YAML Editor AI was removed from SATVIQ: records stored while
    # it existed may still carry its per-resume block — never serve
    # it (the stored file itself is left as-is).
    item.pop("yaml_editor", None)
    # Phone display consistency: the structured phone field is
    # served in the canonical +91 XXXXXXXXXX format wherever the
    # record is displayed. New records are already normalized at
    # validation time (muse_ai.schema); this covers records stored
    # before that. Read-time only — the stored file is never
    # rewritten, and invalid/ambiguous values pass through unchanged.
    try:
        from .phone_numbers import normalize_phone_field

        for key in ("resume_json", "resume_data"):
            data = item.get(key)
            if isinstance(data, dict) and data.get("phone"):
                data = dict(data)
                data["phone"] = normalize_phone_field(data["phone"])
                item[key] = data
        llm_block = item.get("llm")
        if isinstance(llm_block, dict) and isinstance(
                llm_block.get("resume_json"), dict) \
                and llm_block["resume_json"].get("phone"):
            llm_block = dict(llm_block)
            inner = dict(llm_block["resume_json"])
            inner["phone"] = normalize_phone_field(inner["phone"])
            llm_block["resume_json"] = inner
            item["llm"] = llm_block
        recovery = item.get("contact_recovery")
        if isinstance(recovery, dict) and isinstance(
                recovery.get("recovered"), dict) \
                and isinstance(recovery["recovered"].get("phone"), dict):
            recovery = dict(recovery)
            recovered = dict(recovery["recovered"])
            phone_entry = dict(recovered["phone"])
            if phone_entry.get("value"):
                phone_entry["value"] = normalize_phone_field(
                    phone_entry["value"])
                recovered["phone"] = phone_entry
                recovery["recovered"] = recovered
                item["contact_recovery"] = recovery
    except Exception:
        pass
    return item


def summary(record: dict[str, Any]) -> dict[str, Any]:
    """List-view record: everything except the full extracted text."""
    item = public(record)
    text = item.pop("extracted_text", "") or ""
    item["extracted_text_chars"] = len(text)
    item["attempt_count"] = len(item.get("attempts") or [])
    # Evaluation history can be large; list views carry only the
    # latest successful analysis plus the attempt count. The full
    # history stays on the record (detail view / analysis endpoints).
    analyses = item.pop("analyses", None)
    item["analysis_count"] = len(analyses) if isinstance(analyses, list) else 0
    return item


def save_record(record: dict[str, Any]) -> None:
    if _db_primary():
        db = _require_db()
        db.sync_record(record)
        return
    ensure_dirs()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _result_path(record["resume_id"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_raw_record(resume_id: str) -> dict[str, Any] | None:
    """The stored record exactly as it is on disk — no derived
    backfills. For updates that will be saved back (e.g. manual
    extractor runs), so read-time derivations are never persisted."""
    if not valid_resume_id(resume_id):
        return None
    if _db_primary():
        db = _require_db()
        return db.fetch_record(resume_id)
    path = _result_path(resume_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.exception("Could not read stored record %s", resume_id)
        return None


def get_record(resume_id: str) -> dict[str, Any] | None:
    record = load_raw_record(resume_id)
    if record is None:
        return None
    # Records stored before pipeline traces existed get theirs derived
    # from their stored attempt history on read (never persisted back,
    # so stored files are never silently rewritten).
    return ensure_pipeline_trace(record)


def list_records() -> list[dict[str, Any]]:
    if _db_primary():
        db = _require_db()
        records = [ensure_pipeline_trace(r)
                   for r in db.fetch_all_records()]
        records.sort(key=lambda r: r.get("uploaded_at", ""), reverse=True)
        return records
    ensure_dirs()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for path in RESULTS_DIR.glob("*.json"):
        try:
            records.append(ensure_pipeline_trace(
                json.loads(path.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, OSError):
            logger.warning("Skipping unreadable record file: %s", path.name)
    records.sort(key=lambda r: r.get("uploaded_at", ""), reverse=True)
    return records


def delete_record(resume_id: str) -> bool:
    record = get_record(resume_id)
    if record is None:
        return False
    if _db_primary():
        db = _require_db()
        db.delete_candidate_by_resume(resume_id)
    else:
        _result_path(resume_id).unlink(missing_ok=True)
    stored_name = record.get("_stored_name")
    if stored_name:
        # stored_name is generated by the server (uuid + sanitised
        # name); still resolve-and-check to be safe against traversal.
        candidate = (UPLOADS_DIR / stored_name).resolve()
        if candidate.parent == UPLOADS_DIR.resolve() and candidate.exists():
            candidate.unlink()
    return True


def clear_records() -> int:
    count = 0
    for record in list_records():
        if delete_record(record["resume_id"]):
            count += 1
    return count


def compute_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(records)
    successful = sum(1 for r in records if r.get("success"))
    failed = total - successful
    passed = sum(1 for r in records if r.get("quality_status") == "PASS")
    warnings = sum(1 for r in records if r.get("quality_status") == "WARNING")
    quality_failed = sum(1 for r in records if r.get("quality_status") == "FAIL")
    pass_rate = round((passed / total * 100) if total else 0.0, 1)
    return {
        "total_processed": total,
        "successful_extractions": successful,
        "failed_extractions": failed,
        "quality_pass": passed,
        "quality_warning": warnings,
        "quality_fail": quality_failed,
        "quality_pass_rate_percent": pass_rate,
        "recent_activity": [summary(r) for r in records[:5]],
    }
