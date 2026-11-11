"""Tests for the simple production pipeline (pipeline/).

All 16 spec scenarios plus below-threshold, record-compat and health.
No network, no paid API calls, no Tika/Java/Docling required -- fake
extractors and fake LLM callables/services are injected. The one
end-to-end test uses the real PyMuPDF extractor on the repo's
sample_resume.pdf (PyMuPDF is a hard dependency of the project).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from extractor.models import ExtractionResult
from pipeline import ExtractionPipeline, PipelineConfig, evaluate_extraction_quality, select_best_extractor
from pipeline.extractors import run_extractors
from pipeline.llm import LLMProcessor
from pipeline.normalization import normalize_extracted_text
from pipeline.selector import select_best_extractor as _select  # alias

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

# Slightly different good texts per extractor (unique markers for test 11)
GOOD_TIKA = GOOD_TEXT.replace("Jane Doe", "Jane Doe TIKAMARKER")
GOOD_PYMUPDF = GOOD_TEXT.replace("Jane Doe", "Jane Doe PYMUPDFMARKER")
GOOD_DOCLING = GOOD_TEXT.replace("Jane Doe", "Jane Doe DOCLINGMARKER")

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


class FakeExtractor:
    """Object with .extract returning an ExtractionResult."""

    def __init__(self, name, text="", success=True, error=None, delay=0.0):
        self.name = name
        self._text = text
        self._success = success
        self._error = error
        self._delay = delay

    def extract(self, path):
        if self._delay:
            time.sleep(self._delay)
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


def _extractors(tika, pymupdf_e, docling):
    return {"tika": tika, "pymupdf": pymupdf_e, "docling": docling}


def _good_extractors():
    return _extractors(
        FakeExtractor("tika", GOOD_TIKA),
        FakeExtractor("pymupdf", GOOD_PYMUPDF),
        FakeExtractor("docling", GOOD_DOCLING),
    )


def _valid_llm(text):
    return VALID_JSON_STR


@pytest.fixture()
def dummy_pdf(tmp_path):
    p = tmp_path / "resume.pdf"
    p.write_bytes(b"%PDF-fake-content")
    return p


# 1. All three extractors succeed
def test_01_all_three_extractors_succeed(dummy_pdf):
    pipe = ExtractionPipeline(config=PipelineConfig(), extractors=_good_extractors(), llm_callable=_valid_llm)
    result = pipe.run(dummy_pdf)
    assert set(result["extractor_reports"].keys()) == {"tika", "pymupdf", "docling"}
    assert all(r["status"] == "success" for r in result["extractor_reports"].values())
    assert result["status"] == "success"
    assert result["selected_extractor"] in ("tika", "pymupdf", "docling")
    assert result["resume_data"] is not None
    assert result["resume_data"]["full_name"] == "Jane Doe"


# 2. One extractor fails, two succeed
def test_02_one_extractor_fails_two_succeed(dummy_pdf):
    exts = _extractors(
        FakeExtractor("tika", success=False, error="tika boom"),
        FakeExtractor("pymupdf", GOOD_PYMUPDF),
        FakeExtractor("docling", GOOD_DOCLING),
    )
    pipe = ExtractionPipeline(config=PipelineConfig(), extractors=exts, llm_callable=_valid_llm)
    result = pipe.run(dummy_pdf)
    assert result["extractor_reports"]["tika"]["status"] == "failed"
    assert result["selected_extractor"] in ("pymupdf", "docling")
    assert result["status"] == "success"


# 3. Two fail, one succeeds
def test_03_two_fail_one_succeeds(dummy_pdf):
    exts = _extractors(
        FakeExtractor("tika", success=False, error="tika fail"),
        FakeExtractor("pymupdf", success=False, error="pymupdf fail"),
        FakeExtractor("docling", GOOD_DOCLING),
    )
    pipe = ExtractionPipeline(config=PipelineConfig(), extractors=exts, llm_callable=_valid_llm)
    result = pipe.run(dummy_pdf)
    assert result["selected_extractor"] == "docling"
    assert result["status"] == "success"


# 4. All three fail
def test_04_all_three_fail(dummy_pdf):
    exts = _extractors(
        FakeExtractor("tika", success=False, error="a"),
        FakeExtractor("pymupdf", success=False, error="b"),
        FakeExtractor("docling", success=False, error="c"),
    )
    called = []
    def llm(text):
        called.append(text)
        return VALID_JSON_STR
    pipe = ExtractionPipeline(config=PipelineConfig(), extractors=exts, llm_callable=llm)
    result = pipe.run(dummy_pdf)
    assert result["status"] == "extraction_failed"
    assert result["resume_data"] is None
    assert result["selected_extractor"] is None
    assert result["llm"]["status"] == "skipped"
    assert called == []


# 5. Empty extraction output
def test_05_empty_extraction_not_selectable(dummy_pdf):
    exts = _extractors(
        FakeExtractor("tika", ""),
        FakeExtractor("pymupdf", ""),
        FakeExtractor("docling", ""),
    )
    pipe = ExtractionPipeline(config=PipelineConfig(), extractors=exts, llm_callable=_valid_llm)
    result = pipe.run(dummy_pdf)
    assert result["status"] in ("extraction_failed", "below_threshold")
    assert result["selected_extractor"] is None
    assert result["resume_data"] is None
    for rep in result["extractor_reports"].values():
        assert rep["quality_score"] == 0.0


# 6. Corrupted text scores lower than clean
def test_06_corrupted_scores_lower_than_clean():
    clean = evaluate_extraction_quality({"extractor": "tika", "status": "success", "extracted_text": GOOD_TEXT, "error": None, "page_texts": [GOOD_TEXT]})
    corrupted_text = ("\ufffd" * 150) + " random \ufffd garbled \ufffd text " * 15 + GOOD_TEXT[:200]
    corrupted = evaluate_extraction_quality({"extractor": "pymupdf", "status": "success", "extracted_text": corrupted_text, "error": None, "page_texts": [corrupted_text]})
    assert corrupted["replacement_char_ratio"] > 0
    assert corrupted["quality_score"] < clean["quality_score"]


# 7. Duplicated content penalised
def test_07_duplicated_content_penalised():
    dup_text = "\n".join(["Python developer with FastAPI experience building backend services."] * 20)
    uniq_text = "\n".join([f"Python developer line {i} with FastAPI experience building service number {i} and project {i}." for i in range(20)])
    dup = evaluate_extraction_quality({"extractor": "tika", "status": "success", "extracted_text": dup_text, "error": None, "page_texts": [dup_text]})
    uniq = evaluate_extraction_quality({"extractor": "pymupdf", "status": "success", "extracted_text": uniq_text, "error": None, "page_texts": [uniq_text]})
    assert dup["duplication_ratio"] > uniq["duplication_ratio"]
    assert dup["quality_score"] < uniq["quality_score"]
    # Also: repeating the whole good resume must not beat the single copy
    single = evaluate_extraction_quality({"extractor": "tika", "status": "success", "extracted_text": GOOD_TEXT, "error": None, "page_texts": [GOOD_TEXT]})
    triple = evaluate_extraction_quality({"extractor": "tika", "status": "success", "extracted_text": GOOD_TEXT + "\n" + GOOD_TEXT + "\n" + GOOD_TEXT, "error": None, "page_texts": [GOOD_TEXT]})
    assert triple["quality_score"] < single["quality_score"]


# 8. Quality-based selection: best quality (not longest) wins
def test_08_quality_based_selection_not_longest(dummy_pdf):
    long_bad = "\ufffd garbled corrupted text \ufffd\n" * 80  # longest, duplicated + corrupted
    exts = _extractors(
        FakeExtractor("tika", long_bad),
        FakeExtractor("pymupdf", GOOD_TEXT),
        FakeExtractor("docling", success=False, error="docling fail"),
    )
    assert len(long_bad) > len(GOOD_TEXT)
    pipe = ExtractionPipeline(config=PipelineConfig(), extractors=exts, llm_callable=_valid_llm)
    result = pipe.run(dummy_pdf)
    assert result["selected_extractor"] == "pymupdf"
    assert result["extractor_reports"]["pymupdf"]["quality_score"] > result["extractor_reports"]["tika"]["quality_score"]


# 9. Tie-breaking
def test_09_tie_breaking_deterministic(dummy_pdf):
    exts = _extractors(
        FakeExtractor("tika", GOOD_TEXT),
        FakeExtractor("pymupdf", GOOD_TEXT),
        FakeExtractor("docling", GOOD_TEXT),
    )
    cfg = PipelineConfig(tie_break_order=["tika", "pymupdf", "docling"])
    pipe = ExtractionPipeline(config=cfg, extractors=exts, llm_callable=_valid_llm)
    result = pipe.run(dummy_pdf)
    scores = {k: v["quality_score"] for k, v in result["extractor_reports"].items()}
    assert len(set(scores.values())) == 1  # identical outputs -> identical scores
    assert result["selected_extractor"] == "tika"
    # Reverse tie order -> pymupdf wins when tika is not first? Use order pymupdf first
    cfg2 = PipelineConfig(tie_break_order=["pymupdf", "tika", "docling"])
    pipe2 = ExtractionPipeline(config=cfg2, extractors=_good_extractors.__wrapped__() if hasattr(_good_extractors, "__wrapped__") else _extractors(FakeExtractor("tika", GOOD_TEXT), FakeExtractor("pymupdf", GOOD_TEXT), FakeExtractor("docling", GOOD_TEXT)), llm_callable=_valid_llm)
    # Note _good_extractors uses distinct markers, so rebuild identical here:
    pipe2 = ExtractionPipeline(config=cfg2, extractors=_extractors(FakeExtractor("tika", GOOD_TEXT), FakeExtractor("pymupdf", GOOD_TEXT), FakeExtractor("docling", GOOD_TEXT)), llm_callable=_valid_llm)
    result2 = pipe2.run(dummy_pdf)
    assert result2["selected_extractor"] == "pymupdf"


# 10. Normalization preserves meaningful content
def test_10_normalization_preserves_content():
    messy = "Jane Doe\r\n\r\n\r\n\r\njane.doe@example.com   |   +91 98765 43210\r\nSkills\r\n- Python\r\n- FastAPI\r\nWork Experience\r\nData Scientist Intern from May 2025 to November 2025 at Example AI.\r\n\r\n\r\nEducation\r\nMaster of Computer Applications, 2025 to 2027."
    out = normalize_extracted_text(messy)
    assert out["status"] == "success"
    assert out["original_text"] == messy  # original preserved separately, unchanged
    norm = out["normalized_text"]
    assert "jane.doe@example.com" in norm
    assert "+91 98765 43210" in norm
    assert "May 2025" in norm
    assert "- Python" in norm
    assert "Skills" in norm
    assert "Education" in norm
    assert "\r" not in norm
    assert "\n\n\n" not in norm  # excessive blank lines removed


# 11. LLM receives only selected normalized text
def test_11_llm_receives_only_selected_normalized_text(dummy_pdf):
    # Make pymupdf the clear winner: tika/docling contain markers but score lower (tiny)
    # Instead: give all good, but selection is deterministic; check LLM input equals
    # normalized selected text and contains no non-selected markers.
    # Construct: tika good with marker, pymupdf tiny (below), docling tiny.
    # Then selected is tika, LLM must not contain pymupdf/docling markers.
    exts = _extractors(
        FakeExtractor("tika", GOOD_TIKA),
        FakeExtractor("pymupdf", "PYMUPDFMARKER only tiny text"),
        FakeExtractor("docling", "DOCLINGMARKER only tiny text"),
    )
    received = []
    def recording_llm(text):
        received.append(text)
        return VALID_JSON_STR
    pipe = ExtractionPipeline(config=PipelineConfig(), extractors=exts, llm_callable=recording_llm)
    result = pipe.run(dummy_pdf)
    assert result["selected_extractor"] == "tika"
    assert len(received) == 1
    assert received[0] == result["normalized_text"]
    assert "TIKAMARKER" in received[0]
    assert "PYMUPDFMARKER" not in received[0]
    assert "DOCLINGMARKER" not in received[0]


# 12. Valid JSON response
def test_12_valid_json_response(dummy_pdf):
    pipe = ExtractionPipeline(config=PipelineConfig(), extractors=_good_extractors(), llm_callable=_valid_llm)
    result = pipe.run(dummy_pdf)
    assert result["status"] == "success"
    assert result["llm"]["status"] == "success"
    assert result["llm"]["validation"] == "valid"
    assert result["resume_data"]["email"] == "jane.doe@example.com"
    # spec section 8 keys present
    for key in ("status", "selected_extractor", "selected_quality_score", "extractor_reports", "normalization", "llm", "resume_data"):
        assert key in result


# 13. Invalid JSON response
def test_13_invalid_json_response(dummy_pdf):
    def bad_llm(text):
        return "this is not json at all {"
    pipe = ExtractionPipeline(config=PipelineConfig(), extractors=_good_extractors(), llm_callable=bad_llm)
    result = pipe.run(dummy_pdf)
    assert result["status"] == "llm_failed"
    assert result["resume_data"] is None
    assert result["llm"]["status"] == "failed"
    assert result["llm"]["error"]
    assert result["llm"]["attempts"] == 1  # invalid JSON not retried


# 14. Schema validation failure
def test_14_schema_validation_failure(dummy_pdf):
    def schema_bad_llm(text):
        return json.dumps({"skills": 123})  # parses, but fails schema (verified)
    pipe = ExtractionPipeline(config=PipelineConfig(), extractors=_good_extractors(), llm_callable=schema_bad_llm)
    result = pipe.run(dummy_pdf)
    assert result["status"] == "llm_failed"
    assert result["resume_data"] is None
    assert result["llm"]["validation"] == "invalid"
    # Non-object JSON also fails schema validation
    def list_llm(text):
        return "[1,2,3]"
    pipe2 = ExtractionPipeline(config=PipelineConfig(), extractors=_good_extractors(), llm_callable=list_llm)
    result2 = pipe2.run(dummy_pdf)
    assert result2["status"] == "llm_failed"
    assert result2["llm"]["validation"] == "invalid"


# 15. LLM timeout: retried up to limit, and timeout-then-success works
def test_15_llm_timeout_retried_then_fails():
    class TimeoutService:
        configured = True
        model = "fake-model"
        provider_name = "Muse AI"
        configuration_error = None
        def __init__(self):
            self.calls = 0
        def generate_resume_json(self, text, hyperlinks=None):
            self.calls += 1
            return {"ok": False, "error": "Request timed out", "error_type": "TimeoutError",
                    "resume_json": None, "validation": None, "provider": "Muse AI",
                    "model": "fake-model", "raw_text": None, "usage": {}, "duration_ms": 10.0}
    svc = TimeoutService()
    cfg = PipelineConfig(llm_max_retries=2)
    proc = LLMProcessor(service=svc, config=cfg)
    out = proc.process("some normalized text")
    assert out["status"] == "failed"
    assert out["attempts"] == 3  # 1 + 2 retries
    assert svc.calls == 3
    assert out["error_type"] == "TimeoutError"


def test_15b_llm_timeout_then_success():
    class FlakyService:
        configured = True
        model = "fake-model"
        provider_name = "Muse AI"
        configuration_error = None
        def __init__(self):
            self.calls = 0
        def generate_resume_json(self, text, hyperlinks=None):
            self.calls += 1
            if self.calls == 1:
                return {"ok": False, "error": "Connection temporarily unavailable", "error_type": "ConnectionError",
                        "resume_json": None, "validation": None, "provider": "Muse AI",
                        "model": "fake-model", "raw_text": None, "usage": {}, "duration_ms": 5.0}
            parsed = json.loads(VALID_JSON_STR)
            # validate to return proper resume_json shape via schema
            from muse_ai.schema import validate_resume_json
            resume_json, _ = validate_resume_json(VALID_JSON_STR)
            return {"ok": True, "error": None, "error_type": None,
                    "resume_json": resume_json, "validation": "valid", "provider": "Muse AI",
                    "model": "fake-model", "raw_text": VALID_JSON_STR, "usage": {}, "duration_ms": 5.0}
    svc = FlakyService()
    proc = LLMProcessor(service=svc, config=PipelineConfig(llm_max_retries=2))
    out = proc.process("text")
    assert out["status"] == "success"
    assert out["attempts"] == 2
    assert svc.calls == 2


def test_15c_callable_timeout_retried():
    calls = {"n": 0}
    def timeout_callable(text):
        calls["n"] += 1
        raise TimeoutError("timed out")
    proc = LLMProcessor(llm_callable=timeout_callable, config=PipelineConfig(llm_max_retries=1))
    out = proc.process("text")
    assert out["status"] == "failed"
    assert out["attempts"] == 2
    assert calls["n"] == 2


# 16. End-to-end with real PyMuPDF on sample_resume.pdf
def test_16_end_to_end_real_pymupdf(dummy_pdf):
    pymupdf = pytest.importorskip("pymupdf")
    sample = Path(__file__).resolve().parent.parent / "sample_resume.pdf"
    if not sample.is_file():
        pytest.skip("sample_resume.pdf not present")
    from extractor.extractors.pymupdf import PyMuPDFExtractor
    exts = _extractors(
        FakeExtractor("tika", success=False, error="tika unavailable in test env"),
        PyMuPDFExtractor(config={}),
        FakeExtractor("docling", success=False, error="docling unavailable in test env"),
    )
    pipe = ExtractionPipeline(config=PipelineConfig(min_quality_threshold=10.0), extractors=exts, llm_callable=_valid_llm)
    result = pipe.run(sample)
    # Spec section 8 shape
    for key in ("status", "selected_extractor", "selected_quality_score", "extractor_reports", "normalization", "llm", "resume_data", "timings", "error"):
        assert key in result
    assert result["extractor_reports"]["pymupdf"]["word_count"] > 0
    # PyMuPDF is the only real successful extractor here; if the sample
    # has embedded text it should be selected. If not, extraction_failed
    # is honest -- but sample_resume.pdf in this repo does have text.
    if result["extractor_reports"]["pymupdf"]["status"] == "success":
        assert result["selected_extractor"] == "pymupdf"
        assert result["status"] == "success"
        assert result["resume_data"] is not None


# Extra: below threshold
def test_below_threshold_no_llm_call(dummy_pdf):
    exts = _extractors(
        FakeExtractor("tika", "tiny garbled \ufffd text"),
        FakeExtractor("pymupdf", "small \ufffd bad"),
        FakeExtractor("docling", "mini text here"),
    )
    called = []
    def llm(text):
        called.append(text)
        return VALID_JSON_STR
    pipe = ExtractionPipeline(config=PipelineConfig(min_quality_threshold=60.0), extractors=exts, llm_callable=llm)
    result = pipe.run(dummy_pdf)
    assert result["status"] == "below_threshold"
    assert result["selected_extractor"] is None
    assert result["resume_data"] is None
    assert result["llm"]["status"] == "skipped"
    assert called == []
    assert "threshold" in (result["selection_reason"] or "").lower()


# Extra: no extractor timeout — a slow extractor is waited out
def test_extractor_slow_is_awaited_no_timeout():
    slow = FakeExtractor("tika", GOOD_TEXT, delay=1.5)
    fast = FakeExtractor("pymupdf", GOOD_TEXT)
    results = run_extractors("ignored.pdf", extractors={"tika": slow, "pymupdf": fast, "docling": FakeExtractor("docling", GOOD_TEXT)})
    by = {r["extractor"]: r for r in results}
    # Timeouts were removed: the 1.5s extractor completes normally
    # instead of being cut off with a "timeout" status.
    assert by["tika"]["status"] == "success"
    assert by["tika"]["extracted_text"] == GOOD_TEXT
    assert by["pymupdf"]["status"] == "success"
    # canonical order preserved
    assert [r["extractor"] for r in results] == ["tika", "pymupdf", "docling"]


# Extra: callable extractor and dict-returning extractor support
def test_callable_extractor_support(tmp_path):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF")
    def my_callable(path):
        return {"text": GOOD_TEXT, "success": True, "metadata": {}, "page_count": 1, "page_texts": [GOOD_TEXT]}
    results = run_extractors(p, extractors={"tika": my_callable, "pymupdf": FakeExtractor("pymupdf", GOOD_TEXT), "docling": FakeExtractor("docling", GOOD_TEXT)})
    assert results[0]["status"] == "success"
    assert results[0]["extracted_text"] == GOOD_TEXT


# Extra: LLM not configured
def test_llm_not_configured(dummy_pdf):
    pipe = ExtractionPipeline(config=PipelineConfig(), extractors=_good_extractors(), llm_service=None, llm_callable=None, use_default_llm=False)
    result = pipe.run(dummy_pdf)
    assert result["status"] == "llm_not_configured"
    assert result["selected_extractor"] is not None
    assert result["resume_data"] is None
    assert result["llm"]["status"] == "not_configured"


# Extra: process_resume_file simple record compatibility
def test_process_resume_file_simple_record_compat(tmp_path):
    # Build a real PDF with PyMuPDF so the simple record has real text via fake extractors?
    # Use injected pipeline into process_resume_file_simple
    from api.pipeline import process_resume_file_simple
    sample = tmp_path / "resume.pdf"
    sample.write_bytes(b"%PDF-fake")
    pipe = ExtractionPipeline(config=PipelineConfig(), extractors=_good_extractors(), llm_callable=_valid_llm)
    record = process_resume_file_simple(sample, resume_id="test-simple-12345678", original_filename="resume.pdf", pipeline=pipe)
    assert record["pipeline_mode"] == "simple"
    assert record["resume_id"] == "test-simple-12345678"
    assert record["filename"] == "resume.pdf"
    assert record["file_type"] == "pdf"
    assert record["primary_extractor"] == "tika"
    assert record["attempt_count"] == 3
    assert len(record["attempts"]) == 3
    assert [a["extractor_name"] for a in record["attempts"]] == ["tika", "pymupdf", "docling"]
    selected_attempts = [a for a in record["attempts"] if a["is_final_selected"]]
    assert len(selected_attempts) == 1
    assert record["selected_extractor"] == selected_attempts[0]["extractor_name"]
    assert record["extractor_used"] == record["selected_extractor"]
    assert record["resume_json"] is not None
    assert record["resume_data"] == record["resume_json"]
    assert record["resume_json_source"] == "muse_ai"
    assert record["success"] is True
    assert record["quality_status"] in ("PASS", "WARNING")
    assert record["pipeline_trace"]
    assert record["extractor_reports"]
    assert "timings" in record


def test_process_resume_file_mode_simple_delegates(tmp_path, monkeypatch):
    # process_resume_file with pipeline_mode="simple" should produce a simple record
    # It will use real extractors + default Muse (not configured in test env likely).
    # We only assert it returns a record with pipeline_mode simple and does not crash.
    # To keep it environment-independent + fast, monkeypatch the simple function.
    import api.pipeline as ap
    called = {}
    def fake_simple(file_path, *, resume_id, original_filename, muse_service=None, pipeline=None):
        called["mode"] = "simple"
        return {"resume_id": resume_id, "filename": original_filename, "pipeline_mode": "simple", "success": False}
    monkeypatch.setattr(ap, "process_resume_file_simple", fake_simple)
    p = tmp_path / "r.pdf"
    p.write_bytes(b"%PDF")
    rec = ap.process_resume_file(p, resume_id="abc-12345678", original_filename="r.pdf", pipeline_mode="simple")
    assert rec["pipeline_mode"] == "simple"
    assert called["mode"] == "simple"


# Extra: health exposes simple_pipeline without changing existing keys
def test_health_simple_pipeline_key():
    from fastapi.testclient import TestClient
    from api.main import app
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["extractor_order"] == ["tika", "pymupdf", "docling", "ocr"]
    assert "simple_pipeline" in body
    assert body["simple_pipeline"]["extractors"] == ["tika", "pymupdf", "docling"]
    assert body["simple_pipeline"]["min_quality_threshold"] == 60.0
