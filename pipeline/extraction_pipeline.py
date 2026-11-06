"""Simple production extraction pipeline.

DOCUMENT -> Tika + PyMuPDF + Docling (concurrent) -> Quality Evaluation
-> Best Extractor Selection -> Text Normalization -> One LLM Stage
-> Validated Resume JSON.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .config import PipelineConfig, load_pipeline_config
from .extractors import run_extractors
from .llm import LLMProcessor
from .normalization import normalize_extracted_text
from .quality import evaluate_extraction_quality
from .selector import select_best_extractor

logger = logging.getLogger(__name__)


def _default_llm_service() -> Any | None:
    try:
        from muse_ai import MuseService, load_muse_config
        return MuseService(load_muse_config())
    except Exception:
        logger.exception("Muse AI service unavailable for simple pipeline; LLM stage will report not_configured")
        return None


class ExtractionPipeline:
    """The simple pipeline. Legacy ExtractionManager chain is untouched
    and remains available via api.pipeline.process_resume_file (legacy mode).
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

    def run(self, file_path: str | Path, include_extractor_texts: bool = False) -> dict[str, Any]:
        total_started = time.perf_counter()
        cfg = self.config

        # --- Extraction (concurrent) ---
        t0 = time.perf_counter()
        results = run_extractors(file_path, extractors=self.extractors, timeout_seconds=cfg.per_extractor_timeout_seconds)
        extraction_seconds = time.perf_counter() - t0

        # --- Quality evaluation ---
        t0 = time.perf_counter()
        reports: dict[str, Any] = {}
        for res in results:
            name = res.get("extractor")
            reports[name] = evaluate_extraction_quality(res, weights=cfg.effective_weights())
        quality_seconds = time.perf_counter() - t0

        # --- Selection ---
        selection = select_best_extractor(results, reports, cfg)
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
            "original_text": original_text,
            "normalized_text": normalized_text,
            "normalization": normalization_block,
            "llm": llm_result,
            "resume_data": resume_data,
            "timings": {
                **timings_base,
                "normalization_seconds": round(normalization_seconds, 4),
                "llm_seconds": round(llm_seconds, 4),
                "total_seconds": round(total_seconds, 4),
            },
            "error": error,
        }
