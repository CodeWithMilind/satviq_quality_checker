"""Tests for the per-resume pipeline trace & extractor transparency.

The trace scenarios are deterministic: fake extractors are driven
through the REAL ExtractionManager and the REAL QualityGatePolicy /
QualityChecker, and records are assembled by the same
``build_record_from_result`` the live API uses — so the trace asserted
here is the trace the frontend receives. API-level tests additionally
cover upload-twice, history correctness/refresh, delete-one and
delete-all against the real FastAPI app.

Run from the project root:  python -m pytest tests/test_pipeline_trace.py -v
"""

from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")
from fastapi.testclient import TestClient  # noqa: E402

from api import store  # noqa: E402
from api.main import app  # noqa: E402
from api.pipeline import (  # noqa: E402
    QualityGatePolicy,
    build_record_from_result,
    failed_record,
)
from extractor import ExtractionManager  # noqa: E402
from extractor.base import BaseExtractor  # noqa: E402
from quality_checker import QualityChecker  # noqa: E402

client = TestClient(app)

RESUME_LINES = [
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
]
GOOD_TEXT = "\n".join(RESUME_LINES)  # line structure kept: a genuine quality PASS
CORRUPTED_TEXT = "�" * 500
ORDER = ["tika", "pymupdf", "docling", "ocr"]


class FakeExtractor(BaseExtractor):
    """Deterministic extractor: 'fail', 'corrupted' or 'good'."""

    def __init__(self, name, behaviour):
        super().__init__(config={})
        self.name = name
        self.behaviour = behaviour

    def _extract(self, path):
        if self.behaviour == "fail":
            return self._failure(path, f"{self.name} fake failure")
        if self.behaviour == "corrupted":
            return self._success(path, CORRUPTED_TEXT)
        return self._success(path, GOOD_TEXT)


def _run_pipeline(tmp_path, behaviours, resume_id="trace-test-0001"):
    """Real manager + real quality gate + the API's record builder."""
    names = [name for name, _ in behaviours]
    policy = QualityGatePolicy(QualityChecker(), document_id=resume_id)
    manager = ExtractionManager(
        config={"manager": {"order": names, "max_attempts": 10}},
        extractors={name: FakeExtractor(name, b) for name, b in behaviours},
        acceptance_policy=policy,
    )
    sample = tmp_path / "resume.pdf"
    sample.write_bytes(b"%PDF-fake")
    result = manager.extract(sample)
    order = list(dict.fromkeys(manager.order))
    return build_record_from_result(
        resume_id=resume_id, original_filename="resume.pdf", path=sample,
        mime_type="application/pdf", file_size=sample.stat().st_size,
        result=result, policy=policy, order=order,
    )


def _extractor_stages(record):
    return [s for s in record["pipeline_trace"] if s["type"] == "extractor"]


def _quality_stage(record, extractor):
    return next(s for s in record["pipeline_trace"]
                if s["type"] == "quality" and s["extractor"] == extractor)


def _final_stage(record):
    return record["pipeline_trace"][-1]


def _make_pdf_bytes() -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    y = 60
    for line in RESUME_LINES:
        page.insert_text((50, y), line, fontsize=10)
        y += 18
    data = document.tobytes()
    document.close()
    return data


@pytest.fixture(autouse=True, scope="module")
def _clean_state():
    client.delete("/api/resumes")
    yield
    client.delete("/api/resumes")


# 1 — accepted by the first extractor immediately -------------------------

def test_trace_first_extractor_accepted_immediately(tmp_path):
    record = _run_pipeline(tmp_path, [(n, "good") for n in ORDER])
    assert record["success"] is True
    assert record["primary_extractor"] == "tika"
    assert record["extractor_used"] == "tika"
    assert record["attempt_count"] == 1
    assert record["fallback_occurred"] is False
    assert record["fallback_reason"] is None

    stages = {s["extractor"]: s for s in _extractor_stages(record)}
    assert stages["tika"]["status"] == "PASS"
    for name in ("pymupdf", "docling", "ocr"):
        assert stages[name]["status"] == "SKIPPED"
        assert "accepted" in stages[name]["message"]
    qc = _quality_stage(record, "tika")
    assert qc["status"] in ("PASS", "WARNING")
    assert qc["status"] == record["attempts"][0]["quality_status"]
    assert qc["quality"]["text_health"]["score"] >= 0
    assert _final_stage(record)["status"] == "PASS"
    assert _final_stage(record)["extractor"] == "tika"
    # Trace order follows the configured order.
    assert [s["extractor"] for s in _extractor_stages(record)] == ORDER


# 2 — first extractor fails, fallback succeeds ----------------------------

