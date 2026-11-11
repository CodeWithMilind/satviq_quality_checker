"""Quality-based extraction with early stopping (sequential gate).

Spec scenarios — the pipeline must execute extractors ONE AT A TIME
in the order Tika -> PyMuPDF -> Docling -> OCR and STOP at the first
extractor whose text passes the existing Quality Checker gate
(quality_gate thresholds from extractor/config/extractor.yaml:
fail_below 50 / warn_below 75). Skipped extractors are never invoked
and never counted as attempts; the dashboard record must show the
real attempt count, the skipped stages and the final selected
extractor.

Texts are chosen against the REAL QualityChecker (verified):
- PASS_TEXT: Text Health 100 / Completeness 97 / Reading Order 100
- WARN_TEXT: prose with no resume sections -> Completeness low,
  Text Health / Reading Order high -> WARNING (held as candidate)
- FAIL_TEXT: replacement-char garbage -> Text Health 0 -> FAIL
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.pipeline import process_resume_file_simple
from extractor.models import ExtractionResult
from pipeline import ExtractionPipeline, PipelineConfig

PASS_TEXT = """Jane Doe
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
Master of Computer Applications at Example University, Pune, 2025 to 2027.
Bachelor of Computer Applications completed in 2025 with distinction.

Projects
Data workbench for uploading, analysing, and visualising datasets with FastAPI.
PDF Chat Assistant using RAG, LangChain, FAISS, and FastAPI for document questions.

