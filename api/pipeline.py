"""Integration pipeline: ExtractionManager + QualityChecker.

This is the clean integration layer between the two existing packages:

- The ``extractor`` package's ExtractionManager accepts any policy
  object with ``accept(result)`` / ``describe_rejection(result)``
  — :class:`QualityGatePolicy` implements that protocol and runs the
  existing ``QualityChecker`` after every extraction attempt. The
  manager also supports an optional policy extension,
  ``best_candidate_result()``, which the gate uses to nominate the
  best held candidate after every extractor has been tried (the
  manager itself knows nothing about quality or candidates).
- The Quality Checker's filters and scores are used exactly as
  returned. This layer only maps the three filter scores to an overall
  PASS / WARNING / FAIL status and makes the fallback decision.

Gate decision policy (configurable — see :class:`QualityGatePolicy`
and the ``quality_gate:`` section of ``extractor.yaml``):
- PASS    — all three filters >= 75: accept immediately, stop fallback.
- WARNING — at least one filter < 75, but Text Health and Reading
            Order >= 50: the result is held as a *candidate* and the
            next extractor is tried when the warning is significant
            (lowest filter score < ``warning_continue_below``, default
            75, i.e. any WARNING continues); a WARNING at/above that
            threshold is accepted immediately. A missing section alone
            can never produce FAIL: Completeness is not a FAIL
            criterion, matching the Quality Checker's own policy that
            a missing section is a possible omission, not proof of
            failure.
- FAIL    — extraction failed, or Text Health < 50, or Reading Order
            < 50 (the text is technically unusable / garbled): reject
            and try the next extractor.
- If no extractor is accepted outright, the best held candidate is
  returned: highest weakest-filter score, then highest average score,
  then earliest in the configured order.

Per-resume pipeline trace
-------------------------
Every record built here also carries a structured ``pipeline_trace``
(plus ``primary_extractor``, ``attempt_count``, ``fallback_occurred``
and ``fallback_reason``). The trace is derived *only* from the real
``AttemptRecord`` history the ExtractionManager produced and the real
per-attempt QualityReports the gate collected — nothing in it is
simulated. Extractors that were never attempted appear as SKIPPED;
extractors whose text the Quality Checker refused appear as an
extractor PASS followed by a Quality Checker REJECTED stage carrying
the actual rejection reason the manager recorded. Records stored by
older versions (which have attempts/quality but no trace) get their
trace derived from that stored data on read — see
:func:`ensure_pipeline_trace`.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from extractor import ExtractionManager
from extractor.extractors.docling import DoclingExtractor
from extractor.extractors.ocr import OCRExtractor
from extractor.extractors.pymupdf import PyMuPDFExtractor
from extractor.extractors.tika import TikaExtractor
from extractor.models import ExtractionResult
from quality_checker import QualityChecker
from quality_checker.models import QualityReport

logger = logging.getLogger(__name__)

# --- Upload policy ------------------------------------------------------
# Supported extensions are derived from the actual backend: the union
# of the concrete extractors' supported types, plus the general
# formats the primary Tika extractor handles (DOC / TXT / HTML).
SUPPORTED_EXTENSIONS: set[str] = (
    set(DoclingExtractor.supported_extensions)
    | set(OCRExtractor.supported_extensions)
    | {"pdf", "doc", "txt", "html", "htm"}
)
MAX_UPLOAD_MB: int = int(os.environ.get("SATVIQ_MAX_UPLOAD_MB", "5"))
MAX_UPLOAD_BYTES: int = MAX_UPLOAD_MB * 1024 * 1024

# --- Quality status thresholds -------------------------------------------
FAIL_BELOW = 50.0    # for text_health / reading_order only
WARN_BELOW = 75.0    # any filter

_SIGNATURES: list[tuple[bytes, str]] = [
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"PK\x03\x04", "application/zip"),  # docx / xlsx / pptx containers
]
_ZIP_MIME_BY_EXT = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def detect_mime_type(path: Path) -> str:
    """Detect MIME type from magic bytes, refined by extension.

    Falls back to ``mimetypes`` and finally to ``application/octet-stream``
    (or ``text/plain`` for .txt). Detection only — files are never executed.
    """
    try:
        with path.open("rb") as handle:
            header = handle.read(8)
    except OSError:
        header = b""
    ext = path.suffix.lower().lstrip(".")
    for signature, mime in _SIGNATURES:
        if header.startswith(signature):
            if mime == "application/zip" and ext in _ZIP_MIME_BY_EXT:
                return _ZIP_MIME_BY_EXT[ext]
            return mime
    guessed, _ = mimetypes.guess_type(str(path))
    if guessed:
        return guessed
    return "text/plain" if ext == "txt" else "application/octet-stream"


def quality_status_for(report: QualityReport | None, extraction_success: bool,
                       *, fail_below: float = FAIL_BELOW,
                       warn_below: float = WARN_BELOW) -> str:
    """Map a real QualityReport to PASS / WARNING / FAIL (see module docstring)."""
    if not extraction_success or report is None:
        return "FAIL"
    if report.text_health.score < fail_below or report.reading_order.score < fail_below:
        return "FAIL"
    if min(report.text_health.score, report.completeness.score,
           report.reading_order.score) < warn_below:
        return "WARNING"
    return "PASS"


def _filter_scores(report: QualityReport) -> tuple[float, float, float]:
    return (report.text_health.score, report.completeness.score,
            report.reading_order.score)


def _candidate_rank(report: QualityReport) -> tuple[float, float]:
    """Candidate comparison key: (weakest filter score, average score).

    Candidates are compared first by their lowest of the three real
    Quality Checker scores (the weakest link), then by the average of
    the three. Position in the configured order breaks exact ties —
    the earlier candidate wins, so a later extractor never overwrites
    an earlier candidate without actually scoring better.
    """
    scores = _filter_scores(report)
    return (min(scores), sum(scores) / len(scores))


class QualityGatePolicy:
    """ExtractionManager acceptance policy backed by the QualityChecker.

    Implements the manager's policy protocol, plus its optional
    ``best_candidate_result()`` extension. Every extraction attempt
    that produced text is quality-checked with the real QualityChecker
    and decided on its actual status:

    - **PASS** — accept immediately; fallback stops.
    - **WARNING** — held as a *candidate* and the next extractor is
      tried only when a TECHNICAL filter (Text Health / Reading
      Order) makes the warning significant under the configured
      policy. A WARNING caused by Completeness alone is accepted
      immediately: Text Health and Reading Order pass, and missing
      resume sections describe the resume's content, not extraction
      quality — Completeness never triggers extractor fallback.
    - **FAIL** — reject and try the next extractor. FAIL requires
      Text Health or Reading Order below ``fail_below``; missing
      resume sections (Completeness) alone can never cause FAIL.

    If no extractor is accepted outright, the ExtractionManager asks
    for :meth:`best_candidate_result`, which nominates the best held
    candidate by :func:`_candidate_rank` — never simply the last one.

    Configuration (all optional, defaults shown; set them under
    ``quality_gate:`` in ``extractor/config/extractor.yaml``):

    - ``fail_below: 50`` — FAIL threshold for text_health/reading_order.
    - ``warn_below: 75`` — any filter below this makes the status WARNING.
    - ``warning_continue_below: 75`` — a WARNING continues to the next
      extractor when its lowest filter score is below this value.
    """

    def __init__(self, checker: QualityChecker, document_id: str,
                 config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self.checker = checker
        self.document_id = document_id
        self.fail_below: float = float(config.get("fail_below", FAIL_BELOW))
        self.warn_below: float = float(config.get("warn_below", WARN_BELOW))
        self.warning_continue_below: float = float(
            config.get("warning_continue_below", self.warn_below))
        self.reports: dict[str, QualityReport] = {}
        self.last_report: QualityReport | None = None
        # Per-attempt quality outcomes and gate decisions, in execution
        # order, so the API/trace can explain every attempt — not just
        # the accepted one. extractor_name -> {"status", "report",
        # "decision": "accept"|"continue"|"reject", "reason"}
        self.attempt_quality: dict[str, dict[str, Any]] = {}
        # WARNING extractions held while fallback continues:
        # {"extractor_name", "result", "report", "status"}
        self.candidates: list[dict[str, Any]] = []
        self.selected_extractor: str | None = None
        self.selection_reason: str | None = None
        self._best_candidate: dict[str, Any] | None = None
        self._attempt_counter = 0

    def status_for(self, report: QualityReport) -> str:
        return quality_status_for(report, extraction_success=True,
                                  fail_below=self.fail_below,
                                  warn_below=self.warn_below)

    def accept(self, result: ExtractionResult) -> bool:
        if not result.success:
            return False
        self._attempt_counter += 1
        report = self.checker.check(
            result.text,
            document_id=self.document_id,
            source_tool=result.extractor_name,
            attempt_id=f"attempt-{self._attempt_counter}",
        )
        self.reports[result.extractor_name] = report
        self.last_report = report
        status = self.status_for(report)
        scores = _filter_scores(report)
        lowest = min(scores)
        if status == "PASS":
            decision, reason, accepted = (
                "accept",
                "Quality PASS — result accepted; fallback stopped.",
                True,
            )
        elif status == "FAIL":
            decision, accepted = "reject", False
            reason = (
                "Quality Checker status FAIL for this extraction: "
                f"text_health={scores[0]:.1f}, completeness={scores[1]:.1f}, "
                f"reading_order={scores[2]:.1f} "
                f"(FAIL when text_health or reading_order < {self.fail_below:.0f}). "
                "Trying the next fallback extractor."
            )
            # A FAIL is rejected, but its text is still the best
            # available evidence if every extractor fails technically:
            # hold it as a fallback candidate so best_candidate_result
            # can nominate the least-broken text instead of returning
            # nothing. (Pre-existing behaviour: FAIL extractions such
            # as CORRUPT_TEXT were held this way; the candidate pool
            # therefore contains WARNING holds and FAIL rejects.)
            self.candidates.append({
                "extractor_name": result.extractor_name,
                "result": result,
                "report": report,
                "status": status,
            })
        elif (scores[1] < self.warn_below
                and min(scores[0], scores[2]) >= self.warn_below):
            # Extraction-only gating (same rule as the simple pipeline):
            # Text Health and Reading Order pass and the WARNING comes
            # from Completeness alone. Completeness describes the
            # resume's content, not extraction quality, so it never
            # triggers extractor fallback.
            decision, accepted = "accept", True
            reason = (
                "Extraction accepted "
                f"(text_health={scores[0]:.1f}, "
                f"completeness={scores[1]:.1f}, "
                f"reading_order={scores[2]:.1f}) — Text Health and "
                "Reading Order pass; the WARNING is Completeness only, "
                "which never triggers extractor fallback. "
                "Fallback stopped."
            )
        elif lowest >= self.warning_continue_below:
            decision, accepted = "accept", True
            reason = (
                "Quality WARNING, but not significant under the configured "
                f"policy (lowest filter score {lowest:.1f} >= "
                f"warning_continue_below {self.warning_continue_below:.1f}) — "
                "result accepted; fallback stopped."
            )
        else:
            decision, accepted = "continue", False
            reason = (
                "Quality WARNING is significant under the configured policy "
                f"(lowest filter score {lowest:.1f} < warning_continue_below "
                f"{self.warning_continue_below:.1f}) — result held as a "
                "candidate; trying the next extractor."
            )
            self.candidates.append({
                "extractor_name": result.extractor_name,
                "result": result,
                "report": report,
                "status": status,
            })
        self.attempt_quality[result.extractor_name] = {
            "status": status, "report": report,
            "decision": decision, "reason": reason,
        }
        if accepted:
            self.selected_extractor = result.extractor_name
            self.selection_reason = reason
        logger.info(
            "Quality gate: extractor=%s status=%s decision=%s scores "
            "th=%.1f comp=%.1f ro=%.1f",
            result.extractor_name, status, decision,
            scores[0], scores[1], scores[2])
        return accepted

    def describe_rejection(self, result: ExtractionResult) -> str:
        """The manager's recorded reason for a non-accepted attempt:
        the gate's actual decision reason (FAIL rejection, or WARNING
        candidate-hold), never a fabricated one."""
        if not result.success:
            return result.error or "Extraction failed."
        entry = self.attempt_quality.get(result.extractor_name)
        if entry is not None:
            return entry["reason"]
        return "Rejected by quality gate (no quality report available)."

    def best_candidate_result(self) -> ExtractionResult | None:
        """Nominate the best held candidate (manager extension point).

        Compares candidates by (weakest filter score, average score)
        from their real quality reports; exact ties keep the earlier
        candidate in configured order. Returns None when no candidate
        was held. Called by the ExtractionManager once, after every
        extractor has been tried without an outright acceptance.
        """
        if self._best_candidate is not None:
            return self._best_candidate["result"]
        if not self.candidates:
            return None
        best = self.candidates[0]
        for candidate in self.candidates[1:]:
            if _candidate_rank(candidate["report"]) > _candidate_rank(best["report"]):
                best = candidate
        self._best_candidate = best
        compared = "; ".join(
            f"{c['extractor_name']} (weakest {min(_filter_scores(c['report'])):.1f}, "
            f"average {sum(_filter_scores(c['report'])) / 3:.1f})"
            for c in self.candidates)
        weakest, average = _candidate_rank(best["report"])
        self.selected_extractor = best["extractor_name"]
        self.selection_reason = (
            f"Best candidate selected after all extractors were tried: "
            f"{best['extractor_name']} (quality WARNING; weakest filter "
            f"{weakest:.1f}, average {average:.1f}). Candidates compared: "
            f"{compared}."
        )
        logger.info("Quality gate: %s", self.selection_reason)
        return best["result"]


def _quality_payload(report: QualityReport | None, status: str) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "status": status,
        "document_id": report.document_id,
        "source_tool": report.source_tool,
        "attempt_id": report.attempt_id,
        "text_health": report.text_health.model_dump(),
        "completeness": report.completeness.model_dump(),
        "reading_order": report.reading_order.model_dump(),
    }


# --- Per-resume pipeline trace -------------------------------------------
# Display names for the configured extractor keys. Unknown keys (e.g.
# custom extractors injected in tests) are shown as-is.
_EXTRACTOR_DISPLAY: dict[str, str] = {
    "tika": "Tika",
    "pymupdf": "PyMuPDF",
    "docling": "Docling",
    "ocr": "OCR",
    "vision_llm": "Vision LLM",
    "muse_ai": "Muse AI",
}

REASON_UNAVAILABLE = "Reason unavailable"


def extractor_display_name(name: str | None) -> str:
    if not name:
        return "none"
    return _EXTRACTOR_DISPLAY.get(name, name)


def _configured_order() -> list[str]:
    """The backend's configured extractor order (deduped, in order)."""
    try:
        from extractor.manager import load_manager_config

        config = load_manager_config()
        order = list(config.get("manager", {}).get("order", []))
    except Exception:  # config unreadable — fall back to no order
        logger.exception("Could not read extractor config for pipeline trace")
        order = []
    return list(dict.fromkeys(order))