def test_trace_extractor_failure_then_fallback(tmp_path):
    record = _run_pipeline(
        tmp_path, [("tika", "fail"), ("pymupdf", "good"),
                   ("docling", "good"), ("ocr", "good")])
    assert record["success"] is True
    assert record["extractor_used"] == "pymupdf"
    assert record["attempt_count"] == 2
    assert record["fallback_occurred"] is True
    assert "tika fake failure" in record["fallback_reason"]

    stages = {s["extractor"]: s for s in _extractor_stages(record)}
    assert stages["tika"]["status"] == "FAIL"
    assert stages["tika"]["message"] == "tika fake failure"
    assert _quality_stage(record, "tika")["status"] == "SKIPPED"  # QC never ran
    assert stages["pymupdf"]["status"] == "PASS"
    assert stages["docling"]["status"] == "SKIPPED"
    assert stages["ocr"]["status"] == "SKIPPED"
    assert _final_stage(record)["extractor"] == "pymupdf"


# 3 — extraction succeeds but the Quality Checker rejects it --------------

def test_trace_quality_rejection_then_fallback(tmp_path):
    record = _run_pipeline(
        tmp_path, [("tika", "corrupted"), ("pymupdf", "good"),
                   ("docling", "good"), ("ocr", "good")])
    assert record["success"] is True
    assert record["extractor_used"] == "pymupdf"
    assert record["attempt_count"] == 2

    stages = {s["extractor"]: s for s in _extractor_stages(record)}
    assert stages["tika"]["status"] == "PASS"  # extraction itself worked
    qc = _quality_stage(record, "tika")
    assert qc["status"] == "REJECTED"
    assert "Quality Checker status FAIL" in qc["message"]
    # The rejection carries the real per-filter scores of that attempt.
    assert qc["quality"]["text_health"]["score"] < 50
    attempt = record["attempts"][0]
    assert attempt["accepted"] is False
    assert attempt["quality_status"] == "FAIL"
    assert attempt["quality"]["text_health"]["score"] < 50
    assert _quality_stage(record, "pymupdf")["status"] in ("PASS", "WARNING")


# 4 — multiple fallback attempts ------------------------------------------

def test_trace_multiple_fallback_attempts(tmp_path):
    record = _run_pipeline(
        tmp_path, [("tika", "fail"), ("pymupdf", "corrupted"),
                   ("docling", "good"), ("ocr", "good")])
    assert record["success"] is True
    assert record["extractor_used"] == "docling"
    assert record["attempt_count"] == 3
    assert record["fallback_occurred"] is True

    stages = _extractor_stages(record)
    assert [s["extractor"] for s in stages] == ORDER
    assert [s["status"] for s in stages] == ["FAIL", "PASS", "PASS", "SKIPPED"]
    assert _quality_stage(record, "tika")["status"] == "SKIPPED"
    assert _quality_stage(record, "pymupdf")["status"] == "REJECTED"
    assert _quality_stage(record, "docling")["status"] in ("PASS", "WARNING")
    # Every non-accepted attempt carries its real reason.
    for attempt in record["attempts"][:-1]:
        assert attempt["accepted"] is False
        assert attempt["error"]
    assert _final_stage(record)["extractor"] == "docling"


# 5 — the same resume uploaded twice stays two separate runs --------------

def test_same_resume_uploaded_twice_via_api():
    payload = _make_pdf_bytes()
    ids = []
    for _ in range(2):
        response = client.post(
            "/api/resumes/process",
            files=[("files", ("twice.pdf", payload, "application/pdf"))])
        assert response.status_code == 200
        (record,) = response.json()["results"]
        assert record["success"] is True
        assert record["pipeline_trace"], "each run must carry its own trace"
        assert _final_stage(record)["extractor"] == record["extractor_used"]
        ids.append(record["resume_id"])
    assert len(set(ids)) == 2, "re-uploading must not overwrite the earlier run"
    listing = client.get("/api/resumes").json()["resumes"]
    twice = [r for r in listing if r["filename"] == "twice.pdf"]
    assert {r["resume_id"] for r in twice} == set(ids)


# 6 + 7 — two resumes, different final extractors; history matches --------

def _save_record(tmp_path, behaviours, resume_id):
    record = _run_pipeline(tmp_path, behaviours, resume_id=resume_id)
    record["_stored_name"] = None
    store.save_record(record)
    return record


def test_history_shows_correct_final_extractor_per_run(tmp_path):
    first = _save_record(tmp_path, [(n, "good") for n in ORDER],
                         resume_id="tracehist-0001")
    second = _save_record(
        tmp_path, [("tika", "fail"), ("pymupdf", "good"),
                   ("docling", "good"), ("ocr", "good")],
        resume_id="tracehist-0002")
    try:
        assert first["extractor_used"] == "tika"
        assert second["extractor_used"] == "pymupdf"

        listing = {r["resume_id"]: r
                   for r in client.get("/api/resumes").json()["resumes"]}
        for saved in (first, second):
            summary = listing[saved["resume_id"]]
            # History summary fields match the actual accepted extractor.
            assert summary["extractor_used"] == saved["extractor_used"]
            assert summary["primary_extractor"] == "tika"
            assert summary["attempt_count"] == saved["attempt_count"]
            # …and so does the detail record's trace.
            detail = client.get(f"/api/resumes/{saved['resume_id']}").json()
            assert _final_stage(detail)["extractor"] == detail["extractor_used"]
            accepted = [a for a in detail["attempts"] if a["accepted"]]
            assert len(accepted) == 1
            assert accepted[0]["extractor_name"] == detail["extractor_used"]
    finally:
        client.delete("/api/resumes/tracehist-0001")
        client.delete("/api/resumes/tracehist-0002")


