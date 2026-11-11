"""Execution of the independent extractors.

Two runners share the same unified result dicts:

- :func:`run_extractors` — the original CONCURRENT runner (all
  extractors at once). Still used by the extractor diagnostics live
  test, which deliberately exercises every installed extractor.
- :func:`run_one_extractor` — runs ONE extractor, used by the
  production pipeline's sequential quality-gated flow
  (Tika → PyMuPDF → Docling → OCR with early stopping): the next
  extractor is only invoked when the previous one fails its quality
  gate, so a passing Tika never pays for Docling/OCR time.

Results are returned in canonical order
[tika, pymupdf, docling, ocr] as unified dicts.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CANONICAL_ORDER = ["tika", "pymupdf", "docling", "ocr"]


def _default_extractors() -> dict[str, Any]:
    """Construct the real extractors using the manager config sections."""
    extractors: dict[str, Any] = {}
    per_extractor: dict[str, Any] = {}
    try:
        from extractor.manager import load_manager_config
        cfg = load_manager_config()
        per_extractor = cfg.get("extractors", {}) or {}
    except Exception:
        logger.warning("Could not load manager config for pipeline extractors; using defaults", exc_info=True)
        per_extractor = {}
    try:
        from extractor.extractors.tika import TikaExtractor
        extractors["tika"] = TikaExtractor(config=per_extractor.get("tika", {}))
    except Exception as exc:
        logger.warning("Tika extractor construction failed: %s", exc)
        extractors["tika"] = _FailingExtractor("tika", f"Tika extractor unavailable: {exc}")
    try:
        from extractor.extractors.pymupdf import PyMuPDFExtractor
        extractors["pymupdf"] = PyMuPDFExtractor(config=per_extractor.get("pymupdf", {}))
    except Exception as exc:
        logger.warning("PyMuPDF extractor construction failed: %s", exc)
        extractors["pymupdf"] = _FailingExtractor("pymupdf", f"PyMuPDF extractor unavailable: {exc}")
    try:
        from extractor.extractors.docling import DoclingExtractor
        extractors["docling"] = DoclingExtractor(config=per_extractor.get("docling", {}))
    except Exception as exc:
        logger.warning("Docling extractor construction failed: %s", exc)
        extractors["docling"] = _FailingExtractor("docling", f"Docling extractor unavailable: {exc}")
    return extractors


class _FailingExtractor:
    def __init__(self, name: str, error: str):
        self.name = name
        self._error = error

    def extract(self, path):  # noqa: ANN001
        from extractor.models import ExtractionResult
        return ExtractionResult(extractor_name=self.name, success=False, text="", error=self._error, error_type="ExtractorUnavailableError")


def _coerce_raw(name: str, raw: Any, elapsed: float) -> dict[str, Any]:
    """Normalise any extractor return value into the unified dict."""
    # dict form
    if isinstance(raw, dict):
        text = raw.get("extracted_text", raw.get("text", "")) or ""
        success = raw.get("success")
        if success is None:
            success = raw.get("status") == "success"
        error = raw.get("error")
        duration_ms = raw.get("duration_ms")
        if duration_ms is not None:
            exec_time = float(duration_ms) / 1000.0
        elif raw.get("execution_time") is not None:
            exec_time = float(raw["execution_time"])
        else:
            exec_time = elapsed
        return {
            "extractor": name,
            "status": "success" if success else "failed",
            "extracted_text": text if isinstance(text, str) else str(text),
            "execution_time": float(exec_time),
            "metadata": raw.get("metadata") or {},
            "error": error,
            "error_type": raw.get("error_type"),
            "page_count": raw.get("page_count"),
            "page_texts": raw.get("page_texts") or [],
        }
    # object form (ExtractionResult or simple namespace)
    text = getattr(raw, "text", None)
    if text is None:
        text = getattr(raw, "extracted_text", "") or ""
    success = getattr(raw, "success", None)
    if success is None:
        st = getattr(raw, "status", None)
        success = st == "success" if isinstance(st, str) else bool(st)
    error = getattr(raw, "error", None)
    duration_ms = getattr(raw, "duration_ms", None)
    if duration_ms:
        exec_time = float(duration_ms) / 1000.0
    else:
        exec_time = elapsed
    metadata = getattr(raw, "metadata", None) or {}
    if not isinstance(metadata, dict):
        try:
            metadata = dict(metadata)  # type: ignore[arg-type]
        except Exception:
            metadata = {}
    return {
        "extractor": name,
        "status": "success" if success else "failed",
        "extracted_text": text if isinstance(text, str) else str(text),
        "execution_time": float(exec_time),
        "metadata": metadata,
        "error": error,
        "error_type": getattr(raw, "error_type", None),
        "page_count": getattr(raw, "page_count", None),
        "page_texts": getattr(raw, "page_texts", None) or [],
    }


def default_sequential_extractors() -> dict[str, Any]:
    """The default extractor map for the sequential production flow:
    the three document extractors plus the OCR extractor as the final
    fallback. An OCR that cannot be constructed becomes a failing
    stub whose honest error is recorded if — and only if — the flow
    actually reaches it."""
    extractors = _default_extractors()
    if "ocr" not in extractors:
        per_extractor: dict[str, Any] = {}
        try:
            from extractor.manager import load_manager_config
            per_extractor = load_manager_config().get("extractors", {}) or {}
        except Exception:
            per_extractor = {}
        try:
            from extractor.extractors.ocr import OCRExtractor
            extractors["ocr"] = OCRExtractor(config=per_extractor.get("ocr", {}))
        except Exception as exc:
            logger.warning("OCR extractor construction failed: %s", exc)
            extractors["ocr"] = _FailingExtractor(
                "ocr", f"OCR extractor unavailable: {exc}")
    return extractors


def ordered_extractor_names(extractor_map: dict[str, Any],
                            preferred: list[str] | None = None) -> list[str]:
    """Execution order for a sequential run: the preferred order
    first (configured tie-break order), then any remaining canonical
    extractors, then any extras — restricted to names actually
    present in the map."""
    names: list[str] = []
    for n in list(preferred or []) + CANONICAL_ORDER:
        if n in extractor_map and n not in names:
            names.append(n)
    for n in extractor_map:
        if n not in names:
            names.append(n)
    return names


def _failed_result(name: str, elapsed: float, error: str,
                   error_type: str | None) -> dict[str, Any]:
    return {
        "extractor": name,
        "status": "failed",
        "extracted_text": "",
        "execution_time": float(elapsed),
        "metadata": {},
        "error": error,
        "error_type": error_type,
        "page_count": None,
        "page_texts": [],
    }


def _timeout_result(name: str, timeout: float | None,
                    elapsed: float) -> dict[str, Any]:
    return {
        "extractor": name,
        "status": "timeout",
        "extracted_text": "",
        "execution_time": float(timeout if timeout else elapsed),
        "metadata": {},
        "error": f"Extractor '{name}' timed out after {timeout:g}s" if timeout else f"Extractor '{name}' timed out",
        "error_type": "TimeoutError",
        "page_count": None,
        "page_texts": [],
    }


def run_one_extractor(name: str, extractor_obj: Any, file_path: str | Path,
                      timeout_seconds: float | None = None) -> dict[str, Any]:
    """Run exactly ONE extractor and return its unified result dict.

    Same result shape and same EXTRACTION-ONLY timeout semantics as
    :func:`run_extractors` (``timeout_seconds`` is the maximum time to
    WAIT for this extractor; None waits however long it takes). Used
    by the sequential quality-gated pipeline so fallback extractors
    are never invoked once an earlier extractor passes.
    """
    timeout = float(timeout_seconds) if timeout_seconds else None
    started = time.perf_counter()
    if timeout is None:
        try:
            raw = _run_one(name, extractor_obj, file_path)
            return _coerce_raw(name, raw, time.perf_counter() - started)
        except Exception as exc:
            return _failed_result(name, time.perf_counter() - started,
                                  f"{type(exc).__name__}: {exc}",
                                  type(exc).__name__)
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(_run_one, name, extractor_obj, file_path)
        try:
            raw = future.result(timeout=timeout)
            return _coerce_raw(name, raw, time.perf_counter() - started)
        except FutureTimeoutError:
            return _timeout_result(name, timeout, time.perf_counter() - started)
        except Exception as exc:
            return _failed_result(name, time.perf_counter() - started,
                                  f"{type(exc).__name__}: {exc}",
                                  type(exc).__name__)
    finally:
        executor.shutdown(wait=False)


def _run_one(name: str, extractor_obj: Any, file_path: str | Path) -> Any:
    """Call one extractor in whatever form it was injected."""
    path = Path(file_path)
    if hasattr(extractor_obj, "extract"):
        return extractor_obj.extract(path)
    if callable(extractor_obj):
        return extractor_obj(path)
    raise TypeError(f"Extractor '{name}' is neither an object with .extract() nor callable")


def run_extractors(file_path: str | Path, extractors: dict[str, Any] | None = None, timeout_seconds: float | None = None) -> list[dict[str, Any]]:
    """Run all three extractors concurrently.

    Args:
        file_path: document to extract.
        extractors: optional dict name-> extractor object/callable.
            When None, the real Tika/PyMuPDF/Docling extractors are used.
        timeout_seconds: EXTRACTION-ONLY safety limit — the maximum
            time to WAIT for one extractor (a stuck-process guard;
            the pipeline passes its configured, generous value).
            On timeout the result has status "timeout". None (the
            default for direct callers) waits however long it
            takes. This never applies to Muse AI calls.

    Returns:
        List of unified result dicts in canonical order.
    """
    if extractors is None:
        extractor_map: dict[str, Any] = _default_extractors()
    else:
        extractor_map = dict(extractors)

    # Determine names to run: canonical order first, then any extras
    names: list[str] = []
    for n in CANONICAL_ORDER:
        if n in extractor_map:
            names.append(n)
    for n in extractor_map:
        if n not in names:
            names.append(n)
    # If an injected map is missing some canonical extractors, we still
    # only run what was provided (tests inject exactly what they want).
    # For the default path, all three are present.

    results_by_name: dict[str, dict[str, Any]] = {}

    if not names:
        return []

    timeout = float(timeout_seconds) if timeout_seconds else None

    executor = ThreadPoolExecutor(max_workers=3)
    try:
        futures = {}
        start_times = {}
        for name in names:
            start_times[name] = time.perf_counter()
            futures[name] = executor.submit(_run_one, name, extractor_map[name], file_path)
        for name in names:
            started = start_times[name]
            future = futures[name]
            try:
                if timeout is not None:
                    raw = future.result(timeout=timeout)
                else:
                    raw = future.result()
                elapsed = time.perf_counter() - started
                results_by_name[name] = _coerce_raw(name, raw, elapsed)
            except FutureTimeoutError:
                elapsed = time.perf_counter() - started
                results_by_name[name] = {
                    "extractor": name,
                    "status": "timeout",
                    "extracted_text": "",
                    "execution_time": float(timeout if timeout else elapsed),
                    "metadata": {},
                    "error": f"Extractor '{name}' timed out after {timeout:g}s" if timeout else f"Extractor '{name}' timed out",
                    "error_type": "TimeoutError",
                    "page_count": None,
                    "page_texts": [],
                }
            except Exception as exc:
                elapsed = time.perf_counter() - started
                results_by_name[name] = {
                    "extractor": name,
                    "status": "failed",
                    "extracted_text": "",
                    "execution_time": float(elapsed),
                    "metadata": {},
                    "error": f"{type(exc).__name__}: {exc}",
                    "error_type": type(exc).__name__,
                    "page_count": None,
                    "page_texts": [],
                }
    finally:
        executor.shutdown(wait=False)

    # Canonical order output
    ordered: list[dict[str, Any]] = []
    for n in CANONICAL_ORDER:
        if n in results_by_name:
            ordered.append(results_by_name[n])
    for n, r in results_by_name.items():
        if n not in CANONICAL_ORDER:
            ordered.append(r)
    return ordered
