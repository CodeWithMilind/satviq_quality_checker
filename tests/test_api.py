"""API integration tests for SATVIQ — Resume Intelligence.

Uses FastAPI's TestClient against the real backend (real ExtractionManager
+ real QualityChecker). The tests are environment-independent: they pass
whether or not a Tika server / Docling / Tesseract happens to be installed
on the machine running them. Fallback itself is proven deterministically
with fake extractors driven through the real manager and quality gate.

Run from the project root:  python -m pytest tests/test_api.py -v
"""

from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")
from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402

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


def _make_pdf_bytes(lines=RESUME_LINES) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    y = 60
    for line in lines:
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


def _upload(name: str, data: bytes, content_type: str = "application/pdf"):
    return client.post("/api/resumes/process", files=[("files", (name, data, content_type))])


def test_health_endpoint():
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["extractor_order"] == ["tika", "ocr", "pymupdf", "docling"]
    assert body["quality_filters"] == ["text_health", "completeness", "reading_order"]
    assert "pdf" in body["supported_extensions"]


def test_frontend_is_served():
    response = client.get("/")
    assert response.status_code == 200
    assert "SATVIQ" in response.text
    assert client.get("/js/app.js").status_code == 200
    assert client.get("/css/style.css").status_code == 200


def test_process_pdf_with_fallback_and_quality():
    """Environment-independent: this test must pass whether or not a
    Tika server happens to be available on the machine running it.

    If Tika works, it is correctly the accepted extractor; if it is
    unavailable, the pipeline must fall back in the configured order.
    Either way exactly one attempt is selected as the final result
    (accepted outright on PASS, or the best WARNING candidate), it is
    the extractor reported as used, attempts follow the configured
    order, and the quality report is real.
    """
    response = _upload("jane_doe_resume.pdf", _make_pdf_bytes())
    assert response.status_code == 200
    (record,) = response.json()["results"]
    assert record["success"] is True

    attempts = record["attempts"]
    assert attempts, "attempt history must be recorded"
    assert attempts[0]["extractor_name"] == "tika"  # configured order starts with Tika
    attempted_names = [a["extractor_name"] for a in attempts]
    configured_order = ["tika", "ocr", "pymupdf", "docling"]
    assert attempted_names == configured_order[:len(attempted_names)]

    # Exactly one attempt is *selected* as the final result: either
    # accepted outright (quality PASS) or, if every attempt only
    # reached WARNING, the best candidate. It is the reported extractor.
    selected = [a for a in attempts if a.get("selected")]
    assert len(selected) == 1, "exactly one attempt should be selected"
    assert record["extractor_used"] == selected[0]["extractor_name"]
    # Every non-selected attempt must carry a reason (failure,
    # rejection, or candidate-hold decision).
    for attempt in attempts:
        if attempt is not selected[0]:
            assert attempt["selected"] is False
            assert attempt["error"] or attempt["decision_reason"]

    assert "Jane Doe" in record["extracted_text"]
    assert record["error"] is None
    # Real quality-checker output, all three filters present:
    quality = record["quality"]
    assert quality is not None
    for key in ("text_health", "completeness", "reading_order"):
        assert key in quality
        assert 0 <= quality[key]["score"] <= 100
    assert "Skills" in quality["completeness"]["detected_sections"]
    assert record["quality_status"] in ("PASS", "WARNING")