def _attempt_quality_map(policy: Any) -> dict[str, dict[str, Any]]:
    """Normalise a policy's per-attempt quality data to payloads.

    Returns extractor_name -> {"status", "quality": payload|None,
    "decision", "reason"}. Works with QualityGatePolicy (attempt_quality
    of QualityReports + gate decisions); policies without per-attempt
    data yield an empty map and the trace then relies on the attempts'
    accepted flags alone.
    """
    result: dict[str, dict[str, Any]] = {}
    raw = getattr(policy, "attempt_quality", None) or {}
    for name, entry in raw.items():
        status = entry.get("status")
        report = entry.get("report")
        result[name] = {
            "status": status,
            "quality": _quality_payload(report, status) if report is not None else None,
            "decision": entry.get("decision"),
            "reason": entry.get("reason"),
        }
    if not result:
        reports = getattr(policy, "reports", None) or {}
        for name, report in reports.items():
            status = quality_status_for(report, extraction_success=True)
            result[name] = {"status": status,
                            "quality": _quality_payload(report, status),
                            "decision": None, "reason": None}
    return result


def _stage(stage: str, type_: str, status: str, *, extractor: str | None = None,
           duration_ms: float | None = None, message: str | None = None,
           accepted: bool | None = None,
           quality: dict[str, Any] | None = None,
           decision: str | None = None,
           selected: bool | None = None) -> dict[str, Any]:
    return {
        "stage": stage,
        "type": type_,
        "extractor": extractor,
        "status": status,
        "duration_ms": duration_ms,
        "message": message,
        "accepted": accepted,
        "quality": quality,
        "decision": decision,
        "selected": selected,
    }


def _trace_scalars(attempts: list[dict[str, Any]], order: list[str]) -> dict[str, Any]:
    """Primary extractor / attempt count / fallback facts for a record."""
    primary = attempts[0]["extractor_name"] if attempts else (order[0] if order else None)
    fallback_reason: str | None = None
    if len(attempts) > 1:
        first_failed = next((a for a in attempts if not a.get("accepted")), None)
        if first_failed is not None:
            fallback_reason = first_failed.get("error") or REASON_UNAVAILABLE
    return {
        "primary_extractor": primary,
        "attempt_count": len(attempts),
        "fallback_occurred": len(attempts) > 1,
        "fallback_reason": fallback_reason,
    }


def build_pipeline_trace(attempts: list[dict[str, Any]], *,
                         order: list[str],
                         attempt_quality: dict[str, dict[str, Any]],
                         success: bool,
                         extractor_used: str | None,
                         error: str | None,
                         mime_type: str | None,
                         total_duration_ms: float | None = None) -> list[dict[str, Any]]:
    """Build the per-resume execution trace from real attempt data.

    Timeline: Resume Upload → MIME Type Detection → for each extractor
    in the configured order, its extractor stage (and Quality Checker
    stage when it actually ran) or a SKIPPED stage when it was never
    attempted → Final Extraction Result. Statuses come from the
    attempts themselves: FAIL = extraction failed, REJECTED = the
    Quality Checker refused the text, PASS/WARNING = the gate's real
    per-attempt quality status, SKIPPED = never attempted / did not run.
    """
    stages: list[dict[str, Any]] = [
        _stage("Resume Upload", "upload", "PASS",
               message="File received and stored for processing."),
        _stage("MIME Type Detection", "mime", "PASS",
               message=mime_type or REASON_UNAVAILABLE),
    ]
    by_name = {a["extractor_name"]: a for a in attempts}
    selected_name = extractor_used if success else None

    def decision_for(name: str, attempt: dict[str, Any]) -> str:
        """The gate's decision for an attempt: from the policy's own
        record when available; legacy records fall back to the stored
        accepted flag (their only decision data)."""
        q = attempt_quality.get(name) or {}
        if q.get("decision"):
            return str(q["decision"])
        if not attempt.get("success"):
            return "reject"
        return "accept" if attempt.get("accepted") else "reject"

    for name in order:
        display = extractor_display_name(name)
        attempt = by_name.get(name)
        if attempt is None:
            promoted = bool(
                success and extractor_used in by_name
                and decision_for(extractor_used, by_name[extractor_used]) == "continue")
            if success and extractor_used and not promoted:
                message = (f"Skipped — {extractor_display_name(extractor_used)} was "
                           "accepted before this extractor was needed.")
            else:
                message = "Not attempted."
            stages.append(_stage(f"{display} Extractor", "extractor", "SKIPPED",
                                 extractor=name, message=message,
                                 selected=False))
            continue
        decision = decision_for(name, attempt)
        is_selected = bool(selected_name == name and decision in ("accept", "continue"))
        if attempt.get("success"):
            ext_message = {
                "accept": "Extraction succeeded and was accepted.",
                "continue": ("Extraction succeeded; its output was held as a "
                             "candidate while fallback continued."),
                "reject": ("Extraction succeeded; its output was sent to the "
                           "Quality Checker."),
            }[decision]
            stages.append(_stage(f"{display} Extractor", "extractor", "PASS",
                                 extractor=name,
                                 duration_ms=attempt.get("duration_ms"),
                                 message=ext_message,
                                 accepted=bool(attempt.get("accepted")),
                                 decision=decision, selected=is_selected))
        else:
            stages.append(_stage(f"{display} Extractor", "extractor", "FAIL",
                                 extractor=name,
                                 duration_ms=attempt.get("duration_ms"),
                                 message=attempt.get("error") or REASON_UNAVAILABLE,
                                 accepted=False, decision="reject",
                                 selected=False))
        # Quality Checker stage for this attempt.
        if not attempt.get("success"):
            stages.append(_stage("Quality Checker", "quality", "SKIPPED",
                                 extractor=name,
                                 message="Quality Checker did not run because extraction failed.",
                                 selected=False))
        else:
            q = attempt_quality.get(name) or {}
            payload = q.get("quality")
            reason = q.get("reason")
            if decision == "accept":
                q_status = q.get("status") or "PASS"
                q_message = reason or (
                    "Quality checks completed with warnings — extraction accepted."
                    if q_status == "WARNING" else
                    "Quality checks passed — extraction accepted.")
                stages.append(_stage("Quality Checker", "quality", q_status,
                                     extractor=name, message=q_message,
                                     accepted=True, quality=payload,
                                     decision="accept", selected=is_selected))
            elif decision == "continue":
                q_message = reason or (
                    "Quality WARNING — result held as a candidate; "
                    "trying the next extractor.")
                if is_selected:
                    q_message += (" Selected as the best candidate after all "
                                  "extractors were tried.")
                elif success:
                    q_message += (" A later extractor was accepted, so this "
                                  "candidate was not selected.")
                stages.append(_stage("Quality Checker", "quality", "WARNING",
                                     extractor=name, message=q_message,
                                     accepted=False, quality=payload,
                                     decision="continue", selected=is_selected))
            else:
                stages.append(_stage("Quality Checker", "quality", "REJECTED",
                                     extractor=name,
                                     message=reason or attempt.get("error") or REASON_UNAVAILABLE,
                                     accepted=False, quality=payload,
                                     decision="reject", selected=False))
    if success:
        selected_decision = None
        if extractor_used and extractor_used in by_name:
            selected_decision = decision_for(extractor_used, by_name[extractor_used])
        if selected_decision == "continue":
            final_message = (f"Final accepted text produced by "
                             f"{extractor_display_name(extractor_used)} — best "
                             "candidate selected after all extractors were tried.")
        else:
            final_message = (f"Final accepted text produced by "
                             f"{extractor_display_name(extractor_used)}.")
    else:
        final_message = error or REASON_UNAVAILABLE
    stages.append(_stage("Final Extraction Result", "final",
                         "PASS" if success else "FAIL",
                         extractor=extractor_used,
                         duration_ms=total_duration_ms,
                         message=final_message,
                         accepted=bool(success),
                         selected=bool(success)))
    return stages


def build_record_from_result(*, resume_id: str, original_filename: str,
                             path: Path, mime_type: str, file_size: int,
                             result: ExtractionResult, policy: Any,
                             order: list[str],
                             ) -> dict[str, Any]:
    """Assemble the stored/API record for one processed resume.

    Shared by :func:`process_resume_file` (real backend extractors) and
    by tests that drive the real ExtractionManager + QualityGatePolicy
    with fake extractors, so both paths produce identical records —
    including the pipeline trace.
    """
    attempt_quality = _attempt_quality_map(policy)
    if result.success:
        reports = getattr(policy, "reports", {}) or {}
        report = reports.get(result.extractor_name) or getattr(policy, "last_report", None)
    else:
        report = getattr(policy, "last_report", None)
    status_for = getattr(policy, "status_for", None)
    if result.success and report is not None and callable(status_for):
        status = status_for(report)
    else:
        status = quality_status_for(report, extraction_success=result.success)

    selected_name = result.extractor_name if result.success else None
    attempts: list[dict[str, Any]] = []
    for index, raw_attempt in enumerate(result.attempts, start=1):
        entry = raw_attempt.model_dump()
        q = attempt_quality.get(entry["extractor_name"])
        if q is not None and entry.get("success"):
            entry["quality_status"] = q["status"]
            entry["quality"] = q["quality"]
            entry["decision"] = q.get("decision") or (
                "accept" if entry.get("accepted") else "reject")
            entry["decision_reason"] = q.get("reason") or entry.get("error")
        else:
            entry["quality_status"] = None
            entry["quality"] = None
            entry["decision"] = "reject" if not entry.get("success") else (
                "accept" if entry.get("accepted") else "reject")
            entry["decision_reason"] = entry.get("error")
        entry["selected"] = bool(
            selected_name == entry["extractor_name"]
            and entry["decision"] in ("accept", "continue"))
        # Full per-attempt transparency fields: every attempt keeps its
        # OWN extracted text, quality report and fallback decision, so
        # any tool's output can be inspected after the run — nothing is
        # overwritten by a later extractor.
        entry["attempt_id"] = f"{resume_id}-attempt-{index}"
        entry["resume_id"] = resume_id
        entry["attempt_number"] = index
        entry["extraction_status"] = "SUCCESS" if entry.get("success") else "FAILED"
        entry["extracted_text"] = entry.get("text") or ""
        entry["extracted_text_length"] = len(entry["extracted_text"])
        entry["error_message"] = entry.get("error")
        entry["quality_checker_report"] = entry.get("quality")
        entry["fallback_decision"] = entry.get("decision")
        entry["fallback_reason"] = entry.get("decision_reason")
        entry["is_final_selected"] = bool(entry.get("selected"))
        entry["ocr"] = _ocr_info(entry.get("metadata"))
        entry["timeout_seconds"] = None
        entry["model_config_id"] = None
        entry["provider"] = None
        entry["model"] = None
        entry["json_validation"] = None
        entry["input_type"] = None
        attempts.append(entry)

    # Why the final extractor was chosen: the policy's own selection
    # reason (best-candidate promotion or immediate acceptance) when it
    # recorded one; otherwise the selected attempt's decision reason.
    selection_reason = getattr(policy, "selection_reason", None)
    if result.success and not selection_reason:
        chosen = next((a for a in attempts if a.get("selected")), None)
        selection_reason = (chosen or {}).get("decision_reason")
    if not result.success:
        selection_reason = (
            "No candidate was selected — every extractor either failed "
            "extraction or was rejected by the Quality Checker.")

    scalars = _trace_scalars(attempts, order)
    record: dict[str, Any] = {
        "resume_id": resume_id,
        "filename": original_filename,
        "file_type": path.suffix.lower().lstrip(".") or None,
        "mime_type": mime_type,
        "file_size_bytes": file_size,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "success": result.success,
        "extractor_used": result.extractor_name if result.success else None,
        "duration_ms": result.duration_ms,
        "attempts": attempts,
        "extracted_text": result.text if result.success else "",
        "page_count": result.page_count,
        "warnings": result.warnings,
        "error": None if result.success else result.error,
        "quality_status": status,
        "final_quality_status": status,
        "final_extracted_text": result.text if result.success else "",
        "quality": _quality_payload(report, status),
        "selection_reason": selection_reason,
        "extractor_order": list(order),
        "extraction_timeout_seconds": None,
        "resume_json": None,
        "hyperlinks": [],
        "hyperlink_error": None,
        "text_normalization": None,
        "muse_requests": [],
        "token_usage": None,
        "resume_json_source": None,
        "resume_json_meta": None,
        **scalars,
        "pipeline_trace": build_pipeline_trace(
            attempts, order=order, attempt_quality=attempt_quality,
            success=result.success,
            extractor_used=result.extractor_name if result.success else None,
            error=None if result.success else result.error,
            mime_type=mime_type, total_duration_ms=result.duration_ms,
        ),
    }
    _ensure_attempt_normalized_texts(record)
    return record


