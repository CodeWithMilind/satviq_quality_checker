"""Manual extractor comparison — run Tika / PyMuPDF / Docling
independently on a stored original from the Inspector.

Guarantees under test:
- each extractor's result is stored separately under
  record["extractor_results"], with its own normalized text and a
  real QualityChecker score;
- a failing extractor never stops the others and is recorded honestly;
- running one extractor does not disturb a previous manual result;
- the pipeline's attempts, selected extractor, final text and Resume
  JSON are NEVER changed, and no LLM is involved;
- the API endpoint validates extractor names, 404s unknown records,
  and 409s when the original upload is gone.
"""

import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import store
from api.main import app
from api.pipeline import run_manual_extractors
from api.text_normalization import normalize_text
from extractor.models import ExtractionResult

client = TestClient(app)

GOOD_TEXT = (
    "Jane Doe\njane.doe@example.com | +91 98765 43210 | Pune, India\n"
    "Professional Summary\nPython developer building AI and backend "
    "applications with FastAPI, data pipelines and LLM tools.\n"
    "Skills\nPython, FastAPI, REST APIs, SQL, Pandas, LangChain, Git.\n"
    "Work Experience\nData Scientist Intern at Example AI Solutions, "
    "May 2025 to November 2025. Built models and documented results.\n"
    "Education\nMaster of Computer Applications, Example University, "
    "2025 to 2027.\nProjects\nPDF Chat Assistant with RAG and FastAPI."
)


class FakeExtractor:
    def __init__(self, name, text=None, error=None):
        self.name = name
        self._text = text
        self._error = error

    def extract(self, path):
        if self._error:
            return ExtractionResult(
                extractor_name=self.name, success=False, text="",
                error=self._error, error_type="ExtractionFailedError",
                duration_ms=12.0)
        return ExtractionResult(
            extractor_name=self.name, success=True, text=self._text,
            duration_ms=25.0, page_count=2)


def _record():
    return {
        "resume_id": "manual-test-1",
        "extractor_used": "tika",
        "extracted_text": "ORIGINAL PIPELINE TEXT",
        "final_extracted_text": "ORIGINAL PIPELINE TEXT",
        "resume_json": {"full_name": "Jane Doe"},
        "attempts": [{"extractor_name": "tika", "success": True,
                      "text": "ORIGINAL PIPELINE TEXT"}],
        "extractor_results": {},
    }


def _fakes(**overrides):
    fakes = {
        "tika": FakeExtractor("tika", GOOD_TEXT),
        "pymupdf": FakeExtractor("pymupdf", GOOD_TEXT.replace("Jane", "Janet")),
        "docling": FakeExtractor("docling", GOOD_TEXT),
    }
    fakes.update(overrides)
    return fakes


def test_run_all_three_stores_separate_results(tmp_path):
    record = _record()
    doc = tmp_path / "resume.pdf"
    doc.write_bytes(b"%PDF-fake")
    results = run_manual_extractors(
        record, doc, ["tika", "pymupdf", "docling"], extractors=_fakes())
    assert set(results) == {"tika", "pymupdf", "docling"}
    for name, entry in results.items():
        assert entry["status"] == "success"
        assert entry["source"] == "manual"
        assert entry["normalized_text"] == normalize_text(entry["extracted_text"])
        assert entry["char_count"] == len(entry["extracted_text"])
        assert entry["duration_ms"] == 25.0
        assert entry["execution_time"] == 0.025
        # Real QualityChecker ran on the manual text.
        assert entry["quality"] is not None
        assert entry["quality_score"] is not None
    assert results["pymupdf"]["extracted_text"].startswith("Janet Doe")


def test_manual_runs_preserve_pipeline_result_and_ai_output(tmp_path):
    record = _record()
    before = copy.deepcopy(record)
    doc = tmp_path / "resume.pdf"
    doc.write_bytes(b"%PDF-fake")
    run_manual_extractors(record, doc, ["tika", "pymupdf", "docling"],
                          extractors=_fakes())
    assert record["attempts"] == before["attempts"]
    assert record["extracted_text"] == "ORIGINAL PIPELINE TEXT"
    assert record["final_extracted_text"] == "ORIGINAL PIPELINE TEXT"
    assert record["extractor_used"] == "tika"
    assert record["resume_json"] == {"full_name": "Jane Doe"}


