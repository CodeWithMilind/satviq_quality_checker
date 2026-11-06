"""Tests for full per-attempt output preservation.

The requirement under test: EVERY extractor attempt keeps its own
extracted text, quality report, error and fallback decision in the
record — a WARNING or FAIL attempt's text is never overwritten or
discarded just because a later extractor produced the final result.

Scenarios run through the REAL ExtractionManager + QualityGatePolicy
+ QualityChecker with fake extractors (same harness as
tests/test_quality_gate.py), records built by the same
``build_record_from_result`` the API uses, plus a storage round-trip
through api.store and an end-to-end API check.

Measured Quality Checker scores for the fixture texts:
- PASS_TEXT   100 / 100 / 100  -> PASS
- WARN_BETTER  75 /  58 / 92.5 -> WARNING
- WARN_WORSE   75 /  30 / 100  -> WARNING
- CORRUPT       0 /  30 /  70  -> FAIL (text_health < 50)

Run from the project root:  python -m pytest tests/test_attempt_outputs.py -v
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import store
from api.main import app
from api.pipeline import QualityGatePolicy, build_record_from_result
from extractor import ExtractionManager
from extractor.base import BaseExtractor
from quality_checker import QualityChecker

PASS_TEXT = "\n".join([
    "Jane Doe",
    "jane.doe@example.com | +91 98765 43210 | Pune, India",
    "Contact Information",
    "Email: jane.doe@example.com, Phone: +91 98765 43210, Location: Pune, India.",
    "Professional Summary",
    "Python developer building practical AI and backend applications for real users.",
    "Focused on FastAPI services, data pipelines, and LLM powered tools with tested code.",
    "Skills",
    "Python, FastAPI, REST APIs, SQL, MySQL, Pandas, NumPy, LangChain, and Git are used daily.",
    "Built backend services with authentication, validation, and structured logging.",
    "Work Experience",
    "Data Scientist Intern at Example AI Solutions from May 2025 to November 2025.",
    "Cleaned datasets, trained classification models, and documented experiments.",
    "Education",
    "Master of Computer Applications at Example University, Pune, 2025 to 2027.",
    "Bachelor of Computer Applications completed in 2025 with distinction.",
    "Projects",
    "Data workbench for uploading, analysing, and visualising datasets with FastAPI.",
    "PDF Chat Assistant using RAG, LangChain, FAISS, and FastAPI for document questions.",
    "Certifications",
    "Certified Data Scientist, completed in 2025 with additional ML coursework.",
])
WARN_BETTER = (
    "Priya Nair\n"
    "priya.nair@example.com | +91 98220 12345 | Pune, India\n"
    "Contact Information\n"
    "Email: priya.nair@example.com, Phone: +91 98220 12345, Location: Pune, India.\n"
    "Professional Summary\n"
    "Careful developer who writes Python programs and simple web pages for "
    "local clients. She tests her work, writes clear notes, and fixes "
    "defects methodically over time."
)
WARN_WORSE = (
    "John Smith lives in Pune and builds software tools for small teams "
    "in the city. He writes Python programs that read files and produce "
    "simple reports for local shops. In the morning he plans his tasks "
    "and reviews the code he wrote the day before. He keeps careful "
    "notes about bugs and fixes them one by one with tests. Colleagues "
    "describe him as patient and methodical when problems appear. He is "
    "looking for a junior role where he can keep learning every week."
)
CORRUPT_TEXT = "�" * 500
FAIL = None  # behaviour marker: extraction itself fails

REQUIRED_ATTEMPT_FIELDS = {
    "attempt_id", "resume_id", "extractor_name", "extractor_version",
    "attempt_number", "started_at", "completed_at", "duration_ms",
    "success", "extraction_status", "extracted_text",
    "extracted_text_length", "error_type", "error_message",
    "quality_checker_report", "quality_status", "fallback_decision",
    "fallback_reason", "is_final_selected",
}


class FakeExtractor(BaseExtractor):
    def __init__(self, name, text):
        super().__init__(config={})
        self.name = name
        self.text = text

    def _extract(self, path):
        if self.text is FAIL:
            return self._failure(path, f"{self.name} fake failure")
        return self._success(path, self.text)


def _run(tmp_path, behaviours, resume_id="attempt-test-0001"):
    names = [name for name, _ in behaviours]
    policy = QualityGatePolicy(QualityChecker(), document_id=resume_id)
    manager = ExtractionManager(
        config={"manager": {"order": names, "max_attempts": 10}},
        extractors={name: FakeExtractor(name, text) for name, text in behaviours},
        acceptance_policy=policy,
    )
    sample = tmp_path / "resume.pdf"
    sample.write_bytes(b"%PDF-fake")
    result = manager.extract(sample)
    record = build_record_from_result(
        resume_id=resume_id, original_filename="resume.pdf", path=sample,
        mime_type="application/pdf", file_size=sample.stat().st_size,
        result=result, policy=policy, order=names,
    )
    return result, record


def _attempt(record, name):
    return next(a for a in record["attempts"] if a["extractor_name"] == name)


# 1. WARNING (kept) -> extraction FAIL (kept) -> PASS (selected) ----------

def test_every_attempt_output_is_preserved(tmp_path):
    _, record = _run(tmp_path, [("tika", WARN_BETTER),
                                ("pymupdf", FAIL),
                                ("docling", PASS_TEXT)])
    assert record["attempt_count"] == 3
    assert record["extractor_used"] == "docling"
    assert record["fallback_occurred"] is True

    tika = _attempt(record, "tika")
    assert tika["extracted_text"] == WARN_BETTER, \
        "the WARNING attempt's own text must be preserved verbatim"
    assert tika["quality_status"] == "WARNING"
    assert tika["quality_checker_report"] is not None
    assert tika["quality_checker_report"]["completeness"]["score"] == 58
    assert tika["fallback_decision"] == "continue"
    assert tika["fallback_reason"]
    assert tika["is_final_selected"] is False

    pymupdf = _attempt(record, "pymupdf")
    assert pymupdf["success"] is False
    assert pymupdf["extracted_text"] == ""
    assert "fake failure" in pymupdf["error_message"]
    assert pymupdf["quality_checker_report"] is None
    assert pymupdf["is_final_selected"] is False

    docling = _attempt(record, "docling")
    assert docling["extracted_text"] == PASS_TEXT
    assert docling["quality_status"] == "PASS"
    assert docling["is_final_selected"] is True

    # Final aliases mirror the selected attempt's own output.
    assert record["final_extracted_text"] == PASS_TEXT
    assert record["final_quality_status"] == "PASS"

    # The full required schema is present on every attempt.
    for i, attempt in enumerate(record["attempts"], start=1):
        assert REQUIRED_ATTEMPT_FIELDS <= set(attempt), attempt.keys()
        assert attempt["attempt_number"] == i
        assert attempt["attempt_id"] == f"attempt-test-0001-attempt-{i}"
        assert attempt["resume_id"] == "attempt-test-0001"
        assert attempt["started_at"] and attempt["completed_at"]
        assert attempt["extracted_text_length"] == len(attempt["extracted_text"])


# 2. Best-candidate selection keeps every candidate's own text ------------

def test_best_candidate_keeps_all_candidate_texts(tmp_path):
    _, record = _run(tmp_path, [("tika", WARN_WORSE),
                                ("pymupdf", WARN_BETTER)])
    assert record["extractor_used"] == "pymupdf"  # better weakest filter
    assert record["final_extracted_text"] == WARN_BETTER
    assert _attempt(record, "tika")["extracted_text"] == WARN_WORSE
    assert _attempt(record, "pymupdf")["extracted_text"] == WARN_BETTER
    assert _attempt(record, "tika")["is_final_selected"] is False
    assert _attempt(record, "pymupdf")["is_final_selected"] is True


# 3. All tools fail — history still complete -------------------------------

def test_all_tools_fail_history_is_complete(tmp_path):
    _, record = _run(tmp_path, [("tika", CORRUPT_TEXT),
                                ("pymupdf", FAIL)])
    assert record["success"] is False
    assert record["extractor_used"] is None
    assert record["attempt_count"] == 2
    tika = _attempt(record, "tika")
    assert tika["extracted_text"] == CORRUPT_TEXT, \
        "even a FAIL-quality text is preserved for inspection"
    assert tika["quality_status"] == "FAIL"
    assert tika["is_final_selected"] is False
    assert _attempt(record, "pymupdf")["error_message"]


# 4. Storage round-trip: reopening a resume shows every output ------------

def test_attempt_outputs_survive_storage_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(store, "UPLOADS_DIR", tmp_path / "uploads")
    _, record = _run(tmp_path, [("tika", WARN_BETTER),
                                ("pymupdf", PASS_TEXT)],
                     resume_id="attempt-test-store")
    record["_stored_name"] = "attempt-test-store_resume.pdf"
    store.save_record(record)
    loaded = store.get_record("attempt-test-store")
    assert loaded is not None
    assert _attempt(loaded, "tika")["extracted_text"] == WARN_BETTER
    assert _attempt(loaded, "pymupdf")["extracted_text"] == PASS_TEXT
    assert _attempt(loaded, "pymupdf")["is_final_selected"] is True
    # And the raw JSON on disk carries the texts (no re-extraction needed).
    raw = json.loads((tmp_path / "results" / "attempt-test-store.json")
                     .read_text(encoding="utf-8"))
    assert _attempt(raw, "tika")["extracted_text"] == WARN_BETTER


# 5. Legacy record: selected attempt regains the stored final text --------

def test_legacy_record_backfills_selected_attempt_text():
    legacy = {
        "resume_id": "legacy-1", "success": True,
        "extractor_used": "docling", "quality_status": "WARNING",
        "extracted_text": WARN_BETTER,
        "attempts": [
            {"extractor_name": "tika", "success": True, "accepted": False,
             "error": "held as candidate", "quality_status": "WARNING",
             "decision": "continue", "selected": False},
            {"extractor_name": "docling", "success": True, "accepted": True,
             "error": None, "quality_status": "WARNING",
             "decision": "continue", "selected": True},
        ],
        "pipeline_trace": [{"stage": "x"}],  # trace exists -> kept as-is
    }
    from api.pipeline import ensure_pipeline_trace
    out = ensure_pipeline_trace(legacy)
    docling = _attempt(out, "docling")
    assert docling["extracted_text"] == WARN_BETTER
    assert docling["is_final_selected"] is True
    tika = _attempt(out, "tika")
    assert tika["extracted_text"] == "", \
        "a non-selected legacy attempt must not be given another tool's text"
    assert REQUIRED_ATTEMPT_FIELDS <= set(tika)


# 5b. OCR facts from the extractor metadata reach the attempt record -----

class _OcrFakeExtractor(FakeExtractor):
    """A Tika-shaped fake: success with OCR metadata on the result."""

    def _extract(self, path):
        return self._result(
            path, success=True, text=self.text,
            metadata={"ocr_enabled": True, "ocr_used": True,
                      "ocr_language": "eng", "ocr_strategy": "auto",
                      "ocr_tesseract_path": "/opt/tesseract",
                      "ocr_evidence": "Raster image input."})


def test_attempt_record_carries_ocr_info(tmp_path):
    names = ["tika"]
    policy = QualityGatePolicy(QualityChecker(), document_id="ocr-map-1")
    manager = ExtractionManager(
        config={"manager": {"order": names, "max_attempts": 10}},
        extractors={"tika": _OcrFakeExtractor("tika", PASS_TEXT)},
        acceptance_policy=policy)
    sample = tmp_path / "resume.pdf"
    sample.write_bytes(b"%PDF-fake")
    result = manager.extract(sample)
    record = build_record_from_result(
        resume_id="ocr-map-1", original_filename="resume.pdf", path=sample,
        mime_type="application/pdf", file_size=sample.stat().st_size,
        result=result, policy=policy, order=names)
    ocr = record["attempts"][0]["ocr"]
    assert ocr["ocr_enabled"] is True
    assert ocr["ocr_used"] is True
    assert ocr["ocr_language"] == "eng"
    assert ocr["ocr_tesseract_path"] == "/opt/tesseract"
    # Non-Tika attempts have no OCR block (null, never fabricated).
    _, record2 = _run(tmp_path, [("pymupdf", PASS_TEXT)])
    assert record2["attempts"][0]["ocr"] is None


# 6. API returns the complete attempt history ------------------------------

def test_api_returns_complete_attempt_history():
    client = TestClient(app)
    sample = Path(__file__).resolve().parent.parent / "sample_resume.pdf"
    with sample.open("rb") as handle:
        response = client.post(
            "/api/resumes/process",
            files={"files": ("sample_resume.pdf", handle, "application/pdf")})
    assert response.status_code == 200
    record = response.json()["results"][0]
    try:
        assert record["attempt_count"] >= 1
        assert record["final_extracted_text"] == record["extracted_text"]
        assert record["final_quality_status"] == record["quality_status"]
        for attempt in record["attempts"]:
            assert REQUIRED_ATTEMPT_FIELDS <= set(attempt), attempt.keys()
        selected = [a for a in record["attempts"] if a["is_final_selected"]]
        if record["success"]:
            assert len(selected) == 1
            assert selected[0]["extracted_text"] == record["final_extracted_text"]
            assert selected[0]["extracted_text_length"] > 0
        # The detail endpoint serves the same preserved history.
        detail = client.get(f"/api/resumes/{record['resume_id']}")
        assert detail.status_code == 200
        assert detail.json()["attempts"] == record["attempts"]
    finally:
        client.delete(f"/api/resumes/{record['resume_id']}")


# 7. Frontend: per-attempt sections render every output (Node, real JS) ---

_NODE_DRIVER = r"""
const fs = require("fs");
const vm = require("vm");
process.on("unhandledRejection", () => {});
const DATA = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
function makeEl() {
  return { innerHTML: "", textContent: "", className: "", hidden: false, disabled: false,
    href: "#", value: "", files: [], dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, scrollIntoView() {}, setAttribute() {},
    getAttribute() { return null; }, querySelector() { return null; },
    querySelectorAll() { return []; }, closest() { return null; } };
}
const elements = {};
const healthStub = { extractor_order: ["tika", "pymupdf", "docling", "ocr"],
  supported_extensions: ["pdf"], max_upload_mb: 5,
  ai: { provider: "Muse AI", model: "muse-spark-1.3", configured: false } };