def _ocr_info(metadata: Any) -> dict[str, Any] | None:
    """OCR facts for one attempt, lifted from the extractor result's
    metadata (Tika records them — see extractor/extractors/tika.py).
    None when the attempt carries no OCR information (non-Tika
    extractors, or records stored before OCR integration)."""
    if not isinstance(metadata, dict) or "ocr_enabled" not in metadata:
        return None
    return {key: metadata.get(key) for key in (
        "ocr_enabled", "ocr_used", "ocr_language", "ocr_strategy",
        "ocr_tesseract_path", "ocr_evidence")}


def _normalize_attempt_fields(record: dict[str, Any]) -> None:
    """Backfill the full per-attempt transparency fields on records
    stored before they existed (in place).

    Per-attempt texts were not stored by older builds; the one text
    that WAS stored — the final extracted text — belongs to the
    selected attempt, so it is restored onto that attempt only. Other
    attempts honestly keep an empty text (their output was never
    persisted), never a copy of another tool's output.
    """
    resume_id = record.get("resume_id") or "record"
    final_text = record.get("extracted_text") or ""
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(record.get("attempts") or [], start=1):
        entry = dict(raw)
        entry.setdefault("attempt_id", f"{resume_id}-attempt-{index}")
        entry.setdefault("resume_id", record.get("resume_id"))
        entry.setdefault("attempt_number", index)
        entry.setdefault("extraction_status",
                         "SUCCESS" if entry.get("success") else "FAILED")
        if not entry.get("extracted_text"):
            is_selected = bool(entry.get("selected")) or bool(
                record.get("success") and entry.get("accepted")
                and entry.get("extractor_name") == record.get("extractor_used"))
            entry["extracted_text"] = final_text if is_selected \
                else (entry.get("text") or "")
        entry.setdefault("text", entry.get("extracted_text") or "")
        entry["extracted_text_length"] = len(entry.get("extracted_text") or "")
        entry.setdefault("error_message", entry.get("error"))
        entry.setdefault("quality_checker_report", entry.get("quality"))
        entry.setdefault("fallback_decision", entry.get("decision"))
        entry.setdefault("fallback_reason", entry.get("decision_reason"))
        entry.setdefault("is_final_selected", bool(entry.get("selected")))
        entry.setdefault("extractor_version", None)
        entry.setdefault("started_at", None)
        entry.setdefault("completed_at", None)
        entry.setdefault("error_type", None)
        entry.setdefault("error_traceback", None)
        if not entry.get("ocr"):
            # Lift the OCR facts from the attempt's stored extractor
            # metadata when the attempt itself recorded none (the
            # concurrent pipeline stored ocr=None while keeping the
            # metadata). _ocr_info returns None when the metadata
            # carries no OCR information, so attempts without OCR
            # evidence honestly keep None — nothing is invented.
            entry["ocr"] = _ocr_info(entry.get("metadata"))
        entry.setdefault("timeout_seconds", None)
        entry.setdefault("model_config_id", None)
        entry.setdefault("provider", None)
        entry.setdefault("model", None)
        entry.setdefault("json_validation", None)
        entry.setdefault("input_type", None)
        entry.setdefault("duration_ms", 0.0)
        entry.setdefault("accepted", False)
        normalized.append(entry)
    record["attempts"] = normalized
    record.setdefault("extraction_timeout_seconds", None)
    record.setdefault("resume_json", None)
    record.setdefault("resume_json_source", None)
    record.setdefault("resume_json_meta", None)
    record.setdefault("direct_llm_routing", None)
    record.setdefault("final_quality_status", record.get("quality_status"))
    record.setdefault("final_extracted_text",
                      record.get("extracted_text") or "")


# ---------------------------------------------------------------------------
# Muse request / token-usage accounting
# ---------------------------------------------------------------------------

def _tika_text_for_record(record: dict[str, Any]) -> str | None:
    """The Tika attempt's own extracted text (supporting context for
    Muse), or None when Tika produced no usable text."""
    for attempt in record.get("attempts") or []:
        if attempt.get("extractor_name") == "tika":
            text = attempt.get("extracted_text") \
                or attempt.get("text") or ""
            return text if text.strip() else None
    return None


def _usage_numbers(usage: Any) -> dict[str, Any]:
    """Normalize one API response's usage object. Values are the
    API's own numbers — never estimated. total falls back to
    input+output only when the API omitted total_tokens."""
    usage = usage if isinstance(usage, dict) else {}
    inp = usage.get("prompt_tokens")
    out = usage.get("completion_tokens")
    tot = usage.get("total_tokens")
    details = usage.get("prompt_tokens_details")
    cached = details.get("cached_tokens") \
        if isinstance(details, dict) else None
    if tot is None and inp is not None and out is not None:
        tot = inp + out
    return {"input_tokens": inp, "output_tokens": out,
            "total_tokens": tot, "cached_input_tokens": cached,
            "usage_available": any(
                v is not None for v in (inp, out, tot))}


def _muse_request_entry(stage: str, outcome: dict[str, Any],
                        status: str) -> dict[str, Any]:
    entry = {"stage": stage, "model": outcome.get("model"),
             "request_id": outcome.get("request_id"),
             "status": status,
             "duration_ms": outcome.get("duration_ms")}
    entry.update(_usage_numbers(outcome.get("usage")))
    return entry


def _aggregate_token_usage(requests: list[dict[str, Any]],
                           context_window: int | None) -> dict[str, Any]:
    """Per-resume aggregate over its individual Muse requests.
    Each request is counted exactly once. Context status is based
    on the LARGEST single request's input tokens (a context window
    is per-request, not cumulative), and remaining capacity is
    window − input tokens only."""
    def _sum(field: str) -> int | None:
        values = [r[field] for r in requests if r.get(field) is not None]
        return sum(values) if values else None

    models = sorted({r["model"] for r in requests if r.get("model")})
    context = None
    inputs = [r["input_tokens"] for r in requests
              if r.get("input_tokens") is not None]
    if context_window and inputs:
        largest = max(inputs)
        percent = round(largest / context_window * 100, 2)
        context = {
            "window_tokens": context_window,
            "largest_request_input_tokens": largest,
            "remaining_tokens": context_window - largest,
            "percent_used": percent,
            "status": ("Context limit exceeded" if percent >= 100
                       else "Near context limit" if percent >= 80
                       else "Normal usage"),
        }
    return {
        "input_tokens": _sum("input_tokens"),
        "output_tokens": _sum("output_tokens"),
        "total_tokens": _sum("total_tokens"),
        "cached_input_tokens": _sum("cached_input_tokens"),
        "api_calls": len(requests),
        "successful_calls": sum(
            1 for r in requests if r["status"] == "success"),
        "failed_calls": sum(
            1 for r in requests if r["status"] == "failed"),
        "timed_out_calls": sum(
            1 for r in requests if r["status"] == "timeout"),
        "models": models,
        "usage_available": any(
            r.get("usage_available") for r in requests),
        "context": context,
    }


def _request_status(outcome: dict[str, Any]) -> str:
    if outcome.get("ok"):
        return "success"
    if outcome.get("error_type") == "TimeoutError":
        return "timeout"
    return "failed"


def _derive_token_usage(record: dict[str, Any]) -> None:
    """Backfill muse_requests/token_usage for records stored before
    token monitoring existed (their Muse attempts and resume_json
    metadata already carry the API usage). The vision request is
    counted once even when it also produced the final Resume JSON —
    resume_json_meta mirrors it in that case and is skipped."""
    if record.get("token_usage") is not None:
        return
    requests: list[dict[str, Any]] = []
    for attempt in record.get("attempts") or []:
        if attempt.get("extractor_name") != "muse_ai":
            continue
        if attempt.get("error_type") == "ConfigurationError":
            continue  # no API call was made
        meta = attempt.get("metadata") or {}
        requests.append(_muse_request_entry("vision", {
            "usage": meta.get("usage") or {},
            "model": attempt.get("model"),
            "request_id": meta.get("request_id"),
            "duration_ms": attempt.get("duration_ms"),
        }, "success" if attempt.get("success") else (
            "timeout" if attempt.get("error_type") == "TimeoutError"
            else "failed")))
    meta = record.get("resume_json_meta") or {}
    vision_was_the_source = (
        record.get("extractor_used") == "muse_ai"
        and record.get("resume_json_source") == "muse_ai")
    if meta and not vision_was_the_source and meta.get("usage"):
        requests.append(_muse_request_entry("resume_json", {
            "usage": meta.get("usage") or {},
            "model": meta.get("model"),
            "request_id": meta.get("request_id"),
            "duration_ms": meta.get("duration_ms"),
        }, "success" if not meta.get("error") else "failed"))
    if not requests:
        return
    window = None
    try:
        from muse_ai import load_muse_config

        window = load_muse_config().context_window_tokens
    except Exception:
        window = None
    record["muse_requests"] = requests
    record["token_usage"] = _aggregate_token_usage(requests, window)


def _backfill_hyperlinks(record: dict[str, Any]) -> None:
    """Records stored before hyperlink extraction existed regain
    their annotations on read from the stored original upload —
    deterministic data from the file itself, never persisted back."""
    if "hyperlinks" in record:
        return
    record["hyperlinks"] = []
    record.setdefault("hyperlink_error", None)
    stored_name = record.get("_stored_name")
    if not stored_name:
        return
    candidate = (Path(__file__).resolve().parent.parent
                 / "uploads" / stored_name)
    if candidate.suffix.lower() == ".pdf" and candidate.exists():
        from .hyperlinks import extract_pdf_hyperlinks

        result = extract_pdf_hyperlinks(candidate)
        record["hyperlinks"] = result["hyperlinks"]
        record["hyperlink_error"] = result["error"]


def _ensure_attempt_normalized_texts(record: dict[str, Any]) -> None:
    """Per-attempt normalized text, for the Inspector's per-tool text
    viewer (each extractor's own text + its normalized version).

    Computed on read from each attempt's OWN stored text with the
    same deterministic normalizer used for the selected text — the
    stored original is never modified, and nothing is persisted back.
    STRICT RULE: Muse AI attempts are never normalized (their text
    is the model's raw response, not extractor output), so they get
    ``normalized_text = None``.
    """
    from .text_normalization import normalize_text

    for attempt in record.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        if attempt.get("extractor_name") == "muse_ai":
            attempt["normalized_text"] = None
            continue
        if attempt.get("normalized_text") is not None:
            continue
        text = attempt.get("extracted_text") or attempt.get("text") or ""
        if not text:
            attempt["normalized_text"] = ""
            continue
        try:
            attempt["normalized_text"] = normalize_text(text)
        except Exception:
            logger.exception(
                "Per-attempt normalization failed for extractor %s",
                attempt.get("extractor_name"))
            attempt["normalized_text"] = None


def ensure_pipeline_trace(record: dict[str, Any]) -> dict[str, Any]:
    """Fill trace fields for records stored before traces existed.

    Older records already store the real attempt history and the final
    quality report, so their trace is derived from that stored data —
    per-attempt quality detail is attached only for the extractor the
    stored quality report actually belongs to (its ``source_tool``);
    other attempts show their recorded accepted flag and error/reason.
    Records that already carry a trace keep it; their attempts still
    gain any missing per-attempt transparency fields.
    """
    _normalize_attempt_fields(record)
    _derive_token_usage(record)
    _backfill_hyperlinks(record)
    _restore_chain_extracted_text(record)
    _ensure_text_normalization(record)
    _ensure_attempt_normalized_texts(record)
    record.setdefault("extractor_results", {})
    if record.get("pipeline_trace"):
        return record
    attempts: list[dict[str, Any]] = []
    for raw in record.get("attempts") or []:
        entry = dict(raw)
        entry.setdefault("quality_status", None)
        entry.setdefault("quality", None)
        # Legacy records predate gate-decision tracking; the stored
        # accepted flag is the only decision data they have.
        entry.setdefault("decision",
                         "accept" if entry.get("accepted") else "reject")
        entry.setdefault("decision_reason", entry.get("error"))
        entry.setdefault("selected", bool(
            record.get("success") and entry.get("accepted")
            and entry["extractor_name"] == record.get("extractor_used")))
        attempts.append(entry)
    order = list(record.get("extractor_order") or []) or _configured_order()
    for name in [a["extractor_name"] for a in attempts]:
        if name not in order:
            order.append(name)
    attempt_quality: dict[str, dict[str, Any]] = {}
    stored_quality = record.get("quality")
    if stored_quality and stored_quality.get("source_tool"):
        source = stored_quality["source_tool"]
        attempt_quality[source] = {
            "status": record.get("quality_status"),
            "quality": stored_quality,
        }
        for entry in attempts:
            if entry["extractor_name"] == source and entry.get("success"):
                entry["quality_status"] = (
                    record.get("quality_status") if entry.get("accepted") else "FAIL"
                )
                entry["quality"] = stored_quality
    record["attempts"] = attempts
    record["extractor_order"] = order
    if not record.get("selection_reason"):
        record["selection_reason"] = (
            f"Final text produced by {extractor_display_name(record.get('extractor_used'))}."
            if record.get("success") else
            "No candidate was selected — every extractor failed or was rejected.")
    record.update(_trace_scalars(attempts, order))
    record["pipeline_trace"] = build_pipeline_trace(
        attempts, order=order, attempt_quality=attempt_quality,
        success=bool(record.get("success")),
        extractor_used=record.get("extractor_used"),
        error=record.get("error"),
        mime_type=record.get("mime_type"),
        total_duration_ms=record.get("duration_ms"),
    )
    return record