def test_one_failure_is_recorded_and_others_still_run(tmp_path):
    record = _record()
    doc = tmp_path / "resume.pdf"
    doc.write_bytes(b"%PDF-fake")
    fakes = _fakes(docling=FakeExtractor("docling", error="Docling exploded"))
    results = run_manual_extractors(
        record, doc, ["tika", "pymupdf", "docling"], extractors=fakes)
    assert results["docling"]["status"] == "failed"
    assert "Docling exploded" in results["docling"]["error"]
    assert results["docling"]["quality_score"] is None
    assert results["tika"]["status"] == "success"
    assert results["pymupdf"]["status"] == "success"


def test_running_one_extractor_keeps_previous_manual_results(tmp_path):
    record = _record()
    doc = tmp_path / "resume.pdf"
    doc.write_bytes(b"%PDF-fake")
    run_manual_extractors(record, doc, ["tika"], extractors=_fakes())
    first_tika = copy.deepcopy(record["extractor_results"]["tika"])
    run_manual_extractors(record, doc, ["pymupdf"], extractors=_fakes())
    assert record["extractor_results"]["tika"] == first_tika
    assert set(record["extractor_results"]) == {"tika", "pymupdf"}


# --- API endpoint -----------------------------------------------------------

@pytest.fixture()
def stored_record(tmp_path):
    resume_id = "manual-api-1"
    stored_name = f"{resume_id}_resume.pdf"
    original = store.UPLOADS_DIR / stored_name
    store.ensure_dirs()
    original.write_bytes(b"%PDF-fake")
    record = _record()
    record["resume_id"] = resume_id
    record["_stored_name"] = stored_name
    store.save_record(record)
    yield resume_id
    store.delete_record(resume_id)


def test_endpoint_runs_and_persists(stored_record, monkeypatch):
    def fake_run(record, path, names, **kwargs):
        results = dict(record.get("extractor_results") or {})
        for name in names:
            results[name] = {"extractor": name, "source": "manual",
                             "status": "success", "extracted_text": "X",
                             "normalized_text": "X", "quality_score": 90.0,
                             "quality_status": "PASS", "quality": None,
                             "char_count": 1, "word_count": 1,
                             "execution_time": 0.01, "duration_ms": 10.0,
                             "page_count": 1, "error": None,
                             "error_type": None, "run_at": "2026-11-06T00:00:00+00:00"}
        record["extractor_results"] = results
        return results

    monkeypatch.setattr("api.main.run_manual_extractors", fake_run)
    response = client.post(
        f"/api/resumes/{stored_record}/extractors/run",
        json={"extractors": ["pymupdf"]})
    assert response.status_code == 200
    body = response.json()
    assert body["extractor_results"]["pymupdf"]["status"] == "success"
    # Persisted to the stored record; pipeline fields untouched.
    raw = store.load_raw_record(stored_record)
    assert raw["extractor_results"]["pymupdf"]["quality_score"] == 90.0
    assert raw["extracted_text"] == "ORIGINAL PIPELINE TEXT"
    assert raw["resume_json"] == {"full_name": "Jane Doe"}


def test_endpoint_rejects_unknown_extractor(stored_record):
    response = client.post(
        f"/api/resumes/{stored_record}/extractors/run",
        json={"extractors": ["ocr"]})
    assert response.status_code == 400


def test_endpoint_unknown_record_404():
    response = client.post(
        "/api/resumes/does-not-exist-1/extractors/run",
        json={"extractors": ["tika"]})
    assert response.status_code == 404


def test_endpoint_missing_original_409():
    resume_id = "manual-api-2"
    record = _record()
    record["resume_id"] = resume_id
    record["_stored_name"] = f"{resume_id}_gone.pdf"
    store.save_record(record)
    try:
        response = client.post(
            f"/api/resumes/{resume_id}/extractors/run",
            json={"extractors": ["tika"]})
        assert response.status_code == 409
    finally:
        store.delete_record(resume_id)


def test_frontend_wiring():
    root = Path(__file__).resolve().parent.parent
    app_js = (root / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    api_js = (root / "frontend" / "js" / "api.js").read_text(encoding="utf-8")
    assert 'id="extractorSelect"' in app_js
    assert "Run All Extractors" in app_js
    assert "data-run-extractor" in app_js
    assert "data-view-output" in app_js
    assert "Manual Comparison" in app_js
    assert "runExtractors" in api_js
    assert "/extractors/run" in api_js
