"""Tika OCR info on concurrent-pipeline attempts.

The concurrent (default) pipeline's attempt builder used to store
``ocr: None`` even though Tika's result metadata carries the OCR
facts — so the attempt card showed "OCR: N/A" for every Tika
attempt. The builder now lifts the facts via ``_ocr_info`` (the
same helper the chain/manual paths use), and records stored
earlier are backfilled from their stored attempt metadata on read.
Nothing is invented: extractors whose metadata has no OCR keys
keep ``ocr=None``, and for PDFs ``ocr_used`` stays None (unknown) —
Tika's output carries no per-page OCR evidence.
"""
from __future__ import annotations

import json
from pathlib import Path

from api.pipeline import _normalize_attempt_fields, _ocr_info
from api.pipeline import process_resume_file_simple
from extractor.models import ExtractionResult
from muse_ai.schema import validate_resume_json
from pipeline import ExtractionPipeline, PipelineConfig

GOOD_TEXT = """Jane Doe
jane.doe@example.com | +91 98765 43210 | Pune, India

Professional Summary
Python developer building practical AI and backend applications for real users.
Focused on FastAPI services, data pipelines, and LLM powered tools with tested code.

Skills
Python, FastAPI, REST APIs, SQL, MySQL, Pandas, NumPy, LangChain, and Git are used daily.
Built backend services with authentication, validation, and structured logging.

Work Experience
Data Scientist Intern at Example AI Solutions from May 2025 to November 2025.
Cleaned datasets, trained classification models, and documented experiments.
Developed APIs and wrote tests for data processing pipelines used in production.

Education
Master of Computer Applications, Example University, 2027.
Bachelor of Computer Applications, Example College, 2025.

Certifications
Certified Data Scientist, Example Institute, 2025.
"""

TIKA_OCR_METADATA = {
    "ocr_enabled": True,
    "ocr_used": None,  # PDF: no per-page evidence — honestly unknown
    "ocr_language": "eng",
    "ocr_strategy": "auto",
    "ocr_tesseract_path": "C:\\Program Files\\Tesseract-OCR",
    "ocr_evidence": "OCR was available to Tika for this attempt (PDF "
                    "page-text strategy 'auto'); the --text CLI output "
                    "carries no per-page OCR marker, so whether "
                    "specific pages were OCR'd is not reported.",
}

VALID_RESUME = {
    "full_name": "Jane Doe", "email": "jane.doe@example.com",
    "phone": "+91 98765 43210", "location": "Pune, India",
    "summary": "Python developer", "skills": ["Python", "FastAPI"],
    "experience": [], "education": [], "certifications": [],
    "languages": [], "links": [],
}
USAGE = {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}


class FakeExtractor:
    def __init__(self, name, text="", success=True, metadata=None):
        self.name = name
        self._text = text
        self._success = success
        self._metadata = metadata or {}

    def extract(self, path):
        if not self._success:
            return ExtractionResult(
                extractor_name=self.name, success=False, text="",
                error=f"{self.name} unavailable",
                error_type="ExtractionFailedError", duration_ms=5.0,
                page_count=None, page_texts=[], metadata=self._metadata)
        return ExtractionResult(
            extractor_name=self.name, success=True, text=self._text,
            error=None, duration_ms=5.0, page_count=1,
            page_texts=[self._text] if self._text else [],
            metadata=self._metadata)


class FakeMuseService:
    configured = True
    model = "fake-model"
    provider_name = "Muse AI"
    configuration_error = None

    def generate_resume_json(self, text, hyperlinks=None):
        parsed, _err = validate_resume_json(json.dumps(VALID_RESUME))
        assert parsed is not None
        return {"ok": True, "error": None, "error_type": None,
                "resume_json": parsed, "validation": "valid",
                "provider": "Muse AI", "model": "fake-model",
                "raw_text": json.dumps(VALID_RESUME),
                "usage": dict(USAGE), "duration_ms": 12.0,
                "request_id": "req-test-1"}


def test_simple_pipeline_attempt_carries_tika_ocr_info(tmp_path):
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-fake-content")
    extractors = {
        "tika": FakeExtractor("tika", GOOD_TEXT,
                              metadata=dict(TIKA_OCR_METADATA)),
        "pymupdf": FakeExtractor("pymupdf", success=False),
        "docling": FakeExtractor("docling", success=False),
    }
    pipe = ExtractionPipeline(config=PipelineConfig(),
                              extractors=extractors,
                              llm_service=FakeMuseService())
    record = process_resume_file_simple(
        pdf, resume_id="ocr-info-0001", original_filename="resume.pdf",
        pipeline=pipe)
    by_name = {a["extractor_name"]: a for a in record["attempts"]}
    tika_ocr = by_name["tika"]["ocr"]
    assert tika_ocr is not None
    assert tika_ocr["ocr_enabled"] is True
    assert tika_ocr["ocr_strategy"] == "auto"
    assert tika_ocr["ocr_language"] == "eng"
    assert tika_ocr["ocr_tesseract_path"] == \
        "C:\\Program Files\\Tesseract-OCR"
    # PDF: ocr_used stays unknown (None) — never claimed without
    # evidence. Tika's text passes the quality gate, so the failing
    # fallback extractors are SKIPPED under early stopping — they
    # never execute and produce no attempts at all.
    assert tika_ocr["ocr_used"] is None
    assert set(by_name) == {"tika"}
    assert record["skipped_extractors"] == ["pymupdf", "docling"]


def test_backfill_lifts_ocr_info_for_stored_records():
    existing = {"ocr_enabled": True, "ocr_used": True,
                "ocr_language": "eng", "ocr_strategy": "auto",
                "ocr_tesseract_path": "C:\\Tesseract",
                "ocr_evidence": "already recorded"}
    record = {
        "resume_id": "ocr-backfill-0001",
        "extracted_text": "",
        "attempts": [
            # Concurrent-pipeline record stored before the fix:
            # ocr=None but the Tika metadata is still there.
            {"extractor_name": "tika", "ocr": None,
             "metadata": dict(TIKA_OCR_METADATA)},
            # No OCR metadata at all — must honestly stay None.
            {"extractor_name": "muse_ai", "ocr": None, "metadata": {}},
            # Already recorded — must not be overwritten.
            {"extractor_name": "docling", "ocr": dict(existing),
             "metadata": dict(TIKA_OCR_METADATA)},
        ],
    }
    _normalize_attempt_fields(record)
    by_name = {a["extractor_name"]: a for a in record["attempts"]}
    assert by_name["tika"]["ocr"]["ocr_strategy"] == "auto"
    assert by_name["tika"]["ocr"]["ocr_enabled"] is True
    assert by_name["muse_ai"]["ocr"] is None
    assert by_name["docling"]["ocr"] == existing


def test_ocr_info_helper_contract():
    assert _ocr_info(None) is None
    assert _ocr_info({}) is None
    assert _ocr_info({"unrelated": 1}) is None
    info = _ocr_info(dict(TIKA_OCR_METADATA))
    assert set(info) == {"ocr_enabled", "ocr_used", "ocr_language",
                         "ocr_strategy", "ocr_tesseract_path",
                         "ocr_evidence"}


def test_frontend_renders_unknown_not_null():
    root = Path(__file__).resolve().parent.parent
    app_js = (root / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    assert '? "unknown"' in app_js