def _restore_chain_extracted_text(record: dict[str, Any]) -> None:
    """Repair records stored before the strict separation rule.

    Older builds wrote the Muse vision stage's raw response into
    ``extracted_text`` / ``final_extracted_text`` when Muse
    produced the final Resume JSON. Those fields must hold only the
    non-Muse extractor chain's raw text, so when a Muse-final
    record's extracted text is exactly the Muse attempt's own
    text, it is restored here (on read; the stored file is not
    rewritten) to the selected chain attempt's text — or "" when
    the chain produced no text. Muse's output stays available in
    its attempt and in ``resume_json``, unchanged.
    """
    if record.get("extractor_used") != "muse_ai" \
            and record.get("final_extractor") != "muse_ai":
        return
    muse_texts = {
        (a.get("extracted_text") or a.get("text") or "")
        for a in (record.get("attempts") or [])
        if a.get("extractor_name") == "muse_ai"}
    muse_texts.discard("")
    if not muse_texts:
        return
    chain_text, _source = _selected_text_for_record(record)
    if record.get("extracted_text") in muse_texts:
        record["extracted_text"] = chain_text or ""
    if record.get("final_extracted_text") in muse_texts:
        record["final_extracted_text"] = chain_text or ""


def _selected_text_for_record(
        record: dict[str, Any]) -> tuple[str | None, str]:
    """The text of the extractor the pipeline's extractor chain
    SELECTED, with that extractor's name. The Muse vision attempt
    is a downstream Muse stage, not a chain selection, so it is
    never the normalization source. Falls back through the Quality
    Report's source tool, the record's final/used extractor name,
    its final extracted text, and finally the first chain attempt
    with usable text; (None, name) when no chain attempt produced
    usable text. Tika is never assumed."""
    attempts = [a for a in (record.get("attempts") or [])
                if a.get("extractor_name") != "muse_ai"]

    def _usable(attempt: dict[str, Any]) -> str:
        return attempt.get("extracted_text") \
            or attempt.get("text") or ""

    selected = next(
        (a for a in attempts if a.get("is_final_selected")), None)
    if selected is None:
        quality = record.get("quality")
        source_tool = quality.get("source_tool") \
            if isinstance(quality, dict) else None
        selected = next(
            (a for a in attempts
             if a.get("extractor_name") == source_tool), None)
    if selected is None:
        name = record.get("final_extractor") \
            or record.get("extractor_used")
        if name != "muse_ai":
            selected = next(
                (a for a in attempts
                 if a.get("extractor_name") == name), None)
    if selected is None:
        final_text = record.get("final_extracted_text") \
            or record.get("extracted_text")
        if final_text:
            selected = next(
                (a for a in attempts if _usable(a) == final_text),
                None)
    if selected is None:
        selected = next((a for a in attempts if _usable(a).strip()),
                        None)
    if selected is None:
        return None, "none"
    text = _usable(selected)
    return (text if text.strip() else None), \
        selected.get("extractor_name") or "selected"


def _ensure_text_normalization(record: dict[str, Any]) -> dict[str, Any]:
    """Compute (or refresh) the record's text_normalization block
    from the SELECTED extractor's ORIGINAL text — whichever
    extractor the pipeline selected, not necessarily Tika. The
    attempt itself is never modified; original and normalized live
    side by side in the block. The block is recomputed whenever it
    is missing, predates step tracking, or no longer matches the
    selected extractor's text (e.g. a stale "unavailable" block
    stored while the record does hold extracted text — the
    metadata is then calculated from the real text instead of
    showing false zeros)."""
    from .text_normalization import normalization_block

    selected_text, source = _selected_text_for_record(record)
    block = record.get("text_normalization")
    if block is None or "normalization_steps" not in block \
            or block.get("original_extracted_text") != selected_text:
        block = normalization_block(selected_text, source=source)
        record["text_normalization"] = block
    return block


def _tika_normalization_block(record: dict[str, Any]) -> dict[str, Any]:
    """The normalization block for the Tika attempt's own text —
    used ONLY for the vision stage's supporting text and its
    metadata. The record-level block describes the SELECTED
    extractor's text instead."""
    from .text_normalization import normalization_block

    return normalization_block(
        _tika_text_for_record(record), source="tika")


def _tika_text_to_send(record: dict[str, Any]) -> str | None:
    """What Muse receives as the Tika supporting text: the
    NORMALIZED text when normalization applied; the unchanged
    original when it was bypassed (recorded); None when Tika
    produced no text."""
    block = _tika_normalization_block(record)
    original = _tika_text_for_record(record)
    if block.get("status") == "applied":
        return block.get("normalized_text")
    if block.get("status") == "bypassed":
        return original
    return None


def _normalization_info(block: dict[str, Any]) -> dict[str, Any]:
    """The resume_json_meta summary of one normalization block."""
    return {"source": block["source"], "status": block["status"],
            "original_chars": block["original_chars"],
            "normalized_chars": block["normalized_chars"],
            "estimated_reduction_percent":
                block["estimated_reduction_percent"],
            "normalization_applied":
                block.get("normalization_applied", False),
            "normalization_steps":
                block.get("normalization_steps", []),
            "error": block.get("error")}


def _normalize_for_muse(record: dict[str, Any],
                        text: str) -> tuple[str, dict[str, Any]]:
    """Normalize an extracted text destined for Muse (the JSON
    generation input). Reuses the record's block when the text IS
    the selected extractor's text (the block already holds exactly
    that normalization); otherwise normalizes this text and
    reports its own metrics. On bypass the original is returned
    unchanged and the info says so."""
    from .text_normalization import normalization_block

    block = _ensure_text_normalization(record)
    if block.get("original_extracted_text") is not None \
            and text == block["original_extracted_text"]:
        if block.get("status") == "applied":
            return block["normalized_text"], _normalization_info(block)
        return text, _normalization_info(block)
    block = normalization_block(
        text, source=record.get("extractor_used") or "extracted_text")
    if block.get("status") == "applied":
        return block["normalized_text"], _normalization_info(block)
    return text, _normalization_info(block)


def _default_muse_service() -> Any | None:
    try:
        from muse_ai import MuseService, load_muse_config

        return MuseService(load_muse_config())
    except Exception:
        logger.exception("Muse AI service unavailable; Muse stages skipped")
        return None


def _sources_message(outcome: dict[str, Any]) -> str:
    """One honest sentence naming the sources a Muse request
    actually contained — built from the outcome's own flags."""
    parts = []
    if outcome.get("visual_provided"):
        parts.append("original document")
    if outcome.get("tika_text_provided"):
        parts.append("Tika extracted text")
    if outcome.get("hyperlinks_provided"):
        parts.append(
            f"{outcome.get('hyperlink_count') or 0} PDF hyperlink "
            "annotation(s)")
    if not parts:
        return "Muse AI request sent."
    return "Sent to Muse AI: " + " + ".join(parts) + "."


def _muse_unavailable_outcome(service: Any, path: Path,
                              error: str) -> dict[str, Any]:
    """Outcome for a Muse stage that could not even be attempted
    (not configured / vision disabled) — recorded, never hidden."""
    suffix = path.suffix.lower()
    input_type = ("pdf" if suffix == ".pdf" else "image"
                  if suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif")
                  else "unsupported")
    return {"ok": False, "error": error,
            "error_type": "ConfigurationError", "resume_json": None,
            "validation": None, "provider": "Muse AI",
            "model": getattr(service, "model", None), "raw_text": None,
            "usage": {}, "duration_ms": None,
            "timeout_seconds": getattr(
                getattr(service, "config", None), "timeout_seconds", None),
            "input_type": input_type, "page_count": None,
            "pages_processed": 0, "pages_skipped": 0}


