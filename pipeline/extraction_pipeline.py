"""Simple production extraction pipeline.

DOCUMENT -> Sequential Extraction-Gated Extraction
(Tika -> Extraction Check -> STOP on accept; otherwise OCR ->
Extraction Check -> STOP on accept; otherwise PyMuPDF -> Extraction
Check; otherwise Docling) -> Best Extractor Selection (only when no
extractor was accepted outright) -> Text Normalization -> One LLM
Stage -> Validated Resume JSON.

EXTRACTION GATING (revised 2026-11-12 — WARNING no longer accepts):
technical extraction status (success/failed/timeout) and extraction
QUALITY (PASS/WARNING/FAIL from the Quality Checker) are separate
decisions. A technically successful WARNING is NOT a final result
while another extractor remains untried: the result is held as a
candidate and the chain continues, because a WARNING fragment can
hide extraction loss (the download_11_.pdf case: Tika returned 771
characters with only Contact Information detected on a resume that
visibly contains education, experience, skills and languages, and
the old gate accepted it and skipped every other extractor).

Gate outcomes per attempt:
- **PASS** (all filters >= ``warn_below``) — accept immediately;
  later extractors are SKIPPED. Clean documents still stop at the
  first extractor.
- **WARNING** — held as a candidate; the next extractor is tried.
  Completeness is never the SOLE signal: the decision reason records
  the adequacy signals (characters, words, detected sections,
  characters per page when the page count is known) alongside the
  filter scores.
- **FAIL** (extraction failure, or Text Health / Reading Order
  below ``fail_below``) — rejected; the next extractor is tried.

Two protections keep WARNING-continue from wasting time:
- **Convergence stop** — when a new candidate's text is
  substantially the same as the best earlier candidate (token-set
  similarity >= ``convergence_similarity``) and detects no
  additional sections, the extractors agree: the content is
  genuinely what the document contains, so the chain stops and the
  best candidate is selected. A readable resume with unfamiliar
  headings (the case the 2026-11-11 Part A fix protected) stops at
  the second extractor instead of burning Docling/OCR time.
- **OCR necessity** — OCR runs only when no adequate candidate
  exists: every earlier attempt failed, or the best candidate shows
  image/scanned-document signals (very low characters per page, or
  at most ``ocr_max_detected_sections`` detected sections in under
  ``ocr_max_candidate_chars`` characters). Otherwise OCR is SKIPPED
  with the reason recorded.

If no extractor is accepted outright, the existing selector picks
the best-scoring executed candidate (``min_quality_threshold`` still
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
                  "warning_continue_below": 75.0,
                  "convergence_similarity": 0.90,
                  "ocr_min_chars_per_page": 500,
                  "ocr_max_detected_sections": 3,
                  "ocr_max_candidate_chars": 2000}


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
    """Overall quality status for a QualityChecker report — the same
    mapping as api.pipeline.quality_status_for: FAIL only when Text
    Health or Reading Order is below fail_below; WARNING when any
    filter (Completeness included) is below warn_below; PASS
    otherwise. This is the REPORTED status; the gate's accept /
    continue decision is computed separately in _gate_decision and
    never lets Completeness drive fallback."""
    if report.text_health.score < fail_below \
            or report.reading_order.score < fail_below:
        return "FAIL"
    if min(report.text_health.score, report.completeness.score,
           report.reading_order.score) < warn_below:
        return "WARNING"
    return "PASS"


def _token_similarity(a: str, b: str) -> float:
    """Jaccard similarity of the two texts' word-token sets."""
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _signals(res: dict[str, Any], report: Any) -> dict[str, Any]:
    """Deterministic extraction-adequacy signals for one attempt."""
    text = res.get("extracted_text") or ""
    pages = res.get("page_count")
    try:
        pages = int(pages) if pages else None
    except (TypeError, ValueError):
        pages = None
    completeness = getattr(report, "completeness", None)
    return {
        "chars": len(text),
        "words": len(text.split()),
        "page_count": pages,
        "chars_per_page": (round(len(text) / pages, 1)
                           if pages else None),
        "detected_sections": len(
            getattr(completeness, "detected_sections", []) or []),
        "completeness_score": getattr(completeness, "score", None),
    }


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
        """The extraction gate's verdict for ONE executed extraction.

        Returns {"status": PASS|WARNING|FAIL|None, "decision":
        "accept"|"continue"|"reject", "reason": str}. The DECISION
        judges extraction quality only — extraction failure, Text
        Health, Reading Order. Completeness is reported in the
        status/reason but can never cause a continue or a reject:
        a completeness-only WARNING is accepted immediately.
        """
        name = res.get("extractor")
        text = res.get("extracted_text") or ""
        if res.get("status") != "success" or not text.strip():
            return {
                "status": "FAIL", "decision": "reject",
                "signals": {"chars": len(text), "words": len(text.split()),
                            "page_count": res.get("page_count"),
                            "chars_per_page": None,
                            "detected_sections": 0,
                            "completeness_score": None},
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
        signals = _signals(res, report)
        scores = (f"Text Health {report.text_health.score}, "
                  f"Completeness {report.completeness.score}, "
                  f"Reading Order {report.reading_order.score}")
        sig_text = (
            f"{signals['chars']} characters, {signals['words']} words, "
            f"{signals['detected_sections']} resume sections detected"
            + (f", {signals['chars_per_page']} characters/page across "
               f"{signals['page_count']} page(s)"
               if signals["chars_per_page"] is not None else ""))
        if status == "PASS":
            return {
                "status": status, "decision": "accept",
                "signals": signals,
                "reason": f"Quality PASS ({scores}; {sig_text}) — "
                          "result accepted; fallback stopped."}
        if status == "FAIL":
            return {
                "status": status, "decision": "reject",
                "signals": signals,
                "reason": f"Quality FAIL ({scores}; {sig_text}) — "
                          "Text Health or Reading Order below "
                          f"{thresholds['fail_below']:g}; result rejected."}
        # WARNING — never a final result while another extractor
        # remains: a technically successful fragment can hide
        # extraction loss (download_11_.pdf: 771 characters, one
        # section detected, on a visibly complete resume). Hold as
        # a candidate and try the next extractor; the loop's
        # convergence and OCR-necessity rules stop the chain when
        # further extraction demonstrably adds nothing.
        weak = []
        tech_lowest = min(report.text_health.score,
                          report.reading_order.score)
        if tech_lowest < thresholds["warning_continue_below"]:
            weak.append("a technical filter is below the continue "
                        "threshold")
        if report.completeness.score < thresholds["warn_below"]:
            weak.append("content coverage is incomplete — sections "
                        "may be genuinely absent OR lost in extraction, "
                        "which only another extractor can distinguish")
        if signals["chars_per_page"] is not None and \
                signals["chars_per_page"] < \
                thresholds["ocr_min_chars_per_page"]:
            weak.append("text density is very low for the page "
                        "count, suggesting image-based content")
        return {
            "status": status, "decision": "continue",
            "signals": signals,
            "reason": f"Quality WARNING ({scores}; {sig_text}) — "
                      + ("; ".join(weak) if weak else "quality below "
                         "PASS")
                      + ". Result held as a candidate; trying the "
                        "next extractor for comparison."}

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
        candidates: list[str] = []  # WARNING attempts held for comparison
        accepted_name: str | None = None
        extraction_seconds = 0.0
        quality_seconds = 0.0
        for position, name in enumerate(order):
            # --- OCR necessity: OCR is for scanned/image-based
            # documents. When a text-layer candidate is already
            # adequate, OCR is skipped with the reason recorded.
            if name == "ocr" and candidates:
                # Adequacy is judged on the MOST COMPLETE candidate
                # (sections detected, then characters), not the
                # highest quality score: a short high-scoring
                # fragment must not suppress OCR.
                best_so_far = max(
                    candidates,
                    key=lambda n: (
                        (gate_decisions[n].get("signals") or {}).get(
                            "detected_sections", 0),
                        (gate_decisions[n].get("signals") or {}).get(
                            "chars", 0)))
                sig = gate_decisions[best_so_far].get("signals") or {}
                cpp = sig.get("chars_per_page")
                ocr_needed = (
                    (cpp is not None and cpp < thresholds[
                        "ocr_min_chars_per_page"])
                    or (sig.get("detected_sections", 99) <= thresholds[
                            "ocr_max_detected_sections"]
                        and sig.get("chars", 0) < thresholds[
                            "ocr_max_candidate_chars"]))
                if not ocr_needed:
                    gate_decisions[name] = {
                        "status": None, "decision": "skip",
                        "signals": sig,
                        "reason": (
                            f"OCR skipped — the best text-layer "
                            f"candidate ('{best_so_far}') already "
                            f"provides {sig.get('chars')} characters "
                            f"across {sig.get('detected_sections')} "
                            "detected sections; OCR is reserved for "
                            "scanned/image-based documents where text "
                            "extraction is inadequate.")}
                    skipped.append(name)
                    continue
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
                skipped = skipped + [n for n in order[position + 1:]
                                     if n not in skipped]
                break
            # --- Convergence stop: a new WARNING candidate that
            # substantially duplicates the best earlier candidate
            # (and detects no additional sections) proves the
            # extractors agree — the content is genuinely what the
            # document contains, so stop and select the best.
            if decision["decision"] == "continue" \
                    and decision.get("status") == "WARNING":
                candidates.append(name)
                if len(candidates) >= 2:
                    prior = max(
                        candidates[:-1],
                        key=lambda n: reports[n].get("quality_score")
                        or 0)
                    sim = _token_similarity(
                        res.get("extracted_text") or "",
                        next(r.get("extracted_text") or ""
                             for r in results
                             if r.get("extractor") == prior))
                    new_det = (decision.get("signals") or {}).get(
                        "detected_sections", 0)
                    prior_det = (gate_decisions[prior].get("signals")
                                 or {}).get("detected_sections", 0)
                    if sim >= thresholds["convergence_similarity"] \
                            and new_det <= prior_det:
                        best = max(
                            candidates,
                            key=lambda n: reports[n].get(
                                "quality_score") or 0)
                        gate_decisions[best]["reason"] += (
                            f" Extraction stopped by convergence: "
                            f"'{name}' produced substantially the "
                            f"same text as '{prior}' (token "
                            f"similarity {sim:.2f}) with no "
                            "additional sections detected — the "
                            "extractors agree, so unlisted sections "
                            "are genuinely absent from the document "
                            "rather than lost in extraction.")
                        accepted_name = best
                        skipped = skipped + [n for n in
                                             order[position + 1:]
                                             if n not in skipped]
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