Certifications
Certified Data Scientist, completed in 2025 with additional ML coursework.
- Python programming
- FastAPI development
- Machine learning
"""

WARN_TEXT = (
    "John Smith is a software professional with experience in many "
    "technologies and tools used across different companies over the "
    "years including python and data work and web development and "
    "testing and databases and cloud platforms and many other things "
    "in various teams and projects of different sizes and domains."
    * 3)

FAIL_TEXT = "\ufffd" * 500

VALID_JSON_STR = json.dumps({
    "full_name": "Jane Doe",
    "email": "jane.doe@example.com",
    "phone": "+91 98765 43210",
    "location": "Pune, India",
    "summary": "Python developer",
    "skills": ["Python", "FastAPI"],
    "experience": [],
    "education": [],
    "certifications": [],
    "languages": [],
    "links": [],
})


class RecordingExtractor:
    """Fake extractor that records every invocation."""

    def __init__(self, name, text="", success=True, error=None,
                 log: list | None = None):
        self.name = name
        self._text = text
        self._success = success
        self._error = error
        self.log = log if log is not None else []

    def extract(self, path):
        self.log.append(self.name)
        if not self._success:
            return ExtractionResult(
                extractor_name=self.name, success=False, text="",
                error=self._error or "fake extractor failure",
                error_type="ExtractionFailedError", duration_ms=5.0,
                page_count=None, page_texts=[], metadata={})
        return ExtractionResult(
            extractor_name=self.name, success=True, text=self._text,
            error=None, duration_ms=5.0, page_count=1,
            page_texts=[self._text] if self._text else [], metadata={})


def _valid_llm(text):
    return VALID_JSON_STR


@pytest.fixture()
def dummy_pdf(tmp_path):
    p = tmp_path / "resume.pdf"
    p.write_bytes(b"%PDF-fake-content")
    return p


def _run(extractors, dummy_pdf):
    pipe = ExtractionPipeline(config=PipelineConfig(),
                              extractors=extractors,
                              llm_callable=_valid_llm)
    return pipe.run(dummy_pdf, include_extractor_texts=True)


# --- Spec scenario 1: Tika PASS -> Tika only -------------------------
def test_tika_pass_runs_tika_only(dummy_pdf):
    log: list = []
    result = _run({
        "tika": RecordingExtractor("tika", PASS_TEXT, log=log),
        "pymupdf": RecordingExtractor("pymupdf", PASS_TEXT, log=log),
        "docling": RecordingExtractor("docling", PASS_TEXT, log=log),
        "ocr": RecordingExtractor("ocr", PASS_TEXT, log=log),
    }, dummy_pdf)
    assert log == ["tika"]
    assert result["selected_extractor"] == "tika"
    assert result["gate_decisions"]["tika"]["status"] == "PASS"
    assert result["gate_decisions"]["tika"]["decision"] == "accept"
    assert result["skipped_extractors"] == ["pymupdf", "docling", "ocr"]
    assert [r["extractor"] for r in result["extractor_results"]] == ["tika"]
    assert result["status"] == "success"


# --- Spec scenario 2: Tika FAIL, PyMuPDF PASS ------------------------
def test_tika_fail_pymupdf_pass(dummy_pdf):
    log: list = []
    result = _run({
        "tika": RecordingExtractor("tika", "", success=False,
                                   error="tika boom", log=log),
        "pymupdf": RecordingExtractor("pymupdf", PASS_TEXT, log=log),
        "docling": RecordingExtractor("docling", PASS_TEXT, log=log),
        "ocr": RecordingExtractor("ocr", PASS_TEXT, log=log),
    }, dummy_pdf)
    assert log == ["tika", "pymupdf"]
    assert result["selected_extractor"] == "pymupdf"
    assert result["skipped_extractors"] == ["docling", "ocr"]


# --- Spec scenario 3: Tika PASS regardless of Docling availability ---
def test_tika_pass_with_broken_docling_still_tika_only(dummy_pdf):
    log: list = []

    class ExplodingExtractor:
        name = "docling"

        def extract(self, path):  # pragma: no cover - must never run
            raise AssertionError("docling must never be invoked")

    result = _run({
        "tika": RecordingExtractor("tika", PASS_TEXT, log=log),
        "pymupdf": RecordingExtractor("pymupdf", PASS_TEXT, log=log),
        "docling": ExplodingExtractor(),
        "ocr": RecordingExtractor("ocr", PASS_TEXT, log=log),
    }, dummy_pdf)
    assert log == ["tika"]
    assert result["selected_extractor"] == "tika"


# --- Spec scenario 4: Tika FAIL, PyMuPDF FAIL, Docling PASS ----------
def test_tika_and_pymupdf_fail_docling_pass(dummy_pdf):
    log: list = []
    result = _run({
        "tika": RecordingExtractor("tika", FAIL_TEXT, log=log),
        "pymupdf": RecordingExtractor("pymupdf", "", success=False,
                                      error="pymupdf boom", log=log),
        "docling": RecordingExtractor("docling", PASS_TEXT, log=log),
        "ocr": RecordingExtractor("ocr", PASS_TEXT, log=log),
    }, dummy_pdf)
    assert log == ["tika", "pymupdf", "docling"]
    assert result["selected_extractor"] == "docling"
    assert result["gate_decisions"]["tika"]["status"] == "FAIL"
    assert result["skipped_extractors"] == ["ocr"]


# --- Spec scenario 5: all three fail -> OCR runs and can pass --------
def test_all_three_fail_then_ocr(dummy_pdf):
    log: list = []
    result = _run({
        "tika": RecordingExtractor("tika", FAIL_TEXT, log=log),
        "pymupdf": RecordingExtractor("pymupdf", FAIL_TEXT, log=log),
        "docling": RecordingExtractor("docling", "", success=False,
                                      error="docling boom", log=log),
        "ocr": RecordingExtractor("ocr", PASS_TEXT, log=log),
    }, dummy_pdf)
    assert log == ["tika", "pymupdf", "docling", "ocr"]
    assert result["selected_extractor"] == "ocr"
    assert result["skipped_extractors"] == []


# --- Spec scenario 6: every extractor fails -> FAILED/REJECTED -------
def test_all_extractors_fail_is_rejected(dummy_pdf):
    log: list = []
    result = _run({
        "tika": RecordingExtractor("tika", "", success=False, log=log),
        "pymupdf": RecordingExtractor("pymupdf", "", success=False, log=log),
        "docling": RecordingExtractor("docling", "", success=False, log=log),
        "ocr": RecordingExtractor("ocr", "", success=False, log=log),
    }, dummy_pdf)
    assert log == ["tika", "pymupdf", "docling", "ocr"]
    assert result["selected_extractor"] is None
    assert result["status"] in ("extraction_failed", "below_threshold")
    assert result["llm"]["status"] == "skipped"


# --- WARNING is a candidate, not a PASS: fallback continues ----------
def test_warning_continues_to_next_extractor(dummy_pdf):
    log: list = []
    result = _run({
        "tika": RecordingExtractor("tika", WARN_TEXT, log=log),
        "pymupdf": RecordingExtractor("pymupdf", PASS_TEXT, log=log),
        "docling": RecordingExtractor("docling", PASS_TEXT, log=log),
    }, dummy_pdf)
    assert log == ["tika", "pymupdf"]
    assert result["gate_decisions"]["tika"]["status"] == "WARNING"
    assert result["gate_decisions"]["tika"]["decision"] == "continue"
    assert result["selected_extractor"] == "pymupdf"


def test_warning_candidate_selected_when_nothing_passes(dummy_pdf):
    log: list = []
    result = _run({
        "tika": RecordingExtractor("tika", WARN_TEXT, log=log),
        "pymupdf": RecordingExtractor("pymupdf", "", success=False,
                                      error="pymupdf boom", log=log),
        "docling": RecordingExtractor("docling", FAIL_TEXT, log=log),
    }, dummy_pdf)
    assert log == ["tika", "pymupdf", "docling"]
    # Existing acceptance policy: the best executed candidate at/above
    # the configured minimum quality threshold is selected.
    assert result["selected_extractor"] == "tika"
    assert "best of the executed extractors" in (
        result["selection_reason"] or "")


# --- Dashboard/record accuracy via the API record builder ------------
def test_record_shows_skipped_and_real_attempt_count(dummy_pdf):
    log: list = []
    pipe = ExtractionPipeline(config=PipelineConfig(), extractors={
        "tika": RecordingExtractor("tika", PASS_TEXT, log=log),
        "pymupdf": RecordingExtractor("pymupdf", PASS_TEXT, log=log),
        "docling": RecordingExtractor("docling", PASS_TEXT, log=log),
        "ocr": RecordingExtractor("ocr", PASS_TEXT, log=log),
    }, llm_callable=_valid_llm)
    record = process_resume_file_simple(
        dummy_pdf, resume_id="early-stop-1",
        original_filename="resume.pdf", pipeline=pipe)
    assert log == ["tika"]
    # Attempts count only extractors that actually executed.
    assert record["attempt_count"] == 1
    assert [a["extractor_name"] for a in record["attempts"]] == ["tika"]
    assert record["extractor_used"] == "tika"
    assert record["final_extractor"] == "tika"
    assert record["attempts"][0]["is_final_selected"] is True
    assert record["attempts"][0]["quality_status"] == "PASS"
    assert record["quality_status"] == "PASS"
    assert record["fallback_occurred"] is False
    assert record["skipped_extractors"] == ["pymupdf", "docling", "ocr"]
    # Trace shows every non-executed extractor as SKIPPED, in order.
    stages = [(s["stage"], s["status"]) for s in record["pipeline_trace"]]
    extractor_stages = [(n, st) for n, st in stages
                        if n.endswith("Extractor")]
    assert extractor_stages == [
        ("Tika Extractor", "PASS"),
        ("PyMuPDF Extractor", "SKIPPED"),
        ("Docling Extractor", "SKIPPED"),
        ("OCR Extractor", "SKIPPED"),
    ]
    # Only the selected extractor's text was sent onward.
    assert record["text_normalization"]["source"] == "tika"
    assert record["resume_json"] is not None