def _apply_muse_stages(record: dict[str, Any], path: Path, service: Any,
                       timeout_seconds: float | None) -> None:
    """Muse AI stages (in place): direct image/PDF extraction as the
    last fallback, then Resume JSON generation from accepted text.

    The ``timeout_seconds`` parameter is accepted for call
    compatibility only — the per-attempt Extraction Timeout feature
    was removed, so no stage is wall-clock capped here any more.

    Muse is configured ONLY through the backend .env — there are no
    frontend model settings. The direct-extraction stage receives
    the ORIGINAL file (never the incomplete extracted text) and runs
    only when the extractor chain did not reach a Quality Checker
    PASS. When Muse is not configured, a failed Muse attempt with
    the configuration error is recorded so the gap is visible
    instead of silent. The Quality Checker is never involved here.
    """
    import time as _time

    extra_stages: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = list(record.get("muse_requests") or [])
    _ensure_text_normalization(record)

    # PDF hyperlink annotations: deterministic data from the file's
    # own annotation layer (extracted text never contains them).
    # Failure is recorded and the pipeline continues without them.
    if path.suffix.lower() == ".pdf":
        from .hyperlinks import extract_pdf_hyperlinks

        link_data = extract_pdf_hyperlinks(path)
        record["hyperlinks"] = link_data["hyperlinks"]
        record["hyperlink_error"] = link_data["error"]
    hyperlinks = record.get("hyperlinks") or []

    # --- Direct image/PDF extraction (final fallback) ------------------
    # Direct-LLM routing exception: a record promoted by
    # _apply_direct_llm_routing has READABLE text whose only problem
    # is a heading-detection gap — it goes straight to the text stage
    # below, so the vision fallback is skipped while Muse is
    # configured. When Muse is NOT configured the block still runs,
    # so the configuration gap is recorded as visibly as for any
    # other non-PASS record.
    _routed_direct = bool(
        (record.get("direct_llm_routing") or {}).get("routed"))
    if record.get("final_quality_status") != "PASS" and not (
            _routed_direct and getattr(service, "configured", False)):
        if getattr(service, "configured", False) and \
                getattr(service, "vision_enabled", False):
            started = _time.perf_counter()
            outcome = service.vision_extract(
                path, tika_text=_tika_text_to_send(record),
                hyperlinks=hyperlinks)
            requests.append(_muse_request_entry(
                "vision", outcome, _request_status(outcome)))
            duration_ms = outcome.get("duration_ms")
            if duration_ms is None:
                duration_ms = round(
                    (_time.perf_counter() - started) * 1000, 2)
        elif not getattr(service, "configured", False):
            outcome = _muse_unavailable_outcome(
                service, path,
                getattr(service, "configuration_error", None)
                or "Muse AI is not configured.")
            duration_ms = 0.0
        else:
            outcome = _muse_unavailable_outcome(
                service, path,
                "Muse vision input is disabled "
                "(MUSE_VISION_ENABLED=false in the backend .env).")
            duration_ms = 0.0
        ok = bool(outcome.get("ok"))
        tika_block = _tika_normalization_block(record)
        number = len(record["attempts"]) + 1
        raw_text = outcome.get("raw_text") or ""
        attempt_timeout = outcome.get("timeout_seconds")
        record["attempts"].append({
            "attempt_id": f"{record['resume_id']}-attempt-{number}",
            "resume_id": record["resume_id"],
            "attempt_number": number,
            "extractor_name": "muse_ai",
            "extractor_version": None,
            "success": ok,
            "accepted": ok,
            "decision": "accept" if ok else "reject",
            "decision_reason": (
                "Muse AI returned schema-valid Resume JSON."
                if ok else (outcome.get("error")
                            or "Muse AI extraction failed.")),
            "quality_status": None,
            "quality": None,
            "quality_checker_report": None,
            "duration_ms": duration_ms,
            "text": raw_text,
            "extracted_text": raw_text,
            "extracted_text_length": len(raw_text),
            "extraction_status": "SUCCESS" if ok else "FAILED",
            "error": None if ok else outcome.get("error"),
            "error_message": None if ok else outcome.get("error"),
            "error_type": outcome.get("error_type")
            or (None if ok else "MuseError"),
            "error_traceback": None,
            "started_at": None, "completed_at": None,
            "selected": ok,
            "fallback_decision": "accept" if ok else "reject",
            "fallback_reason": (
                "Muse AI returned schema-valid Resume JSON."
                if ok else (outcome.get("error")
                            or "Muse AI extraction failed.")),
            "is_final_selected": ok,
            "ocr": None,
            "timeout_seconds": attempt_timeout,
            "model_config_id": None,
            "provider": outcome.get("provider") or "Muse AI",
            "model": outcome.get("model"),
            "json_validation": outcome.get("validation"),
            "input_type": outcome.get("input_type"),
            "metadata": {
                "input_type": outcome.get("input_type"),
                "visual_provided": outcome.get("visual_provided"),
                "tika_text_provided": outcome.get("tika_text_provided"),
                "tika_text_chars": outcome.get("tika_text_chars"),
                "hyperlinks_provided": outcome.get("hyperlinks_provided"),
                "hyperlink_count": outcome.get("hyperlink_count") or 0,
                "hyperlink_extraction_error":
                    record.get("hyperlink_error"),
                "request_id": outcome.get("request_id"),
                "usage_available": outcome.get("usage_available"),
                "tika_text_normalization": {
                    "status": tika_block.get("status"),
                    "original_chars": tika_block.get("original_chars"),
                    "normalized_chars":
                        tika_block.get("normalized_chars"),
                    "estimated_reduction_percent":
                        tika_block.get("estimated_reduction_percent"),
                    "normalization_applied":
                        tika_block.get("normalization_applied", False),
                    "normalization_steps":
                        tika_block.get("normalization_steps", []),
                    "error": tika_block.get("error")},
                "page_count": outcome.get("page_count"),
                "pages_processed": outcome.get("pages_processed"),
                "pages_skipped": outcome.get("pages_skipped"),
                "pages_beyond_image_limit":
                    outcome.get("pages_beyond_image_limit"),
                "usage": outcome.get("usage") or {},
            },
        })
        record["attempt_count"] = len(record["attempts"])
        extra_stages.append(_stage(
            f"Muse AI ({outcome.get('model') or service.model})",
            "extractor", "PASS" if ok else "FAIL", extractor="muse_ai",
            duration_ms=duration_ms,
            message=((_sources_message(outcome)
                      + " Schema-valid Resume JSON returned.")
                     if ok else (outcome.get("error")
                                 or "Muse AI extraction failed.")),
            accepted=ok, decision="accept" if ok else "reject",
            selected=ok))
        if ok:
            for attempt in record["attempts"][:-1]:
                attempt["is_final_selected"] = False
                attempt["selected"] = False
            record["resume_json"] = outcome.get("resume_json")
            record["resume_json_source"] = "muse_ai"
            record["resume_json_meta"] = {
                "provider": outcome.get("provider") or "Muse AI",
                "model": outcome.get("model"),
                "validation": outcome.get("validation"),
                "error": None,
                "input_type": outcome.get("input_type"),
                "duration_ms": duration_ms,
                "timeout_seconds": attempt_timeout,
                "usage": outcome.get("usage") or {},
                "pages_processed": outcome.get("pages_processed"),
                "pages_skipped": outcome.get("pages_skipped"),
            }
            record["extractor_used"] = "muse_ai"
            record["final_extractor"] = "muse_ai"
            # STRICT RULE — the three outputs stay separate:
            # extracted_text / final_extracted_text always hold the
            # non-Muse extractor chain's raw text (possibly empty);
            # Muse's response lives only in this stage's attempt
            # (above) and in resume_json. Muse output is never
            # written into the extracted-text fields and is never
            # passed to text normalization.
            record["success"] = True
            record["error"] = None
            record["selection_reason"] = (
                (record.get("selection_reason") or "")
                + " Muse AI produced validated Resume JSON from the "
                  "original file after the extractor chain did not "
                  "reach PASS.").strip()

    # --- Resume JSON generation from the accepted text -------------------
    if record.get("resume_json") is None and record.get("extracted_text") \
            and getattr(service, "configured", False):
        started = _time.perf_counter()
        sent_text, normalization_info = _normalize_for_muse(
            record, record["extracted_text"])
        outcome = service.generate_resume_json(
            sent_text, hyperlinks=hyperlinks)
        requests.append(_muse_request_entry(
            "resume_json", outcome, _request_status(outcome)))
        duration_ms = outcome.get("duration_ms")
        if duration_ms is None:
            duration_ms = round((_time.perf_counter() - started) * 1000, 2)
        ok = bool(outcome.get("ok"))
        record["resume_json"] = outcome.get("resume_json")
        record["resume_json_source"] = "muse_ai" if ok else None
        record["resume_json_meta"] = {
            "provider": outcome.get("provider") or "Muse AI",
            "model": outcome.get("model"),
            "validation": outcome.get("validation"),
            "error": None if ok else outcome.get("error"),
            "input_type": "text",
            "duration_ms": duration_ms,
            "timeout_seconds": outcome.get("timeout_seconds"),
            "usage": outcome.get("usage") or {},
            "text_normalization": normalization_info,
        }
        extra_stages.append(_stage(
            f"Muse AI — Resume JSON Generation "
            f"({outcome.get('model') or service.model})",
            "extractor", "PASS" if ok else "FAIL",
            duration_ms=duration_ms,
            message=("Structured Resume JSON generated by Muse AI "
                     "and validated against the schema."
                     if ok else (outcome.get("error")
                                 or "Resume JSON generation failed.")),
            accepted=ok, decision="accept" if ok else "reject",
            selected=False))
        extra_stages.append(_stage(
            "JSON Validation", "quality",
            "PASS" if outcome.get("validation") == "valid" else "FAIL",
            message=("Resume JSON matches the project schema."
                     if outcome.get("validation") == "valid"
                     else (outcome.get("error")
                           or "Resume JSON did not validate.")),
            accepted=outcome.get("validation") == "valid",
            selected=False))

    if requests:
        record["muse_requests"] = requests
        window = getattr(getattr(service, "config", None),
                         "context_window_tokens", None)
        record["token_usage"] = _aggregate_token_usage(requests, window)

    if extra_stages:
        trace = record.get("pipeline_trace") or []
        final_index = next(
            (i for i, s in enumerate(trace) if s.get("type") == "final"),
            len(trace))
        record["pipeline_trace"] = (trace[:final_index] + extra_stages
                                    + trace[final_index:])
        if record.get("extractor_used") == "muse_ai":
            final_stage = record["pipeline_trace"][-1]
            final_stage["status"] = "PASS"
            final_stage["extractor"] = "muse_ai"
            final_stage["accepted"] = True
            final_stage["selected"] = True
            final_stage["message"] = (
                "Muse AI produced the final Resume JSON.")


def _apply_direct_llm_routing(record: dict[str, Any], *,
                              fail_below: float = FAIL_BELOW,
                              ) -> dict[str, Any] | None:
    """Direct-LLM routing for detection-gap records (Approach A).

    Runs only when the extractor chain produced NO selected text
    (``record["success"]`` is False) even though an attempt extracted
    readable text — the record would otherwise dead-end before the
    LLM text stage. For the best such candidate (highest mean filter
    score among attempts whose Text Health is at or above the gate's
    ``fail_below`` — genuinely corrupt text is never promoted), the
    sections completeness reported missing at required/recommended
    importance are probed against the candidate's own normalized text
    (:mod:`api.content_probe`).

    If at least one missing section's content IS present, the miss
    was a heading-detection gap (typo'd/glued heading), so the
    candidate is promoted as the record's text source and the record
    is stamped ``direct_llm_routing`` for audit; ``_apply_muse_stages``
    then runs the text stage (and skips vision) as usual. Attempt
    decisions and quality reports are NOT rewritten — promotion is
    expressed through the selected flags, the record fields, the
    selection reason and a trace stage. When no missing section
    probes positive, the content is genuinely absent and the record
    is returned unchanged (returns None).
    """
    if record.get("success") or record.get("extracted_text"):
        return None
    from .content_probe import probe_sections

    best: tuple[float, int, dict[str, Any], list[str]] | None = None
    for index, attempt in enumerate(record.get("attempts") or []):
        if attempt.get("extractor_name") == "muse_ai":
            continue
        if not attempt.get("success"):
            continue
        text = attempt.get("extracted_text") or ""
        report = attempt.get("quality_checker_report") \
            or attempt.get("quality")
        if not text.strip() or not isinstance(report, dict):
            continue
        scores = []
        for name in ("text_health", "completeness", "reading_order"):
            value = (report.get(name) or {}).get("score")
            if isinstance(value, (int, float)):
                scores.append(float(value))
        text_health = (report.get("text_health") or {}).get("score")
        if not scores or not isinstance(text_health, (int, float)) \
                or float(text_health) < fail_below:
            continue
        completeness = report.get("completeness") or {}
        missing = [str(s) for s in
                   (completeness.get("missing_required") or [])
                   + (completeness.get("missing_recommended") or [])]
        if not missing:
            continue
        headline = sum(scores) / len(scores)
        if best is None or headline > best[0]:
            best = (headline, index, attempt, missing)
    if best is None:
        return None
    _headline, _index, attempt, missing = best

    probe_text = attempt.get("normalized_text") \
        or attempt.get("extracted_text") or ""
    probed = probe_sections(probe_text, missing)
    gaps = [s for s in missing if probed.get(s, {}).get("present")]
    if not gaps:
        return None

    name = attempt.get("extractor_name")
    routing: dict[str, Any] = {
        "routed": True,
        "reason": "detection_gap",
        "candidate_extractor": name,
        "candidate_quality_status": attempt.get("quality_status"),
        "trigger_sections": gaps,
        "probed_sections": probed,
    }
    record["direct_llm_routing"] = routing
    attempt["selected"] = True
    attempt["is_final_selected"] = True
    record["success"] = True
    record["extractor_used"] = name
    record["final_extractor"] = name
    record["extracted_text"] = attempt.get("extracted_text") or ""
    record["final_extracted_text"] = record["extracted_text"]
    record["quality"] = attempt.get("quality")
    record["quality_status"] = attempt.get("quality_status")
    record["final_quality_status"] = attempt.get("quality_status")
    record["error"] = None
    record["selection_reason"] = (
        (record.get("selection_reason") or "")
        + f" Direct-LLM routing: completeness missed"
          f" {', '.join(gaps)}, but content probes found that"
          f" content in the {extractor_display_name(name)} extraction"
          f" (a heading-detection gap), so the record was forwarded"
          f" directly to the LLM text stage.").strip()
    trace = record.get("pipeline_trace") or []
    final_index = next(
        (i for i, s in enumerate(trace) if s.get("type") == "final"),
        len(trace))
    record["pipeline_trace"] = (
        trace[:final_index]
        + [_stage("Direct LLM Routing — detection gap", "quality",
                  "WARNING", extractor=name,
                  message=(f"Completeness missed {', '.join(gaps)}, "
                           f"but content probes found the content in "
                           f"the {extractor_display_name(name)} text; "
                           f"forwarded directly to the LLM text "
                           f"stage."),
                  accepted=True, decision="accept", selected=False)]
        + trace[final_index:])
    return routing


