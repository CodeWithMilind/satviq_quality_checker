"""Ground-truth dataset import helpers (spec Part C).

Parses human annotation files (JSON or CSV) into validated
annotation dicts keyed by resume filename. Row-level errors are
collected and returned — a bad row never silently passes and never
kills the valid rows. Nothing here touches the database or the
filesystem; the API layer (a later phase) wires parsing to storage.

CSV format (one header row):
    filename, career_stage, professional_domain, present_sections,
    missing_sections, completeness_score, quality_score,
    suitable_roles, notes
Section/role lists are semicolon-separated. An empty suitable_roles
cell means "not labelled"; the literal NONE means "labelled, no role
is suitable".

JSON format: a list of objects (or {"annotations": [...]}) with the
same fields, lists as real arrays, scores as numbers or null, and
expected_suitable_roles null when unlabelled.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from typing import Any

from .schemas import validate_annotation


def content_hash(data: bytes) -> str:
    """SHA-256 of a resume file — dataset dedupe and split leakage
    prevention both key on this."""
    return hashlib.sha256(data).hexdigest()


def _split_cell(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).split(";")
            if part.strip()]


def _finish(rows: list[tuple[str, dict[str, Any]]]
            ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    annotations: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    for filename, raw in rows:
        if not filename:
            errors.append({"row": filename, "error": "missing filename"})
            continue
        if filename in annotations:
            errors.append({"row": filename,
                           "error": "duplicate filename in import file"})
            continue
        clean, error = validate_annotation(raw)
        if error is not None:
            errors.append({"row": filename, "error": error})
        else:
            annotations[filename] = clean  # type: ignore[assignment]
    return annotations, errors


def parse_ground_truth_json(
    text: str,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, [{"row": None, "error": f"invalid JSON: {exc}"}]
    if isinstance(payload, dict):
        payload = payload.get("annotations")
    if not isinstance(payload, list):
        return {}, [{"row": None,
                     "error": "expected a list of annotation objects"}]
    rows: list[tuple[str, dict[str, Any]]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            rows.append(("", {}))
            continue
        entry = dict(entry)
        filename = str(entry.pop("filename", "") or "").strip()
        rows.append((filename, entry))
    return _finish(rows)


def parse_ground_truth_csv(
    text: str,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return {}, [{"row": None, "error": "CSV has no header row"}]
    rows: list[tuple[str, dict[str, Any]]] = []
    for record in reader:
        filename = str(record.get("filename") or "").strip()
        roles_cell = (record.get("suitable_roles") or "").strip()
        if roles_cell == "":
            roles: list[str] | None = None
        elif roles_cell.upper() == "NONE":
            roles = []
        else:
            roles = _split_cell(roles_cell)
        rows.append((filename, {
            "expected_present_sections":
                _split_cell(record.get("present_sections")),
            "expected_missing_sections":
                _split_cell(record.get("missing_sections")),
            "expected_completeness_score":
                (record.get("completeness_score") or "").strip() or None,
            "expected_quality_score":
                (record.get("quality_score") or "").strip() or None,
            "expected_suitable_roles": roles,
            "career_stage": record.get("career_stage") or "",
            "professional_domain": record.get("professional_domain") or "",
            "notes": record.get("notes") or "",
        }))
    return _finish(rows)
