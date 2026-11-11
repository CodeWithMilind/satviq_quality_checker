"""One-click extractor diagnostics (Settings → Extractor Status).

Two levels of check, both honest about what they prove:

* :func:`probe_extractors` — an INSTALLATION probe. For every
  extractor it verifies the pieces that extractor needs on this
  machine (Tika: Java runtime + tika-app JAR; PyMuPDF / Docling:
  the Python package; OCR: the Tesseract binary + pytesseract),
  reusing each extractor's OWN resolution logic and error messages
  rather than duplicating them. "available" means the installation
  looks ready — it is not proof an extraction succeeds.
* :func:`run_live_tests` — a REAL extraction of the bundled
  ``sample_resume.pdf`` through the same code paths production
  uses (the concurrent pipeline's ``run_extractors`` with the
  pipeline's configured per-extractor safety cap, and the OCR
  extractor directly). This is the working / not-working proof:
  per-extractor status, extracted character count, duration and
  the real error when one fails.

No result is ever fabricated: a probe or test that cannot run
reports the underlying error verbatim.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LABELS = {
    "tika": "Apache Tika",
    "pymupdf": "PyMuPDF",
    "docling": "Docling",
    "ocr": "Tesseract OCR",
}
ORDER = ["tika", "ocr", "pymupdf", "docling"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _manager_config() -> dict[str, dict[str, Any]]:
    """Per-extractor config sections from extractor.yaml (the same
    config the extraction manager and the pipeline run with)."""
    from extractor.manager import load_manager_config
    config = load_manager_config()
    return config.get("extractors", {}) or {}


def _row(name: str, status: str, detail: str = "",
         error: str | None = None) -> dict[str, Any]:
    return {"name": name, "label": LABELS[name], "status": status,
            "detail": detail, "error": error}


def probe_tika(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Tika needs BOTH the tika-app JAR and a Java runtime."""
    from extractor.exceptions import ExtractorUnavailableError
    from extractor.extractors.tika import TikaExtractor

    ext = TikaExtractor(config=config or {})
    problems: list[str] = []
    details: list[str] = []
    try:
        jar = ext._require_jar()
        details.append(f"Tika JAR {ext.version or ''} found "
                       f"({jar.name})".replace("  ", " "))
    except ExtractorUnavailableError as exc:
        problems.append(str(exc))
    java_bin = ext.java_bin
    if any(sep in java_bin for sep in ("/", "\\")):
        java_found = Path(java_bin).exists()
        java_where = java_bin
    else:
        resolved = shutil.which(java_bin)
        java_found = resolved is not None
        java_where = resolved or java_bin
    if java_found:
        details.append(f"Java runtime: {java_where}")
    else:
        problems.append(
            f"Java runtime not found ('{java_bin}' is not on PATH and "
            "is not an existing file). Tika cannot run without Java.")
    # Tesseract for Tika's internal image OCR — informational only:
    # Tika extracts PDFs fine without it.
    tess_dir = ext._resolve_tesseract_dir()
    if tess_dir:
        details.append(f"Tesseract for Tika OCR: {tess_dir}")
    else:
        details.append("Tesseract for Tika OCR: not found "
                       "(PDF text extraction unaffected; image OCR "
                       "inside Tika unavailable)")
    if problems:
        return _row("tika", "unavailable", "; ".join(details),
                    " ".join(problems))
    return _row("tika", "available", "; ".join(details))


def probe_pymupdf() -> dict[str, Any]:
    try:
        import pymupdf  # type: ignore
        version = getattr(pymupdf, "__version__", None)
        if not version:
            import importlib.metadata
            version = importlib.metadata.version("PyMuPDF")
        return _row("pymupdf", "available", f"PyMuPDF {version} installed")
    except Exception as exc:  # ImportError and friends
        return _row("pymupdf", "unavailable", "",
                    f"PyMuPDF is not importable ({exc}). "
                    "Install it with: pip install PyMuPDF")


def probe_docling() -> dict[str, Any]:
    try:
        import docling  # noqa: F401  type: ignore
        version = None
        try:
            import importlib.metadata
            version = importlib.metadata.version("docling")
        except Exception:
            version = getattr(docling, "__version__", None)
        detail = f"Docling {version} installed".replace(" None", "")
        return _row("docling", "available",
                    detail + " — package present; run the live test "
                             "to verify a real conversion")
    except Exception as exc:
        return _row("docling", "unavailable", "",
                    "The 'docling' package is not installed. "
                    f"Install it with: pip install docling ({exc})")


def probe_ocr(config: dict[str, Any] | None = None) -> dict[str, Any]:
    from extractor.exceptions import ExtractorUnavailableError
    from extractor.extractors.ocr import OCRExtractor

    ext = OCRExtractor(config=config or {})
    try:
        ext._load_pytesseract()
    except ExtractorUnavailableError as exc:
        return _row("ocr", "unavailable", "", str(exc))
    path = ext._tesseract_path()
    return _row("ocr", "available", f"Tesseract binary: {path}")


_PROBES = {"tika": probe_tika, "pymupdf": probe_pymupdf,
           "docling": probe_docling, "ocr": probe_ocr}


def probe_extractors() -> dict[str, Any]:
    """Installation probe for every extractor. A probe that itself
    crashes reports that extractor unavailable with the real error —
    the endpoint never fails because one probe did."""
    per_extractor = _manager_config()
    rows = []
    for name in ORDER:
        try:
            if name in ("tika", "ocr"):
                rows.append(_PROBES[name](per_extractor.get(name) or {}))
            else:
                rows.append(_PROBES[name]())
        except Exception as exc:  # a probe must never 500 the page
            rows.append(_row(name, "unavailable", "",
                             f"Probe failed: {exc}"))
    return {"checked_at": _now(), "extractors": rows}


def sample_path() -> Path:
    from api import store
    return store.PROJECT_ROOT / "sample_resume.pdf"


def run_live_tests(path: Path) -> dict[str, Any]:
    """Extract ``path`` with every extractor for real.

    The three document extractors go through the production
    concurrent runner with the pipeline's configured per-extractor
    safety cap; OCR runs its extractor directly (it is not part of
    the concurrent set)."""
    from pipeline import extractors as pipeline_extractors
    from pipeline.config import load_pipeline_config

    cap = load_pipeline_config().per_extractor_timeout_seconds
    results: dict[str, Any] = {}
    raw = pipeline_extractors.run_extractors(
        str(path),
        extractors=pipeline_extractors._default_extractors(),
        timeout_seconds=cap)
    for entry in raw:
        results[entry["extractor"]] = {
            "status": entry["status"],
            "chars": entry.get("character_count") or 0,
            "duration_ms": round((entry.get("execution_time") or 0) * 1000),
            "error": entry.get("error"),
        }
    # OCR — direct extractor call, same interface as the chain.
    try:
        from extractor.extractors.ocr import OCRExtractor
        ocr = OCRExtractor(config=_manager_config().get("ocr") or {})
        result = ocr.extract(str(path))
        results["ocr"] = {
            "status": "success" if result.success else "failed",
            "chars": len(result.text or ""),
            "duration_ms": round(result.duration_ms or 0),
            "error": result.error,
        }
    except Exception as exc:
        results["ocr"] = {"status": "failed", "chars": 0,
                          "duration_ms": 0, "error": str(exc)}
    ordered = {name: results[name] for name in ORDER if name in results}
    return {"file": Path(path).name, "ran_at": _now(), "results": ordered}
