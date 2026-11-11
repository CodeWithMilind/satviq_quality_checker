"""Simple production extraction pipeline.

DOCUMENT -> Sequential Quality-Gated Extraction
(Tika -> Quality Check -> STOP on PASS; otherwise PyMuPDF ->
Quality Check -> STOP on PASS; otherwise Docling -> Quality Check;
otherwise OCR) -> Best Extractor Selection (only when no extractor
passed outright) -> Text Normalization -> One LLM Stage
-> Validated Resume JSON.

The quality gate is the project's existing QualityChecker with the
configured ``quality_gate:`` thresholds from
``extractor/config/extractor.yaml`` — the same checker, thresholds
and PASS / WARNING / FAIL semantics the legacy chain's
QualityGatePolicy uses:

- **PASS** — accept immediately; every later extractor is SKIPPED
  (never invoked, never counted as an attempt).
- **WARNING** — held as a candidate; the next extractor is tried
  (unless the warning's lowest filter score is at/above
  ``warning_continue_below``, in which case it is accepted at once).
- **FAIL** — rejected; the next extractor is tried. FAIL requires
  Text Health or Reading Order below ``fail_below``; Completeness
  alone can never cause FAIL.

If no extractor passes outright, the existing selector picks the
best-scoring executed candidate (``min_quality_threshold`` still
applies); if none qualifies, the resume is failed/rejected exactly
as before.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .config import PipelineConfig, load_pipeline_config
from .contact_recovery import recover_contact_information
from .extractors import (default_sequential_extractors,
                         ordered_extractor_names, run_one_extractor)
from .llm import LLMProcessor
from .normalization import normalize_extracted_text
from .quality import evaluate_extraction_quality
from .selector import select_best_extractor

logger = logging.getLogger(__name__)

# Quality-gate thresholds — defaults identical to QualityGatePolicy;
# the configured values are read from the SAME ``quality_gate:``
# section of extractor/config/extractor.yaml the legacy chain uses.
_GATE_DEFAULTS = {"fail_below": 50.0, "warn_below": 75.0,
                  "warning_continue_below": 75.0}


def _load_gate_thresholds() -> dict[str, float]:
    thresholds = dict(_GATE_DEFAULTS)
    try:
        from extractor.manager import load_manager_config
        gate = (load_manager_config() or {}).get("quality_gate") or {}
        for key in thresholds:
            if gate.get(key) is not None:
                thresholds[key] = float(gate[key])
        if gate.get("warning_continue_below") is None \
                and gate.get("warn_below") is not None:
            thresholds["warning_continue_below"] = thresholds["warn_below"]
    except Exception:
        logger.warning(
            "Could not read quality_gate thresholds; using defaults",
            exc_info=True)
    return thresholds


def _gate_status(report: Any, fail_below: float, warn_below: float) -> str:
    """PASS / WARNING / FAIL for a QualityChecker report — the same
    mapping as api.pipeline.quality_status_for / QualityGatePolicy:
    FAIL only when Text Health or Reading Order is below fail_below;
    WARNING when any filter is below warn_below; PASS otherwise."""
    if report.text_health.score < fail_below \
            or report.reading_order.score < fail_below:
        return "FAIL"
    if min(report.text_health.score, report.completeness.score,
           report.reading_order.score) < warn_below:
        return "WARNING"
    return "PASS"


def _default_llm_service() -> Any | None:
    try:
        from muse_ai import MuseService, load_muse_config
        return MuseService(load_muse_config())
    except Exception:
        logger.exception("Muse AI service unavailable for simple pipeline; LLM stage will report not_configured")
        return None


class ExtractionPipeline:
    """The simple pipeline — the DEFAULT for new uploads via
    api.pipeline.process_resume_file. The legacy ExtractionManager
    chain is untouched and remains available via
    api.pipeline.process_resume_file with pipeline_mode="legacy".
    """

    def __init__(self, config: PipelineConfig | None = None, extractors: dict[str, Any] | None = None, llm_service: Any = None, llm_callable: Any = None, use_default_llm: bool = True):
        self.config = config or load_pipeline_config()
        self.extractors = extractors
        self.llm_callable = llm_callable
        self._use_default_llm = use_default_llm
        if llm_service is not None:
            self.llm_service = llm_service
        elif llm_callable is not None:
            self.llm_service = None
        elif use_default_llm:
            self.llm_service = _default_llm_service()
        else:
            self.llm_service = None

    @staticmethod
    def _gate_decision(res: dict[str, Any], *, checker: Any | None,
                       thresholds: dict[str, float], document_id: str,
                       attempt_number: int) -> dict[str, Any]:
        """The quality gate's verdict for ONE executed extraction.

        Returns {"status": PASS|WARNING|FAIL|None, "decision":
        "accept"|"continue"|"reject", "reason": str}. Only a real
        QualityChecker PASS (or a WARNING at/above the configured
        continue threshold) accepts — a successful extraction or
        non-empty text alone never qualifies.
        """
        name = res.get("extractor")
        text = res.get("extracted_text") or ""
        if res.get("status") != "success" or not text.strip():
            return {
                "status": "FAIL", "decision": "reject",
                "reason": res.get("error")
                or "Extraction failed or produced no usable text."}
        if checker is None:
            return {
                "status": None, "decision": "continue",
                "reason": "Quality Checker unavailable — no early-stop "
                          "decision was made for this attempt."}
        try:
            report = checker.check(
                text, document_id=document_id, source_tool=name,
                attempt_id=f"attempt-{attempt_number}")
        except Exception:
            logger.exception(
                "Quality Checker failed for gate attempt %s", name)
            return {
                "status": None, "decision": "continue",
                "reason": "Quality Checker error — no early-stop "
                          "decision was made for this attempt."}
        status = _gate_status(report, thresholds["fail_below"],
                              thresholds["warn_below"])
        scores = (f"Text Health {report.text_health.score}, "
                  f"Completeness {report.completeness.score}, "
                  f"Reading Order {report.reading_order.score}")
        if status == "PASS":
            return {
                "status": status, "decision": "accept",
                "reason": f"Quality PASS ({scores}) — result accepted; "
                          "fallback stopped."}
        if status == "FAIL":
            return {
                "status": status, "decision": "reject",
                "reason": f"Quality FAIL ({scores}) — Text Health or "
                          "Reading Order below "
                          f"{thresholds['fail_below']:g}; result rejected."}
        lowest = min(report.text_health.score, report.completeness.score,
                     report.reading_order.score)
        if lowest >= thresholds["warning_continue_below"]:
            return {
                "status": status, "decision": "accept",
                "reason": f"Quality WARNING ({scores}) accepted — lowest "
                          "filter score is at/above the continue "
                          "threshold "
                          f"{thresholds['warning_continue_below']:g}."}
        return {
            "status": status, "decision": "continue",
            "reason": f"Quality WARNING ({scores}) — result held as a "
                      "candidate; trying the next extractor."}

    def run(self, file_path: str | Path, include_extractor_texts: bool = False) -> dict[str, Any]:
        total_started = time.perf_counter()
        cfg = self.config

        # --- Extraction (sequential, quality-gated, early stopping) --
        # Extractors run ONE AT A TIME in configured order. After each
        # extraction the result goes through the existing quality
        # validation (the deterministic scorer AND the real
        # QualityChecker gate). A gate PASS accepts the extractor and
        # STOPS the flow — later extractors are never invoked and are
        # reported as skipped, not as attempts.
        if self.extractors is not None:
            extractor_map: dict[str, Any] = dict(self.extractors)
        else:
            extractor_map = default_sequential_extractors()
        order = ordered_extractor_names(
            extractor_map, preferred=list(cfg.tie_break_order or []))
        thresholds = _load_gate_thresholds()
        checker: Any | None = None
        try:
            from quality_checker import QualityChecker
            checker = QualityChecker()
        except Exception:
            logger.exception(
                "Quality Checker unavailable for the sequential gate; "
                "early stopping is disabled for this run and selection "
                "falls back to the quality-score selector")

        results: list[dict[str, Any]] = []
        reports: dict[str, Any] = {}
        gate_decisions: dict[str, dict[str, Any]] = {}
        skipped: list[str] = []
        accepted_name: str | None = None
        extraction_seconds = 0.0
        quality_seconds = 0.0
        for position, name in enumerate(order):
            t0 = time.perf_counter()
            res = run_one_extractor(
                name, extractor_map[name], file_path,
                timeout_seconds=cfg.per_extractor_timeout_seconds)
            extraction_seconds += time.perf_counter() - t0
            results.append(res)

            t0 = time.perf_counter()
            reports[name] = evaluate_extraction_quality(
                res, weights=cfg.effective_weights())
            decision = self._gate_decision(
                res, checker=checker, thresholds=thresholds,
                document_id=Path(file_path).name,
                attempt_number=position + 1)
            quality_seconds += time.perf_counter() - t0
            gate_decisions[name] = decision
            if decision["decision"] == "accept":
                accepted_name = name
                skipped = order[position + 1:]
                break

        # --- Selection ---
        if accepted_name is not None:
            selection = {
                "selected_extractor": accepted_name,
                "selected_quality_score":
                    reports[accepted_name].get("quality_score"),
                "selection_reason":
                    gate_decisions[accepted_name]["reason"]
                    + f" Selected '{accepted_name}' with quality score "
                      f"{reports[accepted_name].get('quality_score')}."
                    + (f" Skipped (never executed): "
                       f"{', '.join(skipped)}." if skipped else ""),
                "status": "selected",
            }
        else:
            # No extractor passed outright — the existing acceptance
            # policy decides: the selector picks the best-scoring
            # executed candidate (WARNING candidates included) when it
            # meets the configured minimum threshold; otherwise the
            # resume is failed/rejected as before.
            selection = select_best_extractor(results, reports, cfg)
            if selection.get("selected_extractor") is not None:
                _sel = selection["selected_extractor"]
                _gate = gate_decisions.get(_sel) or {}
                if _gate.get("decision") == "continue":
                    selection["selection_reason"] = (
                        _gate["reason"]
                        + " No extractor passed outright, so this "
                          "candidate was selected as the best of the "
                          "executed extractors. "
                        + str(selection.get("selection_reason") or ""))
        selected_name = selection.get("selected_extractor")
        selected_score = selection.get("selected_quality_score")
        selection_reason = selection.get("selection_reason")
        selection_status = selection.get("status")

        # Build extractor_results for output (strip texts by default)
        extractor_results_out: list[dict[str, Any]] = []
        for res in results:
            if include_extractor_texts:
                extractor_results_out.append(dict(res))
            else:
                stripped = {k: v for k, v in res.items() if k not in ("extracted_text", "page_texts", "text")}
                # keep a length hint without storing the copy
                text_val = res.get("extracted_text") or ""
                stripped["extracted_text_length"] = len(text_val)
                extractor_results_out.append(stripped)

        timings_base = {
            "extraction_seconds": round(extraction_seconds, 4),
            "quality_seconds": round(quality_seconds, 4),
        }

        if not selected_name:
            total_seconds = time.perf_counter() - total_started
            status = "below_threshold" if selection_status == "below_threshold" else "extraction_failed"
            return {
                "status": status,
                "selected_extractor": None,
                "selected_quality_score": selected_score,
                "selection_reason": selection_reason,
                "extractor_reports": reports,
                "extractor_results": extractor_results_out,
                "skipped_extractors": skipped,
                "gate_decisions": gate_decisions,
                "extractor_order": order,
                "original_text": None,
                "normalized_text": None,
                "normalization": {
                    "status": "skipped",
                    "original_chars": 0,
                    "normalized_chars": 0,
                    "normalization_applied": False,
                    "normalization_steps": [],
                    "error": None,
                },
                "llm": {
                    "status": "skipped",
                    "provider": None,
                    "model": None,
                    "resume_json": None,
                    "validation": None,
                    "error": None,
                    "error_type": None,
                    "attempts": 0,
                    "duration_ms": None,
                    "usage": {},
                },
                "contact_recovery": None,
                "resume_data": None,
                "timings": {
                    **timings_base,
                    "normalization_seconds": 0.0,
                    "llm_seconds": 0.0,
                    "total_seconds": round(total_seconds, 4),
                },
                "error": selection_reason,
            }

        # Find selected result
        selected_result = next((r for r in results if r.get("extractor") == selected_name), None)
        original_text = (selected_result.get("extracted_text") if selected_result else "") or ""

        # --- Normalization ---
        t0 = time.perf_counter()
        norm = normalize_extracted_text(original_text)
        normalization_seconds = time.perf_counter() - t0
        normalized_text = norm.get("normalized_text")
        if normalized_text is None:
            normalized_text = original_text
        normalization_block = {k: v for k, v in norm.items() if k not in ("original_text", "normalized_text")}

        # --- Contact Information Recovery ---
        # If phone / email / location are missing from the selected
        # normalized text, try to recover them — from the other
        # extractors' outputs, the PDF's first-page text layer, then
        # OCR — and merge ONLY validated recovered fields into the
        # text the LLM stage receives. The selected extractor and
        # its original text are never changed. A failure here must
        # never fail the resume: the uncorrected text goes onward.
        t0 = time.perf_counter()
        contact_recovery: dict[str, Any] | None = None
        try:
            normalized_text, contact_recovery = recover_contact_information(
                normalized_text=normalized_text,
                selected_extractor=selected_name,
                results=results,
                file_path=file_path)
        except Exception as exc:
            logger.exception("Contact Information Recovery failed; "
                             "continuing with the uncorrected text")
            contact_recovery = {"status": "error", "error": str(exc),
                                "applied": False, "recovered": {}}
        recovery_seconds = time.perf_counter() - t0

        # --- LLM (only selected normalized text) ---
        t0 = time.perf_counter()
        processor = LLMProcessor(service=self.llm_service, config=cfg, llm_callable=self.llm_callable)
        llm_result = processor.process(normalized_text)
        llm_seconds = time.perf_counter() - t0

        resume_data = llm_result.get("resume_json") if llm_result.get("status") == "success" else None
        if llm_result.get("status") == "success" and llm_result.get("validation") in (None, "valid") and resume_data is not None:
            status = "success"
            error = None
        elif llm_result.get("status") == "not_configured":
            status = "llm_not_configured"
            error = llm_result.get("error")
            resume_data = None
        else:
            status = "llm_failed"
            error = llm_result.get("error")
            resume_data = None

        total_seconds = time.perf_counter() - total_started
        return {
            "status": status,
            "selected_extractor": selected_name,
            "selected_quality_score": selected_score,
            "selection_reason": selection_reason,
            "extractor_reports": reports,
            "extractor_results": extractor_results_out,
            "skipped_extractors": skipped,
            "gate_decisions": gate_decisions,
            "extractor_order": order,
            "original_text": original_text,
            "normalized_text": normalized_text,
            "normalization": normalization_block,
            "contact_recovery": contact_recovery,
            "llm": llm_result,
            "resume_data": resume_data,
            "timings": {
                **timings_base,
                "normalization_seconds": round(normalization_seconds, 4),
                "recovery_seconds": round(recovery_seconds, 4),
                "llm_seconds": round(llm_seconds, 4),
                "total_seconds": round(total_seconds, 4),
            },
            "error": error,
        }