def process_resume_file_simple(file_path: str | Path, *, resume_id: str,
                               original_filename: str,
                               muse_service: Any | None = None,
                               pipeline: Any | None = None) -> dict[str, Any]:
    """Run the SIMPLE pipeline (pipeline.ExtractionPipeline) for one
    upload and build a record compatible with existing records.

    This is the DEFAULT pipeline for new uploads (see
    :func:`process_resume_file`); the legacy chain runs only when
    explicitly requested. The record carries the full integration
    surface the frontend/API rely on: ``text_normalization`` (the
    selected extractor's original + normalized text — the normalized
    text is byte-for-byte what Muse received), ``resume_json`` +
    ``resume_json_source`` + ``resume_json_meta``, ``muse_requests``
    and ``token_usage`` from the actual Muse response.
    Never raises for bad documents -- failures are recorded.
    """
    path = Path(file_path)
    mime_type = detect_mime_type(path)
    file_size = path.stat().st_size if path.exists() else 0
    file_type = path.suffix.lower().lstrip(".") or None

    # Resolve pipeline instance
    pipe = pipeline
    if pipe is None:
        from pipeline import ExtractionPipeline as _EP
        if muse_service is not None:
            # muse_service may be a MuseService-like object or a callable
            if hasattr(muse_service, "generate_resume_json"):
                pipe = _EP(llm_service=muse_service)
            elif callable(muse_service):
                pipe = _EP(llm_callable=muse_service)
            else:
                pipe = _EP(llm_service=muse_service)
        else:
            pipe = _EP()

    # Need per-extractor texts for attempt records
    try:
        result = pipe.run(path, include_extractor_texts=True)
    except TypeError:
        # Custom pipeline objects in tests may not accept the flag
        result = pipe.run(path)  # type: ignore[attr-defined]
        # If texts were stripped, attempts will have empty texts -- acceptable fallback

    selected = result.get("selected_extractor")
    selected_score = result.get("selected_quality_score")
    selection_reason = result.get("selection_reason")
    reports: dict[str, Any] = result.get("extractor_reports") or {}
    extractor_results: list[dict[str, Any]] = result.get("extractor_results") or []
    # Sequential quality-gated flow facts: extractors that were never
    # invoked because an earlier one passed (NOT attempts), and the
    # gate's per-attempt verdicts that explain each decision.
    skipped_extractors: list[str] = list(result.get("skipped_extractors") or [])
    gate_decisions: dict[str, Any] = result.get("gate_decisions") or {}
    # Canonical order for attempts
    _canonical = list(result.get("extractor_order") or
                      ["tika", "ocr", "pymupdf", "docling"])
    by_name = {r.get("extractor"): r for r in extractor_results if isinstance(r, dict)}
    ordered_results: list[dict[str, Any]] = []
    for _n in _canonical:
        if _n in by_name:
            ordered_results.append(by_name[_n])
    for _r in extractor_results:
        if _r.get("extractor") not in _canonical:
            ordered_results.append(_r)

    # Config threshold for quality_status mapping
    try:
        from pipeline import load_pipeline_config as _load_cfg
        _cfg = _load_cfg()
        _threshold = float(getattr(pipe, "config", _cfg).min_quality_threshold) if getattr(pipe, "config", None) is not None else float(_cfg.min_quality_threshold)
    except Exception:
        _threshold = 60.0
    try:
        if getattr(pipe, "config", None) is not None:
            _threshold = float(pipe.config.min_quality_threshold)
    except Exception:
        pass

    if selected is None:
        quality_status = "FAIL"
    elif selected_score is not None and selected_score >= 75:
        quality_status = "PASS"
    elif selected_score is not None and selected_score >= _threshold:
        quality_status = "WARNING"
    else:
        quality_status = "FAIL"

    pipeline_status = result.get("status")
    # Extraction success and LLM success are tracked SEPARATELY:
    # ``success`` means an extractor was selected and its text stored
    # (the meaning the flag has on legacy records); the Muse outcome
    # lives in ``llm`` / ``llm_status`` / ``resume_json_meta`` and
    # never flips the extraction result.
    success = selected is not None

    attempts: list[dict[str, Any]] = []
    selected_qc_payload: dict[str, Any] | None = None
    selected_qc_status: str | None = None
    try:
        _checker: QualityChecker | None = QualityChecker()
    except Exception:
        logger.exception("Quality Checker unavailable for simple-pipeline records")
        _checker = None
    # The extraction-only safety limit in force for this run
    # (recorded on each attempt; never applies to Muse AI).
    timeout_seconds_val: float | None = None
    try:
        timeout_seconds_val = float(pipe.config.per_extractor_timeout_seconds) if getattr(pipe, "config", None) is not None and pipe.config.per_extractor_timeout_seconds is not None else None
    except Exception:
        timeout_seconds_val = None

    for index, res in enumerate(ordered_results, start=1):
        name = res.get("extractor")
        text_val = res.get("extracted_text") or res.get("text") or ""
        is_success = res.get("status") == "success"
        is_selected = bool(selected is not None and name == selected)
        exec_time = res.get("execution_time") or 0.0
        try:
            duration_ms = float(exec_time) * 1000.0
        except Exception:
            duration_ms = 0.0
        err_val = res.get("error")
        report = reports.get(name) or {}
        score_val = report.get("quality_score") if isinstance(report, dict) else None
        gate = gate_decisions.get(name) or {}
        gate_reason = gate.get("reason")
        if is_selected:
            decision = "accept"
            if gate.get("decision") == "continue" and gate_reason:
                decision_reason = (
                    gate_reason
                    + " Selected as the best candidate after all "
                      "executed extractors were tried.")
            else:
                decision_reason = gate_reason or selection_reason
        elif not is_success:
            decision = "reject"
            decision_reason = err_val or "Extraction failed."
        elif gate_reason:
            decision = "reject"
            decision_reason = gate_reason
        else:
            decision = "reject"
            decision_reason = (
                f"Not selected: quality score {score_val} (selected '{selected}' scored {selected_score})."
                if selected else (selection_reason or err_val or "Not selected."))
        err_type = res.get("error_type")
        if res.get("status") == "timeout" and not err_type:
            err_type = "TimeoutError"
        # Real Quality Checker report for this attempt's OWN text
        # (the same checker the legacy pipeline uses), so simple
        # records carry the same quality payload shape as legacy
        # records. Presentation/record parity only — SELECTION
        # already happened with the simple pipeline's own quality
        # scores and is never re-decided here.
        qc_payload: dict[str, Any] | None = None
        qc_status: str | None = None
        if is_success and text_val.strip() and _checker is not None:
            try:
                _report = _checker.check(
                    text_val, document_id=resume_id, source_tool=name,
                    attempt_id=f"{resume_id}-attempt-{index}")
                qc_status = quality_status_for(_report, True)
                qc_payload = _quality_payload(_report, qc_status)
            except Exception:
                logger.exception(
                    "Quality Checker failed for simple-pipeline attempt %s", name)
                qc_payload = None
                qc_status = None
        if is_selected:
            selected_qc_payload = qc_payload
            selected_qc_status = qc_status
        attempts.append({
            "attempt_id": f"{resume_id}-attempt-{index}",
            "resume_id": resume_id,
            "attempt_number": index,
            "extractor_name": name,
            "extractor_version": None,
            "success": is_success,
            "accepted": is_selected,
            "selected": is_selected,
            "is_final_selected": is_selected,
            "decision": decision,
            "decision_reason": decision_reason,
            "fallback_decision": decision,
            "fallback_reason": decision_reason,
            "quality_status": qc_status,
            "quality": qc_payload,
            "quality_checker_report": qc_payload,
            "quality_score": score_val,
            "duration_ms": duration_ms,
            "text": text_val,
            "extracted_text": text_val,
            "extracted_text_length": len(text_val),
            "extraction_status": "SUCCESS" if is_success else "FAILED",
            "error": None if is_success else err_val,
            "error_message": None if is_success else err_val,
            "error_type": err_type,
            "error_traceback": None,
            "started_at": None,
            "completed_at": None,
            "timeout_seconds": timeout_seconds_val,
            "metadata": res.get("metadata") or {},
            "ocr": _ocr_info(res.get("metadata")),
            "model_config_id": None,
            "provider": None,
            "model": None,
            "json_validation": None,
            "input_type": None,
        })

    # The record's headline quality status is the Quality Checker's
    # real status for the SELECTED extractor's text when the checker
    # ran; the score-derived mapping above remains the fallback.
    if selected_qc_status is not None:
        quality_status = selected_qc_status

    selected_result = by_name.get(selected) if selected else None
    page_count = selected_result.get("page_count") if selected_result else None
    original_text = result.get("original_text") or ""
    if original_text is None:
        original_text = ""
    total_ms: float | None = None
    timings = result.get("timings") or {}
    if timings.get("total_seconds") is not None:
        try:
            total_ms = float(timings["total_seconds"]) * 1000.0
        except Exception:
            total_ms = None

    # --- Selected normalized text (the EXACT text sent to Muse) -----
    # The pipeline result carries the selected extractor's original
    # text and its normalized form as separate top-level fields; the
    # record's text_normalization block (the shape the frontend and
    # legacy records use) is built from those exact strings and then
    # PINNED to them, so the Selected Normalized Text tab shows
    # byte-for-byte what Muse received — never a re-normalization
    # that could drift from the sent text.
    normalized_text = result.get("normalized_text")
    if normalized_text is None:
        normalized_text = original_text
    norm_block = result.get("normalization") or {}
    if selected is not None:
        from .text_normalization import normalization_block as _make_norm_block

        text_normalization = _make_norm_block(original_text, source=selected)
        text_normalization["normalized_text"] = normalized_text
        text_normalization["original_extracted_text"] = original_text
        text_normalization["normalization_applied"] = bool(
            norm_block.get("normalization_applied"))
        text_normalization["normalization_steps"] = list(
            norm_block.get("normalization_steps") or [])
        if norm_block.get("status") == "success":
            text_normalization["status"] = "applied"
        elif text_normalization.get("status") == "applied":
            text_normalization["status"] = "bypassed"
            text_normalization["error"] = norm_block.get("error")
    else:
        text_normalization = {
            "source": "none", "status": "unavailable", "error": None,
            "original_extracted_text": None, "normalized_text": None,
            "normalization_applied": False, "normalization_steps": [],
            "original_chars": 0, "normalized_chars": 0,
            "original_estimated_tokens": 0, "normalized_estimated_tokens": 0,
            "estimated_reduction_percent": 0.0,
        }
    normalization_out = dict(norm_block)
    normalization_out.setdefault("original_text", original_text)
    normalization_out.setdefault("normalized_text", normalized_text)

    # --- Contact Information Recovery: completeness re-check -------
    # The pipeline's recovery stage may have corrected the selected
    # normalized text (validated contact fields merged in before the
    # Muse call). Re-run the Quality Checker against the CORRECTED
    # text — the text actually sent to Muse — and let the selected
    # attempt and the record quality show that updated result.
    # Before/after completeness is preserved on the recovery block;
    # the attempts' original extracted texts, the other attempts'
    # reports and the selection itself are untouched.
    recovery_block = result.get("contact_recovery")
    if isinstance(recovery_block, dict) and recovery_block.get("applied"):
        _corrected = text_normalization.get("normalized_text") or ""
        _sel_attempt = next(
            (a for a in attempts if a.get("is_final_selected")), None)
        if _checker is not None and _sel_attempt is not None and _corrected:
            def _comp_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
                comp = (payload or {}).get("completeness") or {}
                metrics = comp.get("metrics") or {}
                return {
                    "score": comp.get("score"),
                    "contact_information_detected":
                        "Contact Information" in
                        (metrics.get("detected_sections") or []),
                    "missing_sections":
                        metrics.get("missing_sections") or [],
                }

            try:
                _report2 = _checker.check(
                    _corrected, document_id=resume_id,
                    source_tool=selected,
                    attempt_id=_sel_attempt.get("attempt_id"))
                _status2 = quality_status_for(_report2, True)
                _payload2 = _quality_payload(_report2, _status2)
                recovery_block["completeness_before"] = _comp_summary(
                    selected_qc_payload)
                recovery_block["completeness_after"] = _comp_summary(
                    _payload2)
                _sel_attempt["quality"] = _payload2
                _sel_attempt["quality_checker_report"] = _payload2
                _sel_attempt["quality_status"] = _status2
                selected_qc_payload = _payload2
                selected_qc_status = _status2
                quality_status = _status2
            except Exception:
                logger.exception(
                    "Completeness re-check after contact recovery "
                    "failed for %s", resume_id)

    # --- Muse AI outcome → persisted record fields --------------------
    # The LLM block already carries the real outcome of the single
    # Muse call (provider, model, validation, duration, usage,
    # error). Map it onto the record fields the frontend/API have
    # always used — resume_json_meta, muse_requests, token_usage —
    # instead of dropping it (the previous build stored None/[] here,
    # which is why the AI Output tab and token panel were empty even
    # when Muse had run).
    llm_block = result.get("llm") or {}
    llm_status_str = llm_block.get("status")
    resume_json = result.get("resume_data")
    if resume_json is None and llm_status_str == "success":
        resume_json = llm_block.get("resume_json")
    if llm_status_str != "success":
        resume_json = None
    normalization_info = {
        "source": selected, "status": text_normalization.get("status"),
        "original_chars": text_normalization.get("original_chars"),
        "normalized_chars": text_normalization.get("normalized_chars"),
        "estimated_reduction_percent":
            text_normalization.get("estimated_reduction_percent"),
        "normalization_applied":
            text_normalization.get("normalization_applied", False),
        "normalization_steps":
            text_normalization.get("normalization_steps", []),
        "error": text_normalization.get("error"),
    }
    resume_json_meta: dict[str, Any] = {
        "provider": llm_block.get("provider") or "Muse AI",
        "model": llm_block.get("model"),
        "validation": llm_block.get("validation"),
        "error": llm_block.get("error"),
        "error_type": llm_block.get("error_type"),
        "status": llm_status_str,
        "input_type": "text",
        "duration_ms": llm_block.get("duration_ms"),
        "usage": llm_block.get("usage") or {},
        "attempts": llm_block.get("attempts"),
        "text_normalization": normalization_info,
    }
    muse_requests: list[dict[str, Any]] = []
    if llm_block.get("attempts"):
        _req_status = (
            "success" if llm_status_str == "success"
            else "timeout" if llm_block.get("error_type") == "TimeoutError"
            else "failed")
        muse_requests.append(_muse_request_entry("resume_json", {
            "usage": llm_block.get("usage") or {},
            "model": llm_block.get("model"),
            "request_id": None,
            "duration_ms": llm_block.get("duration_ms"),
        }, _req_status))
    token_usage = None
    if muse_requests:
        _window = None
        try:
            from muse_ai import load_muse_config as _load_muse_cfg

            _window = _load_muse_cfg().context_window_tokens
        except Exception:
            _window = None
        token_usage = _aggregate_token_usage(muse_requests, _window)

    # Pipeline trace -- honest stages for the required flow:
    # Upload → MIME → extractors → Quality Evaluation → Best
    # Extractor Selection → Text Normalization → Muse AI — Resume
    # JSON Generation → JSON Validation → Final Extraction Result.
    trace: list[dict[str, Any]] = [
        _stage("Resume Upload", "upload", "PASS", message="File received and stored for processing."),
        _stage("MIME Type Detection", "mime", "PASS", message=mime_type or REASON_UNAVAILABLE),
    ]
    _attempts_by_name = {a["extractor_name"]: a for a in attempts}
    _stage_names: list[str] = []
    for _nm in _canonical:
        if _nm in _attempts_by_name or _nm in skipped_extractors:
            _stage_names.append(_nm)
    for _nm in _attempts_by_name:
        if _nm not in _stage_names:
            _stage_names.append(_nm)
    for _nm in skipped_extractors:
        if _nm not in _stage_names:
            _stage_names.append(_nm)
    for nm in _stage_names:
        disp = extractor_display_name(nm)
        att = _attempts_by_name.get(nm)
        if att is None:
            # Never invoked: show the gate's recorded reason when
            # one exists (early PASS, convergence, OCR necessity),
            # else the generic early-acceptance message.
            _gate_reason = (gate_decisions.get(nm) or {}).get("reason")
            trace.append(_stage(
                f"{disp} Extractor", "extractor", "SKIPPED",
                extractor=nm,
                message=(_gate_reason
                         or (f"Skipped — {extractor_display_name(selected)} passed "
                             "quality checks and was accepted before this "
                             "extractor was needed."
                             if selected else "Skipped — not executed.")),
                accepted=False, decision="skipped", selected=False))
            continue
        trace.append(_stage(f"{disp} Extractor", "extractor", "PASS" if att["success"] else "FAIL",
                            extractor=nm, duration_ms=att.get("duration_ms"),
                            message=("Extraction succeeded." if att["success"] else (att.get("error") or REASON_UNAVAILABLE)),
                            accepted=att.get("accepted"), decision=att.get("decision"), selected=att.get("selected")))
    trace.append(_stage("Quality Evaluation", "quality", "PASS" if selected else "FAIL",
                        message=(f"Quality scores: " + ", ".join(f"{k}={ (v.get('quality_score') if isinstance(v, dict) else v)}" for k, v in reports.items()) if reports else (selection_reason or REASON_UNAVAILABLE)),
                        selected=bool(selected)))
    trace.append(_stage("Best Extractor Selection", "selection", "PASS" if selected else "FAIL",
                        extractor=selected, message=selection_reason or REASON_UNAVAILABLE, selected=bool(selected)))
    if selected is None:
        _norm_stage = ("SKIPPED", "Skipped — no extractor was selected.")
    elif norm_block.get("status") == "success":
        _norm_stage = ("PASS", (
            f"Selected {extractor_display_name(selected)} text normalized: "
            f"{text_normalization.get('original_chars')} → "
            f"{text_normalization.get('normalized_chars')} characters."))
    else:
        _norm_stage = ("FAIL", norm_block.get("error") or "Text normalization failed.")
    trace.append(_stage("Text Normalization", "normalization", _norm_stage[0],
                        message=_norm_stage[1], selected=bool(selected)))
    if isinstance(recovery_block, dict):
        _rec = recovery_block.get("recovered") or {}
        _rec_fields = ", ".join(
            f"{f} from {_rec[f]['source']}"
            for f in ("phone", "email", "location") if f in _rec)
        _rec_status = recovery_block.get("status")
        if _rec_status == "not_needed":
            _rec_stage = ("PASS", "Phone, email and location already "
                                  "present in the selected text — no "
                                  "recovery needed.")
        elif _rec_status == "recovered":
            _rec_stage = ("PASS", f"Recovered {_rec_fields} and merged "
                                  "them into the selected normalized "
                                  "text sent to Muse AI.")
        elif _rec_status == "partial":
            _rec_stage = ("WARNING",
                          f"Recovered {_rec_fields}; still missing: "
                          + ", ".join(recovery_block.get("still_missing")
                                      or []) + ".")
        elif _rec_status == "error":
            _rec_stage = ("FAIL", "Contact recovery error: "
                                  + str(recovery_block.get("error")))
        else:
            _rec_stage = ("FAIL", "Contact information missing and "
                                  "could not be recovered from any "
                                  "source (extractor outputs, "
                                  "first-page text layer, OCR). Still "
                                  "missing: "
                                  + ", ".join(recovery_block.get(
                                      "still_missing") or []) + ".")
        trace.append(_stage("Contact Information Recovery",
                            "contact_recovery", _rec_stage[0],
                            message=_rec_stage[1],
                            selected=bool(selected)))
    if llm_status_str == "success":
        _muse_stage = ("PASS", "Structured Resume JSON generated by Muse AI "
                               "from the selected normalized text.")
    elif llm_status_str == "not_configured":
        _muse_stage = ("FAIL", "NOT CONFIGURED — "
                       + (llm_block.get("error")
                          or "Muse AI is not configured; no LLM call was made."))
    elif llm_status_str == "skipped":
        _muse_stage = ("SKIPPED", "Skipped — no extractor was selected, "
                                  "so no text was sent to Muse AI.")
    else:
        _muse_stage = ("FAIL", llm_block.get("error")
                       or "Muse AI Resume JSON generation failed.")
    trace.append(_stage(
        f"Muse AI — Resume JSON Generation ({llm_block.get('model') or 'Muse AI'})",
        "llm", _muse_stage[0], duration_ms=llm_block.get("duration_ms"),
        message=_muse_stage[1], accepted=llm_status_str == "success",
        selected=False))
    validation_val = llm_block.get("validation")
    if validation_val == "valid":
        _val_stage = ("PASS", "Resume JSON matches the project schema.")
    elif llm_status_str in ("skipped", "not_configured"):
        _val_stage = ("SKIPPED", "Skipped — Muse AI produced no JSON to validate.")
    else:
        _val_stage = ("FAIL", llm_block.get("error")
                      or f"Validation: {validation_val}")
    trace.append(_stage("JSON Validation", "quality", _val_stage[0],
                        message=_val_stage[1],
                        accepted=validation_val == "valid", selected=False))
    _full_success = bool(selected is not None and resume_json is not None)
    if _full_success:
        _final_msg = (f"Muse AI produced validated Resume JSON from the "
                      f"{extractor_display_name(selected)} extraction.")
    elif selected is not None:
        _final_msg = (
            f"Extraction selected {extractor_display_name(selected)} and "
            "its normalized text is stored, but the Muse AI stage did "
            "not produce Resume JSON: "
            + (llm_block.get("error") or f"LLM status: {llm_status_str}"))
    else:
        _final_msg = result.get("error") or REASON_UNAVAILABLE
    trace.append(_stage("Final Extraction Result", "final",
                        "PASS" if _full_success else "FAIL",
                        extractor=selected, duration_ms=total_ms,
                        message=_final_msg,
                        accepted=_full_success, selected=bool(selected)))

    record: dict[str, Any] = {
        "resume_id": resume_id,
        "filename": original_filename,
        "file_type": file_type,
        "mime_type": mime_type,
        "file_size_bytes": file_size,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "success": success,
        "extractor_used": selected,
        "final_extractor": selected,
        "primary_extractor": "tika",
        "attempts": attempts,
        "attempt_count": len(attempts),
        "fallback_occurred": len(attempts) > 1,
        "fallback_reason": (
            next((a.get("error") or a.get("decision_reason")
                  for a in attempts if not a.get("is_final_selected")),
                 None)
            if len(attempts) > 1 else None),
        "skipped_extractors": skipped_extractors,
        "gate_decisions": gate_decisions,
        "extracted_text": original_text,
        "final_extracted_text": original_text,
        "page_count": page_count,
        "warnings": [],
        "error": None if selected is not None else result.get("error"),
        "quality_status": quality_status,
        "final_quality_status": quality_status,
        "quality": selected_qc_payload,
        "selection_reason": selection_reason,
        "pipeline_trace": trace,
        "duration_ms": total_ms or 0.0,
        "extractor_order": list(_canonical),
        "extraction_timeout_seconds": timeout_seconds_val,
        "hyperlinks": [],
        "hyperlink_error": None,
        "text_normalization": text_normalization,
        "contact_recovery": recovery_block
        if isinstance(recovery_block, dict) else None,
        "muse_requests": muse_requests,
        "token_usage": token_usage,
        "resume_json_meta": resume_json_meta,
        # Simple-pipeline fields (additive, backward-compatible)
        "pipeline_mode": "simple",
        "selected_extractor": selected,
        "selected_quality_score": selected_score,
        "extractor_reports": reports,
        "normalization": normalization_out,
        "llm": llm_block,
        "llm_status": llm_status_str,
        "resume_data": resume_json,
        "resume_json": resume_json,
        "resume_json_source": "muse_ai" if resume_json is not None else None,
        "timings": timings,
    }
    _ensure_attempt_normalized_texts(record)
    return record