const ctx = vm.createContext({
  document: { getElementById: (id) => (elements[id] ??= makeEl()),
    querySelector: () => null, querySelectorAll: () => [], addEventListener() {},
    body: makeEl(), createElement: () => makeEl() },
  window: { addEventListener() {} },
  location: { hash: "#/process" },
  Api: { health: async () => healthStub,
    downloadOriginalUrl: (id) => "/api/resumes/" + id + "/file",
    downloadTextUrl: (id) => "/api/resumes/" + id + "/text" },
  fetch: async () => ({ ok: true, json: async () => ({}) }), console,
  __D__: DATA, __EL__: elements,
  alert: () => {}, confirm: () => true, navigator: {},
  URLSearchParams, URL, setTimeout, clearTimeout,
});
const code = fs.readFileSync(process.argv[2], "utf8") + `
;(() => {
  const assert = (cond, msg) => { if (!cond) throw new Error(msg); };

  const record = __D__.record;
  const cards = record.attempts.map((a, i) => attemptCardHtml(a, i)).join("");
  assert(cards.includes("TIKA-WARN-TEXT-MARKER"), "tika attempt text missing");
  assert(cards.includes("DOCLING-PASS-TEXT-MARKER"), "docling attempt text missing");
  assert(cards.includes("PYMUPDF-FAIL-MARKER"), "pymupdf failure reason missing");
  assert(cards.includes("Apache Tika"), "engine label missing");
  assert(cards.includes("Accepted"), "selected attempt must read Accepted");
  assert(cards.includes("Failed"), "failed attempt must read Failed");
  assert(cards.includes("ocr_used"), "OCR status missing from attempt details");
  assert(cards.includes("eng"), "OCR language missing from attempt details");
  assert(cards.includes("Text Health 75"), "attempt quality scores missing");
  assert(cards.includes("Completeness 58"), "attempt completeness score missing");
  assert(cards.includes("Reading Order 92.5"), "attempt reading-order score missing");
  const card = resultCardHtml(record);
  assert(card.includes("N/A"), "missing quality must render N/A, never 0");
  console.log("ATTEMPT SECTIONS FRONTEND OK");
})();
`;
vm.runInContext(code, ctx);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_frontend_attempt_sections_render_every_output(tmp_path):
    record = {
        "resume_id": "front-1",
        "pipeline_trace": [
            {"stage": "Tika Extractor", "type": "extractor", "extractor": "tika",
             "status": "PASS", "message": "ok"},
            {"stage": "Final Extraction Result", "type": "final",
             "status": "PASS", "message": "done"},
        ],
        "selection_reason": "Docling accepted.",
        "attempts": [
            {"attempt_number": 1, "extractor_name": "tika",
             "extractor_version": "4.2.0", "success": True,
             "ocr": {"ocr_enabled": True, "ocr_used": True,
                     "ocr_language": "eng", "ocr_strategy": "auto",
                     "ocr_tesseract_path": "C:\\Program Files\\Tesseract-OCR",
                     "ocr_evidence": "Raster image input."},
             "quality_status": "WARNING", "fallback_decision": "continue",
             "fallback_reason": "held as candidate",
             "extracted_text": "TIKA-WARN-TEXT-MARKER",
             "extracted_text_length": 22, "duration_ms": 10.0,
             "quality_checker_report": {
                 "text_health": {"score": 75, "flags": []},
                 "completeness": {"score": 58, "flags": [],
                                  "detected_sections": ["Contact Information"],
                                  "missing_sections": ["Skills"]},
                 "reading_order": {"score": 92.5, "flags": []}},
             "is_final_selected": False, "error_message": None},
            {"attempt_number": 2, "extractor_name": "pymupdf",
             "extractor_version": None, "success": False,
             "quality_status": None, "fallback_decision": "reject",
             "fallback_reason": "PYMUPDF-FAIL-MARKER",
             "extracted_text": "", "extracted_text_length": 0,
             "duration_ms": 5.0, "quality_checker_report": None,
             "is_final_selected": False,
             "error_message": "PYMUPDF-FAIL-MARKER",
             "error_type": "ExtractionFailedError"},
            {"attempt_number": 3, "extractor_name": "docling",
             "extractor_version": None, "success": True,
             "quality_status": "PASS", "fallback_decision": "accept",
             "fallback_reason": "accepted",
             "extracted_text": "DOCLING-PASS-TEXT-MARKER",
             "extracted_text_length": 24, "duration_ms": 7.0,
             "quality_checker_report": None,
             "is_final_selected": True, "error_message": None},
        ],
    }
    data_path = tmp_path / "record.json"
    data_path.write_text(json.dumps({"record": record}))
    driver_path = tmp_path / "driver.js"
    driver_path.write_text(_NODE_DRIVER)
    app_js = Path(__file__).resolve().parent.parent / "frontend" / "js" / "app.js"
    completed = subprocess.run(
        ["node", str(driver_path), str(app_js), str(data_path)],
        capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert "ATTEMPT SECTIONS FRONTEND OK" in completed.stdout
