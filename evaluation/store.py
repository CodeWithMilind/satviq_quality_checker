"""Persistence for resume EVALUATION analyses.

SATVIQ has no database: records live as one JSON file per resume
under ``results/`` via ``api.store`` (see EVALUATION_NOTES.md on the
spec's PostgreSQL assumption). Analyses follow the same pattern —
they are stored ON the resume's record:

- ``record["analyses"]`` — the full append-only history of
  evaluation attempts (successes AND failures, in run order);
- ``record["analysis"]`` — the latest SUCCESSFUL analysis entry,
  or absent/None when no evaluation has ever succeeded.

A failed attempt therefore never overwrites the latest successful
analysis. Writes go through ``api.store.save_record``, exactly like
every other record mutation in this project.
"""

from __future__ import annotations

from typing import Any

from api import store as record_store

from . import config as eval_config


def yaml_version() -> str:
    """The configuration version string stamped on every analysis."""
    return eval_config.yaml_version()


def append_analysis(record: dict[str, Any],
                    entry: dict[str, Any]) -> dict[str, Any]:
    """Append ``entry`` to the record's analysis history, promote it
    to ``record["analysis"]`` when it succeeded, and save the record
    through the project's record store. Returns the entry."""
    history = record.get("analyses")
    if not isinstance(history, list):
        history = []
    history = [*history, entry]
    record["analyses"] = history
    if entry.get("status") == "success":
        record["analysis"] = entry
    record_store.save_record(record)
    return entry


def latest_success(record: dict[str, Any]) -> dict[str, Any] | None:
    """The record's latest successful analysis entry, if any."""
    analysis = record.get("analysis")
    if isinstance(analysis, dict) and analysis.get("status") == "success":
        return analysis
    for entry in reversed(record.get("analyses") or []):
        if isinstance(entry, dict) and entry.get("status") == "success":
            return entry
    return None


def history(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The record's full analysis history (oldest first)."""
    entries = record.get("analyses")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]