def _upsert_ai_trace_stages(record: dict[str, Any],
                            outcome: dict[str, Any],
                            duration_ms: float | None, ok: bool) -> None:
    """Refresh the record's Muse AI / JSON Validation trace stages
    after an on-demand generation: any previous stages for that step
    are replaced by the latest real outcome, inserted before the
    final stage, and the final stage is upgraded to PASS when the
    record now holds validated Resume JSON."""
    trace = [s for s in (record.get("pipeline_trace") or [])
             if not (str(s.get("stage", "")).startswith(
                 "Muse AI — Resume JSON Generation")
                     or s.get("stage") == "JSON Validation")]
    validation = outcome.get("validation")
    stages = [
        _stage(
            f"Muse AI — Resume JSON Generation "
            f"({outcome.get('model') or 'Muse AI'})",
            "llm", "PASS" if ok else "FAIL", duration_ms=duration_ms,
            message=("Structured Resume JSON generated by Muse AI "
                     "from the selected normalized text "
                     "(on-demand generation)."
                     if ok else (outcome.get("error")
                                 or "Muse AI Resume JSON generation "
                                    "failed.")),
            accepted=ok, selected=False),
        _stage(
            "JSON Validation", "quality",
            "PASS" if validation == "valid" else "FAIL",
            message=("Resume JSON matches the project schema."
                     if validation == "valid"
                     else (outcome.get("error")
                           or f"Validation result: {validation}")),
            accepted=validation == "valid", selected=False),
    ]
    final_index = next(
        (i for i, s in enumerate(trace) if s.get("type") == "final"),
        len(trace))
    record["pipeline_trace"] = (trace[:final_index] + stages
                                + trace[final_index:])
    if record.get("resume_json"):
        final_stage = record["pipeline_trace"][-1]
        final_stage["status"] = "PASS"
        final_stage["accepted"] = True
        final_stage["selected"] = True
        final_stage["extractor"] = record.get("extractor_used")
        final_stage["message"] = (
            "Muse AI produced validated Resume JSON from the "
            "selected normalized text.")


# --- On-demand AI generation guard --------------------------------------
# Only ONE active AI generation per resume: a second request while
# one is running is refused (the endpoint maps it to HTTP 409)
# instead of firing a duplicate Muse call. The registry is
# in-process state only — it never touches the stored record.
_AI_GENERATION_LOCK = threading.Lock()
_AI_GENERATION_RUNNING: set[str] = set()


def ai_generation_in_progress(resume_id: str | None) -> bool:
    """True while an on-demand AI generation is running for a resume."""
    if not resume_id:
        return False
    with _AI_GENERATION_LOCK:
        return resume_id in _AI_GENERATION_RUNNING


def _try_acquire_ai_generation(resume_id: str) -> bool:
    with _AI_GENERATION_LOCK:
        if resume_id in _AI_GENERATION_RUNNING:
            return False
        _AI_GENERATION_RUNNING.add(resume_id)
        return True


def _release_ai_generation(resume_id: str) -> None:
    with _AI_GENERATION_LOCK:
        _AI_GENERATION_RUNNING.discard(resume_id)


