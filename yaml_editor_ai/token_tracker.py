"""Independent token + execution tracking for YAML Editor AI.

Every agent execution — success, no-change, invalid response, API
failure — is appended to a persistent JSON store (default
``yaml_editor_ai/token_usage.json``) so analytics survive server
restarts. Token numbers are the ACTUAL usage values returned by the
Muse API (``usage.prompt_tokens`` / ``usage.completion_tokens``);
they are recorded as null when the API provides none — never
estimated.

This store is fully separate from the main pipeline's per-record
``token_usage`` (Muse resume extraction) — the two never mix.

The same store doubles as the duplicate-processing guard: a resume
that already has a COMPLETED analysis (status success / no_change)
is never analysed again — its information was already consumed
against the YAML, including any version its own analysis created.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

#: Execution statuses that count as "this resume was analysed".
COMPLETED_STATUSES = ("success", "no_change")


class TokenTracker:
    """Thread-safe append-only execution log with atomic persistence."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.Lock()

    # -- persistence -----------------------------------------------------
    def _read(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _write(self, entries: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent),
                                        prefix=self.path.name + ".",
                                        suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(entries, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # -- recording ---------------------------------------------------------
    def record(self, entry: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            entries = self._read()
            entries.append(entry)
            self._write(entries)
        return entry

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._read()

    # -- duplicate guard ---------------------------------------------------
    def already_analyzed(self, resume_id: str, yaml_version: str) -> bool:
        """True when this resume already has a COMPLETED analysis
        (failed attempts may be retried).

        The guard is per resume, not per version: a completed
        analysis consumed the resume's information against the YAML
        as it then stood — including any version that analysis
        itself created — so re-analysing the same resume against
        the version it just produced would only re-spend tokens.
        Analyses run against later versions created by OTHER
        resumes still carry this resume's completed entry, and the
        spec's rule ("not already analyzed for the same YAML
        version") is honoured in effect: a resume is analysed at
        most once until its own analysis is the stale one — the
        tracker keeps the version it was analysed against in
        ``yaml_version_before`` for audit."""
        with self._lock:
            for entry in self._read():
                if entry.get("resume_id") == resume_id \
                        and entry.get("status") in COMPLETED_STATUSES:
                    return True
        return False

    # -- analytics -----------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        entries = self.all()
        completed = [e for e in entries if e.get("status") in COMPLETED_STATUSES]
        failures = [e for e in entries if e.get("status") == "failed"]
        with_usage = [e for e in entries
                      if isinstance(e.get("input_tokens"), int)
                      and isinstance(e.get("output_tokens"), int)]
        total_in = sum(e["input_tokens"] for e in with_usage)
        total_out = sum(e["output_tokens"] for e in with_usage)
        durations = [e["duration_ms"] for e in entries
                     if isinstance(e.get("duration_ms"), (int, float))]
        return {
            "total_executions": len(entries),
            "successful_executions": len(
                [e for e in entries if e.get("status") == "success"]),
            "no_change_executions": len(
                [e for e in entries if e.get("status") == "no_change"]),
            "failed_executions": len(failures),
            "api_failures": len(failures),
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "total_tokens": total_in + total_out,
            "executions_with_usage": len(with_usage),
            "avg_tokens_per_execution": (
                round((total_in + total_out) / len(with_usage), 1)
                if with_usage else None),
            "avg_duration_ms": (
                round(sum(durations) / len(durations), 2)
                if durations else None),
            "total_keywords_suggested": sum(
                int(e.get("keywords_suggested") or 0) for e in entries),
            "total_keywords_accepted": sum(
                int(e.get("keywords_accepted") or 0) for e in entries),
            "total_keywords_rejected": sum(
                int(e.get("keywords_rejected") or 0) for e in entries),
        }

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Newest-first execution history for the analytics table
        (no resume content — metadata only)."""
        rows = []
        for entry in reversed(self.all()):
            rows.append({
                "resume_id": entry.get("resume_id"),
                "filename": entry.get("filename"),
                "timestamp": entry.get("timestamp"),
                "model": entry.get("model"),
                "input_tokens": entry.get("input_tokens"),
                "output_tokens": entry.get("output_tokens"),
                "total_tokens": (
                    entry["input_tokens"] + entry["output_tokens"]
                    if isinstance(entry.get("input_tokens"), int)
                    and isinstance(entry.get("output_tokens"), int)
                    else None),
                "keywords_suggested": entry.get("keywords_suggested"),
                "keywords_added": entry.get("keywords_accepted"),
                "keywords_rejected": entry.get("keywords_rejected"),
                "yaml_version": entry.get("yaml_version_after")
                or entry.get("yaml_version_before"),
                "status": entry.get("status"),
                "duration_ms": entry.get("duration_ms"),
                "error": entry.get("error"),
            })
            if len(rows) >= limit:
                break
        return rows