# 8 — refreshing history is consistent -------------------------------------

def test_history_refresh_is_consistent(tmp_path):
    saved = _save_record(tmp_path, [(n, "good") for n in ORDER],
                         resume_id="tracehist-0003")
    try:
        first = client.get("/api/resumes").json()["resumes"]
        second = client.get("/api/resumes").json()["resumes"]
        assert [r["resume_id"] for r in first] == [r["resume_id"] for r in second]
        entry = next(r for r in second if r["resume_id"] == "tracehist-0003")
        assert entry["extractor_used"] == saved["extractor_used"]
        assert entry["pipeline_trace"] == saved["pipeline_trace"]
    finally:
        client.delete("/api/resumes/tracehist-0003")


# 9 — deleting one record leaves the others intact --------------------------

def test_delete_one_record_keeps_others(tmp_path):
    _save_record(tmp_path, [(n, "good") for n in ORDER], resume_id="tracehist-0004")
    _save_record(tmp_path, [(n, "good") for n in ORDER], resume_id="tracehist-0005")
    try:
        assert client.delete("/api/resumes/tracehist-0004").status_code == 200
        assert client.get("/api/resumes/tracehist-0004").status_code == 404
        remaining = client.get("/api/resumes/tracehist-0005")
        assert remaining.status_code == 200
        assert remaining.json()["pipeline_trace"]
    finally:
        client.delete("/api/resumes/tracehist-0005")


# 10 — delete all ------------------------------------------------------------

def test_delete_all_clears_history():
    response = client.post(
        "/api/resumes/process",
        files=[("files", ("clear_me.pdf", _make_pdf_bytes(), "application/pdf"))])
    assert response.status_code == 200
    assert client.delete("/api/resumes").json()["deleted_count"] >= 1
    assert client.get("/api/resumes").json()["resumes"] == []


# Extra — real API uploads carry a faithful trace ---------------------------

def test_api_process_response_trace_matches_attempts():
    response = client.post(
        "/api/resumes/process",
        files=[("files", ("api_trace.pdf", _make_pdf_bytes(), "application/pdf"))])
    (record,) = response.json()["results"]
    assert record["success"] is True
    trace = record["pipeline_trace"]
    assert trace[0]["stage"] == "Resume Upload" and trace[0]["status"] == "PASS"
    assert trace[1]["stage"] == "MIME Type Detection"
    assert trace[1]["message"] == "application/pdf"
    assert record["attempt_count"] == len(record["attempts"])
    assert record["primary_extractor"] == record["attempts"][0]["extractor_name"]
    attempted = [a["extractor_name"] for a in record["attempts"]]
    assert attempted == ORDER[:len(attempted)]
    skipped = [s["extractor"] for s in _extractor_stages(record)
               if s["status"] == "SKIPPED"]
    assert skipped == ORDER[len(attempted):]
    assert _final_stage(record)["status"] == "PASS"
    assert _final_stage(record)["extractor"] == record["extractor_used"]


def test_failed_record_has_trace():
    record = failed_record(
        resume_id="trace-fail-0001", original_filename="bad.exe",
        file_type="exe", mime_type=None, file_size=10,
        error="Unsupported file type '.exe'.")
    assert record["attempt_count"] == 0
    assert record["primary_extractor"] == "tika"
    stages = _extractor_stages(record)
    assert stages and all(s["status"] == "SKIPPED" for s in stages)
    assert _final_stage(record)["status"] == "FAIL"


def test_legacy_record_without_trace_is_backfilled(tmp_path):
    """Records stored before traces existed (attempts + final quality
    only) get a trace derived from that stored data on read — with the
    real final extractor and skipped stages, and no invented scores."""
    record = _run_pipeline(tmp_path, [(n, "good") for n in ORDER],
                           resume_id="tracehist-0006")
    legacy = {k: v for k, v in record.items()
              if k not in ("pipeline_trace", "primary_extractor",
                           "attempt_count", "fallback_occurred",
                           "fallback_reason", "extractor_order")}
    for attempt in legacy["attempts"]:
        attempt.pop("quality_status", None)
        attempt.pop("quality", None)
    legacy["_stored_name"] = None
    store.save_record(legacy)
    try:
        detail = client.get("/api/resumes/tracehist-0006").json()
        assert detail["pipeline_trace"]
        assert detail["primary_extractor"] == "tika"
        assert detail["attempt_count"] == 1
        assert _final_stage(detail)["extractor"] == "tika"
        skipped = [s["extractor"] for s in _extractor_stages(detail)
                   if s["status"] == "SKIPPED"]
        assert skipped == ["pymupdf", "docling", "ocr"]
        qc = _quality_stage(detail, "tika")
        assert qc["quality"]["text_health"]["score"] == \
            record["quality"]["text_health"]["score"]
    finally:
        client.delete("/api/resumes/tracehist-0006")