def generate_ai_output_for_record(
        record: dict[str, Any],
        service: Any | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run ONLY the Muse AI Resume-JSON stage for an existing record.

    On-demand generation for the AI Output tab's button: the text
    sent to Muse is EXACTLY the record's stored Selected Normalized
    Text (the ``text_normalization`` block's normalized text, or the
    selected original text when normalization was bypassed) — no
    extractor is re-run, no other extractor's text is sent, and
    exactly one Muse call is made. The outcome is recorded on the
    record (``ai_last_attempt`` always; ``resume_json`` /
    ``resume_json_meta`` / ``llm`` / ``muse_requests`` /
    ``token_usage`` / trace stages on a real API outcome).

    A failed regeneration never destroys an existing stored
    ``resume_json`` — the failure is reported through
    ``ai_last_attempt`` instead. Returns ``(record, attempt)`` where
    attempt["status"] is one of "success", "failed",
    "not_configured", "no_text" or "already_running" (a generation
    for this resume is already in progress; no call was made).
    There is NO timeout on the Muse call: however long the API
    takes, the generation waits for it.
    """
    _ensure_text_normalization(record)
    block = record.get("text_normalization") or {}
    sent_text = None
    if block.get("status") == "applied":
        sent_text = block.get("normalized_text")
    if not sent_text:
        sent_text = block.get("normalized_text") \
            or block.get("original_extracted_text")
    if not sent_text or not str(sent_text).strip():
        return record, {
            "status": "no_text", "ok": False,
            "error": "No selected normalized text is stored for "
                     "this resume, so there is nothing to send to "
                     "Muse AI. Reprocess the resume first."}
    resume_id = record.get("resume_id")
    if resume_id is not None \
            and not _try_acquire_ai_generation(resume_id):
        return record, {
            "status": "already_running", "ok": False,
            "error": "AI generation is already running for this "
                     "resume. Wait for it to finish — a slow Muse "
                     "response is still running, not failed."}
    try:
        return _generate_ai_output_acquired(
            record, service, block, sent_text)
    finally:
        if resume_id is not None:
            _release_ai_generation(resume_id)


def _generate_ai_output_acquired(
        record: dict[str, Any], service: Any | None,
        block: dict[str, Any], sent_text: str,
        ) -> tuple[dict[str, Any], dict[str, Any]]:
    """The on-demand Muse call itself (guard already held)."""
    import time as _time

    svc = service if service is not None else _default_muse_service()
    attempted_at = datetime.now(timezone.utc).isoformat()
    if svc is None or not getattr(svc, "configured", False):
        error = None
        if svc is not None:
            error = getattr(svc, "configuration_error", None)
        attempt = {
            "at": attempted_at, "status": "not_configured",
            "ok": False, "provider": "Muse AI",
            "model": getattr(svc, "model", None),
            "validation": None,
            "error": error or ("Muse AI is not configured — set "
                               "MUSE_API_KEY in the backend .env "
                               "file."),
            "error_type": "ConfigurationError", "duration_ms": None}
        record["ai_last_attempt"] = attempt
        if not record.get("resume_json"):
            record["llm_status"] = "not_configured"
        return record, attempt
    hyperlinks = record.get("hyperlinks") or []
    started = _time.perf_counter()
    outcome = svc.generate_resume_json(sent_text, hyperlinks=hyperlinks)
    duration_ms = outcome.get("duration_ms")
    if duration_ms is None:
        duration_ms = round((_time.perf_counter() - started) * 1000, 2)
    ok = bool(outcome.get("ok")) \
        and outcome.get("resume_json") is not None
    requests = list(record.get("muse_requests") or [])
    requests.append(_muse_request_entry(
        "resume_json", outcome, _request_status(outcome)))
    record["muse_requests"] = requests
    window = getattr(getattr(svc, "config", None),
                     "context_window_tokens", None)
    record["token_usage"] = _aggregate_token_usage(requests, window)
    attempt = {
        "at": attempted_at, "status": "success" if ok else "failed",
        "ok": ok, "provider": outcome.get("provider") or "Muse AI",
        "model": outcome.get("model"),
        "validation": outcome.get("validation"),
        "error": None if ok else outcome.get("error"),
        "error_type": None if ok else outcome.get("error_type"),
        "duration_ms": duration_ms}
    record["ai_last_attempt"] = attempt
    if ok:
        record["resume_json"] = outcome.get("resume_json")
        record["resume_data"] = outcome.get("resume_json")
        record["resume_json_source"] = "muse_ai"
        record["resume_json_meta"] = {
            "provider": outcome.get("provider") or "Muse AI",
            "model": outcome.get("model"),
            "validation": outcome.get("validation"),
            "error": None,
            "input_type": "text",
            "duration_ms": duration_ms,
            "timeout_seconds": outcome.get("timeout_seconds"),
            "usage": outcome.get("usage") or {},
            "text_normalization": _normalization_info(block),
        }
        record["llm"] = {
            "status": "success",
            "provider": outcome.get("provider") or "Muse AI",
            "model": outcome.get("model"),
            "resume_json": outcome.get("resume_json"),
            "validation": outcome.get("validation") or "valid",
            "error": None, "error_type": None, "attempts": 1,
            "duration_ms": duration_ms,
            "usage": outcome.get("usage") or {},
        }
        record["llm_status"] = "success"
        record["success"] = True
        record["error"] = None
    elif not record.get("resume_json"):
        record["llm_status"] = "failed"
    _upsert_ai_trace_stages(record, outcome, duration_ms, ok)
    return record, attempt


def process_resume_file(file_path: str | Path, *, resume_id: str,
                        original_filename: str,
                        muse_service: Any | None = None,
                        ai_service: Any | None = None,
                        pipeline_mode: str | None = None) -> dict[str, Any]:
    """Run the full pipeline for one stored upload and build its record.

    The DEFAULT pipeline is the simple production pipeline: MIME
    detection → Tika + PyMuPDF + Docling concurrently → quality
    evaluation → best-extractor selection → normalization of the
    SELECTED extractor's text only → one Muse AI call
    (``MuseService.generate_resume_json``) → schema validation →
    persisted record. Never raises for bad documents — failures are
    recorded.

    The legacy sequential chain (ExtractionManager + QualityChecker
    gate + vision fallback) remains available ONLY when explicitly
    requested with ``pipeline_mode="legacy"``. The
    ``SATVIQ_PIPELINE_MODE`` environment variable ("simple" or
    "legacy") chooses the mode only when the caller passes none.
    """
    _mode = pipeline_mode or os.environ.get("SATVIQ_PIPELINE_MODE") or "simple"
    if _mode not in ("simple", "legacy"):
        _mode = "simple"
    if _mode == "simple":
        _svc = muse_service if muse_service is not None else ai_service
        return process_resume_file_simple(
            file_path, resume_id=resume_id, original_filename=original_filename,
            muse_service=_svc)
    path = Path(file_path)
    mime_type = detect_mime_type(path)
    file_size = path.stat().st_size if path.exists() else 0
    logger.info("Processing resume_id=%s file=%s mime=%s size=%d",
                resume_id, original_filename, mime_type, file_size)

    checker = QualityChecker()
    gate_config: dict[str, Any] = {}
    try:
        from extractor.manager import load_manager_config

        gate_config = load_manager_config().get("quality_gate", {}) or {}
    except Exception:
        logger.exception("Could not read quality_gate config; using defaults")
    policy = QualityGatePolicy(checker, document_id=resume_id, config=gate_config)
    manager = ExtractionManager(acceptance_policy=policy)
    result = manager.extract(path)

    order = list(dict.fromkeys(manager.order))
    record = build_record_from_result(
        resume_id=resume_id, original_filename=original_filename,
        path=path, mime_type=mime_type, file_size=file_size,
        result=result, policy=policy, order=order,
    )
    if not record["success"]:
        # Detection-gap check BEFORE the Muse stages: a record the
        # chain left unselected may still hold readable text whose
        # only fault is a heading the completeness filter could not
        # see (typo/glued). If the content probes find the missing
        # sections' content, the candidate is promoted so the LLM
        # text stage receives it; otherwise the record stands.
        try:
            _fail_below = float(gate_config.get("fail_below")
                                or FAIL_BELOW)
        except (TypeError, ValueError):
            _fail_below = FAIL_BELOW
        _apply_direct_llm_routing(record, fail_below=_fail_below)
    service = muse_service if muse_service is not None else \
        (ai_service if ai_service is not None else _default_muse_service())
    if service is not None:
        _apply_muse_stages(record, path, service, None)
    if (record.get("direct_llm_routing") or {}).get("routed") \
            and record.get("resume_json"):
        # The routed record reached the LLM after all — the trace's
        # final stage (built when the chain had failed) must say so.
        trace = record.get("pipeline_trace") or []
        if trace:
            final_stage = trace[-1]
            final_stage["status"] = "PASS"
            final_stage["accepted"] = True
            final_stage["selected"] = True
            final_stage["extractor"] = record.get("extractor_used")
            final_stage["message"] = (
                "Muse AI produced the final Resume JSON from the "
                f"{extractor_display_name(record.get('extractor_used'))}"
                " extraction (direct-LLM routing).")
    logger.info("Processed resume_id=%s success=%s extractor=%s quality=%s",
                resume_id, result.success, record["extractor_used"],
                record["quality_status"])
    return record


# --- Manual extractor comparison -------------------------------------------
# On-demand, independent runs of the three comparison extractors on a
# resume's STORED ORIGINAL file, for the Inspector's manual-comparison
# UI. These runs are deliberately separate from the pipeline:
# - results live in record["extractor_results"][<extractor>] and never
#   touch record["attempts"], the selected/final text, extractor_used,
#   or resume_json;
# - no Muse / LLM call of any kind is made here;
# - the pipeline's own selection is never changed by a manual run.

#: Extractors offered for manual comparison.
MANUAL_EXTRACTORS: tuple[str, ...] = ("tika", "pymupdf", "docling")

_MANUAL_EXTRACTOR_CLASSES: dict[str, type] = {
    "tika": TikaExtractor,
    "pymupdf": PyMuPDFExtractor,
    "docling": DoclingExtractor,
}


def _build_manual_extractors(names: list[str]) -> dict[str, Any]:
    """Instantiate the requested comparison extractors with their
    configured sections from extractor.yaml (same config the pipeline
    uses). Test seam: run_manual_extractors accepts injected instances
    instead."""
    try:
        from extractor.manager import load_manager_config

        per_extractor = load_manager_config().get("extractors", {}) or {}
    except Exception:
        logger.exception("Could not read extractor config for manual runs")
        per_extractor = {}
    built: dict[str, Any] = {}
    for name in names:
        cls = _MANUAL_EXTRACTOR_CLASSES.get(name)
        if cls is not None:
            built[name] = cls(config=per_extractor.get(name, {}))
    return built


def run_manual_extractors(record: dict[str, Any], file_path: str | Path,
                          extractor_names: list[str], *,
                          extractors: dict[str, Any] | None = None,
                          checker: QualityChecker | None = None,
                          ) -> dict[str, Any]:
    """Run ``extractor_names`` independently on ``file_path`` and store
    each result under ``record["extractor_results"][name]`` (in place).

    Each entry: status, extracted_text, normalized_text (the existing
    deterministic normalizer on that extractor's own text), quality
    score/status/report from the existing QualityChecker when text was
    produced, character/word counts, execution time, page count, and
    the real error when the run failed. A failing extractor never
    stops the others. Returns the updated ``extractor_results`` dict.
    """
    from .text_normalization import normalize_text

    path = Path(file_path)
    built = extractors if extractors is not None else \
        _build_manual_extractors(list(extractor_names))
    qc = checker if checker is not None else QualityChecker()
    results: dict[str, Any] = dict(record.get("extractor_results") or {})
    for name in extractor_names:
        extractor = built.get(name)
        if extractor is None:
            results[name] = {
                "extractor": name, "source": "manual", "status": "failed",
                "extracted_text": "", "normalized_text": "",
                "quality_score": None, "quality_status": None,
                "quality": None, "char_count": 0, "word_count": 0,
                "execution_time": 0.0, "duration_ms": 0.0,
                "page_count": None,
                "error": f"Extractor '{name}' is not available.",
                "error_type": "ExtractorUnavailableError",
                "run_at": datetime.now(timezone.utc).isoformat(),
            }
            continue
        try:
            result = extractor.extract(path)
        except Exception as exc:  # an extractor must never crash the batch
            logger.exception("Manual extractor '%s' raised", name)
            result = ExtractionResult(
                extractor_name=name, success=False, text="",
                error=f"{type(exc).__name__}: {exc}",
                error_type=type(exc).__name__)
        text = result.text or ""
        quality_score: float | None = None
        quality_status: str | None = None
        quality_payload: dict[str, Any] | None = None
        if result.success and text.strip():
            report = qc.check(
                text,
                document_id=record.get("resume_id") or "manual",
                source_tool=name,
                attempt_id=f"manual-{name}",
            )
            quality_status = quality_status_for(report, True)
            quality_payload = _quality_payload(report, quality_status)
            scores = _filter_scores(report)
            quality_score = round(sum(scores) / len(scores), 1)
        results[name] = {
            "extractor": name, "source": "manual",
            "status": "success" if result.success else "failed",
            "extracted_text": text,
            "normalized_text": normalize_text(text) if text else "",
            "quality_score": quality_score,
            "quality_status": quality_status,
            "quality": quality_payload,
            "char_count": len(text), "word_count": len(text.split()),
            "execution_time": round((result.duration_ms or 0.0) / 1000, 3),
            "duration_ms": result.duration_ms or 0.0,
            "page_count": result.page_count,
            "error": None if result.success else result.error,
            "error_type": getattr(result, "error_type", None),
            "run_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("Manual extraction: resume=%s extractor=%s status=%s "
                    "chars=%d", record.get("resume_id"), name,
                    results[name]["status"], len(text))
    record["extractor_results"] = results
    return results


def failed_record(*, resume_id: str, original_filename: str, file_type: str | None,
                  mime_type: str | None, file_size: int, error: str) -> dict[str, Any]:
    """Record for uploads rejected before extraction (empty, too large,
    unsupported type) — still shown in history/statistics, as failures."""
    order = _configured_order()
    trace = [
        _stage("Resume Upload", "upload", "FAIL", message=error),
        _stage("MIME Type Detection", "mime", "SKIPPED",
               message="Not run — the upload was rejected before processing."),
    ]
    for name in order:
        trace.append(_stage(f"{extractor_display_name(name)} Extractor",
                            "extractor", "SKIPPED", extractor=name,
                            message="Not attempted."))
    trace.append(_stage("Final Extraction Result", "final", "FAIL",
                        message=error, accepted=False))
    return {
        "resume_id": resume_id,
        "filename": original_filename,
        "file_type": file_type,
        "mime_type": mime_type,
        "file_size_bytes": file_size,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "success": False,
        "extractor_used": None,
        "duration_ms": 0.0,
        "attempts": [],
        "extracted_text": "",
        "page_count": None,
        "warnings": [],
        "error": error,
        "quality_status": "FAIL",
        "final_quality_status": "FAIL",
        "final_extracted_text": "",
        "quality": None,
        "selection_reason": (
            "No candidate was selected — the upload was rejected before "
            "extraction ran."),
        "extractor_order": order,
        "primary_extractor": order[0] if order else None,
        "attempt_count": 0,
        "fallback_occurred": False,
        "fallback_reason": None,
        "pipeline_trace": trace,
    }