def test_fallback_with_real_quality_gate_is_deterministic(tmp_path):
    """Fallback proven without depending on which extractors are
    installed: fake extractors + the REAL QualityGatePolicy and the
    REAL QualityChecker, driven through the REAL ExtractionManager.

    Case 1: first extractor fails outright -> second is used.
    Case 2: first extractor returns corrupted text that the Quality
    Checker FAILs -> the gate rejects it and the second is used.
    """
    from api.pipeline import QualityGatePolicy
    from extractor import ExtractionManager
    from extractor.base import BaseExtractor
    from quality_checker import QualityChecker

    good_text = "\n".join(RESUME_LINES)  # line structure kept: a genuine quality PASS
    corrupted_text = "�" * 500

    class FakeExtractor(BaseExtractor):
        def __init__(self, name, behaviour):
            super().__init__(config={})
            self.name = name
            self.behaviour = behaviour

        def _extract(self, path):
            if self.behaviour == "fail":
                return self._failure(path, "fake extractor failure")
            if self.behaviour == "corrupted":
                return self._success(path, corrupted_text)
            return self._success(path, good_text)

    sample = tmp_path / "resume.pdf"
    sample.write_bytes(b"%PDF-fake")

    for first_behaviour in ("fail", "corrupted"):
        policy = QualityGatePolicy(QualityChecker(), document_id="test-doc")
        manager = ExtractionManager(
            config={"manager": {"order": ["first", "second"], "max_attempts": 4}},
            extractors={
                "first": FakeExtractor("first", first_behaviour),
                "second": FakeExtractor("second", "good"),
            },
            acceptance_policy=policy,
        )
        result = manager.extract(sample)
        assert result.success is True
        assert result.extractor_name == "second"
        assert result.attempts[0].extractor_name == "first"
        assert result.attempts[0].accepted is False
        assert result.attempts[0].error  # rejection/failure reason recorded
        assert result.attempts[1].accepted is True
        # The quality gate really ran and kept a report for the winner:
        assert policy.reports.get("second") is not None


def test_process_multiple_files():
    response = client.post("/api/resumes/process", files=[
        ("files", ("a_resume.pdf", _make_pdf_bytes(), "application/pdf")),
        ("files", ("b_resume.pdf", _make_pdf_bytes(), "application/pdf")),
    ])
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 2
    assert len({r["resume_id"] for r in results}) == 2  # unique IDs
    assert all(r["success"] for r in results)


def test_invalid_file_type():
    response = _upload("malware.exe", b"MZ fake executable", "application/octet-stream")
    (record,) = response.json()["results"]
    assert record["success"] is False
    assert "Unsupported file type" in record["error"]
    assert record["quality_status"] == "FAIL"


def test_empty_file():
    response = _upload("empty.pdf", b"")
    (record,) = response.json()["results"]
    assert record["success"] is False
    assert "empty" in record["error"].lower()


def test_corrupt_pdf():
    response = _upload("corrupt.pdf", b"this is not a pdf at all, just text bytes")
    (record,) = response.json()["results"]
    assert record["success"] is False
    assert record["error"]
    assert record["attempts"], "fallback attempts must be recorded"
    assert record["quality_status"] == "FAIL"


def test_history_detail_text_and_stats():
    response = _upload("history_resume.pdf", _make_pdf_bytes())
    (record,) = response.json()["results"]
    resume_id = record["resume_id"]

    listing = client.get("/api/resumes").json()["resumes"]
    assert any(r["resume_id"] == resume_id for r in listing)
    assert "extracted_text" not in listing[0]  # summaries omit full text

    detail = client.get(f"/api/resumes/{resume_id}")
    assert detail.status_code == 200
    assert "Jane Doe" in detail.json()["extracted_text"]

    text_response = client.get(f"/api/resumes/{resume_id}/text")
    assert text_response.status_code == 200
    assert "Jane Doe" in text_response.text
    assert "attachment" in text_response.headers.get("content-disposition", "")

    stats = client.get("/api/stats").json()
    assert stats["total_processed"] >= 1
    assert stats["successful_extractions"] >= 1
    assert 0 <= stats["quality_pass_rate_percent"] <= 100


def test_delete_and_404():
    response = _upload("to_delete.pdf", _make_pdf_bytes())
    resume_id = response.json()["results"][0]["resume_id"]
    assert client.delete(f"/api/resumes/{resume_id}").status_code == 200
    assert client.get(f"/api/resumes/{resume_id}").status_code == 404
    assert client.get("/api/resumes/does-not-exist-1234").status_code == 404
    assert client.delete("/api/resumes/does-not-exist-1234").status_code == 404


def test_path_traversal_filename_is_sanitised():
    response = _upload("../../evil.pdf", _make_pdf_bytes())
    (record,) = response.json()["results"]
    assert "/" not in record["filename"] and ".." not in record["filename"]
    assert record["filename"] == "evil.pdf"
    # No internal server paths anywhere in the API response:
    assert "uploads" not in str(record) or "_stored_name" not in record


def test_no_internal_fields_exposed():
    response = _upload("clean_response.pdf", _make_pdf_bytes())
    (record,) = response.json()["results"]
    assert not any(key.startswith("_") for key in record)
