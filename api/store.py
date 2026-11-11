"""Simple local persistence for processing records.

Records are stored as one JSON file per resume under ``results/`` and
the uploaded originals under ``uploads/``. This is a deliberately
simple local store (no database) compatible with the existing project;
it is the documented persistence limitation of this GUI layer.

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
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)


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
    ensure_dirs()
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
    ensure_dirs()
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
