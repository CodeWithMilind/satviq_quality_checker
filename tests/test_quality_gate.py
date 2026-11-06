"""Tests for the QualityGatePolicy candidate behaviour.

The Quality Checker must actually drive fallback decisions:

- PASS    -> accept immediately, fallback stops.
- WARNING -> the result is held as a candidate and the next extractor
             is tried (when the warning is significant under the
             configured policy); if nothing better is accepted, the
             best candidate is returned at the end.
- FAIL    -> reject and try the next extractor.

All scenarios are deterministic: fake extractors return fixed texts
whose real Quality Checker scores were measured against the actual
filters (see the score comments), driven through the REAL
ExtractionManager and the REAL QualityGatePolicy/QualityChecker, with
records built by the same ``build_record_from_result`` the API uses.

Measured scores (text_health / completeness / reading_order):
- PASS_TEXT   100 / 100 / 100  -> PASS
- WARN_BETTER  75 /  58 / 92.5 -> WARNING (weakest 58, average 75.2)
- WARN_MID     75 /  55 / 100  -> WARNING (weakest 55, average 76.7)
- WARN_WORSE   75 /  30 / 100  -> WARNING (weakest 30, average 68.3)
- CORRUPT       0 /  30 /  70  -> FAIL (text_health < 50)

Run from the project root:  python -m pytest tests/test_quality_gate.py -v
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.pipeline import QualityGatePolicy, build_record_from_result
from extractor import ExtractionManager
from extractor.base import BaseExtractor
from quality_checker import QualityChecker

client = TestClient(app)

_PASS_LINES = [
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
PASS_TEXT = "\n".join(_PASS_LINES)
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
WARN_MID = (
    "Arjun Patil\n"
    "arjun.patil@example.com | +91 98220 99999 | Pune, India\n"
    "Contact Information\n"
    "Email: arjun.patil@example.com, Phone: +91 98220 99999, Location: Pune, India.\n"
    "He writes Python programs for local shops and tests them carefully "
    "each week. He keeps notes on defects and fixes them in small steps "
    "over time."
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


class FakeExtractor(BaseExtractor):
    def __init__(self, name, text):
        super().__init__(config={})
        self.name = name
        self.text = text

    def _extract(self, path):
        if self.text is FAIL:
            return self._failure(path, f"{self.name} fake failure")
        return self._success(path, self.text)


def _run(tmp_path, behaviours, gate_config=None, resume_id="gate-test-0001"):
    names = [name for name, _ in behaviours]
    policy = QualityGatePolicy(QualityChecker(), document_id=resume_id,
                               config=gate_config)
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
    return result, policy, record


def _attempt(record, name):
    return next(a for a in record["attempts"] if a["extractor_name"] == name)


def _quality_stage(record, name):
    return next(s for s in record["pipeline_trace"]
                if s["type"] == "quality" and s["extractor"] == name)


# PASS ----------------------------------------------------------------------

def test_pass_accepts_immediately_and_stops_fallback(tmp_path):
    result, policy, record = _run(
        tmp_path, [("tika", PASS_TEXT), ("pymupdf", PASS_TEXT), ("docling", PASS_TEXT)])
    assert result.success is True
    assert record["attempt_count"] == 1, "PASS must stop fallback processing"
    attempt = _attempt(record, "tika")
    assert attempt["quality_status"] == "PASS"
    assert attempt["decision"] == "accept"
    assert attempt["selected"] is True
    assert record["extractor_used"] == "tika"
    assert record["quality_status"] == "PASS"
    assert "Quality PASS" in record["selection_reason"]


# WARNING -------------------------------------------------------------------

def test_warning_is_held_as_candidate_and_next_extractor_tried(tmp_path):
    result, policy, record = _run(
        tmp_path, [("tika", WARN_BETTER), ("pymupdf", PASS_TEXT)])
    assert record["attempt_count"] == 2, "WARNING must not stop the pipeline"
    first = _attempt(record, "tika")
    assert first["quality_status"] == "WARNING"
    assert first["decision"] == "continue"
    assert first["selected"] is False
    assert "held as a candidate" in first["decision_reason"]
    assert [c["extractor_name"] for c in policy.candidates] == ["tika"]
    # The later PASS wins and is selected:
    assert record["extractor_used"] == "pymupdf"
    assert _attempt(record, "pymupdf")["selected"] is True
    assert record["quality_status"] == "PASS"
    # Trace stays in sync with the decision:
    stage = _quality_stage(record, "tika")
    assert stage["status"] == "WARNING"
    assert stage["decision"] == "continue"
    assert "held as a candidate" in stage["message"]


def test_warning_below_significance_threshold_is_accepted(tmp_path):
    """Configurable policy: WARN_BETTER's weakest score is 58, so with
    warning_continue_below=58 the warning is not significant and the
    result is accepted without trying the next extractor."""
    result, policy, record = _run(
        tmp_path, [("tika", WARN_BETTER), ("pymupdf", PASS_TEXT)],
        gate_config={"warning_continue_below": 58})
    assert record["attempt_count"] == 1
    attempt = _attempt(record, "tika")
    assert attempt["quality_status"] == "WARNING"
    assert attempt["decision"] == "accept"
    assert attempt["selected"] is True
    assert record["extractor_used"] == "tika"
    assert record["quality_status"] == "WARNING"
    assert policy.candidates == []


# FAIL ----------------------------------------------------------------------

def test_fail_is_rejected_and_next_extractor_tried(tmp_path):
    result, policy, record = _run(
        tmp_path, [("tika", CORRUPT_TEXT), ("pymupdf", PASS_TEXT)])
    assert record["attempt_count"] == 2
    first = _attempt(record, "tika")
    assert first["quality_status"] == "FAIL"
    assert first["decision"] == "reject"
    assert first["selected"] is False
    assert policy.candidates == [], "FAIL results are rejected, not held as candidates"
    stage = _quality_stage(record, "tika")
    assert stage["status"] == "REJECTED"
    assert "Quality Checker status FAIL" in stage["message"]
    assert record["extractor_used"] == "pymupdf"


def test_extraction_failure_then_pass(tmp_path):
    result, policy, record = _run(
        tmp_path, [("tika", FAIL), ("pymupdf", PASS_TEXT)])
    assert _attempt(record, "tika")["decision"] == "reject"
    assert _quality_stage(record, "tika")["status"] == "SKIPPED"
    assert record["extractor_used"] == "pymupdf"


# Candidate comparison --------------------------------------------------------

def test_best_candidate_wins_when_it_is_first(tmp_path):
    """The first candidate must NOT be overwritten by a worse later one."""
    result, policy, record = _run(
        tmp_path, [("tika", WARN_BETTER), ("pymupdf", WARN_WORSE)])
    assert record["success"] is True
    assert record["extractor_used"] == "tika"
    assert record["extracted_text"] == WARN_BETTER
    assert record["quality_status"] == "WARNING"
    assert _attempt(record, "tika")["selected"] is True
    assert _attempt(record, "pymupdf")["selected"] is False
    assert "Best candidate selected" in record["selection_reason"]
    assert "tika" in record["selection_reason"]
    assert "pymupdf" in record["selection_reason"]  # both were compared


def test_best_candidate_wins_when_it_is_last(tmp_path):
    result, policy, record = _run(
        tmp_path, [("tika", WARN_WORSE), ("pymupdf", WARN_BETTER)])
    assert record["extractor_used"] == "pymupdf"
    assert record["extracted_text"] == WARN_BETTER
    assert _attempt(record, "pymupdf")["selected"] is True


def test_three_candidates_compared_by_weakest_link_then_average(tmp_path):
    """WARN_MID has a higher average (76.7) than WARN_BETTER (75.2) but
    a lower weakest score (55 < 58); the configured policy ranks by
    weakest link first, so WARN_BETTER must win from any position."""
    result, policy, record = _run(
        tmp_path, [("tika", WARN_MID), ("pymupdf", WARN_WORSE),
                   ("docling", WARN_BETTER)])
    assert record["attempt_count"] == 3
    assert len(policy.candidates) == 3
    assert record["extractor_used"] == "docling"
    assert record["extracted_text"] == WARN_BETTER
    selected = [a["extractor_name"] for a in record["attempts"] if a["selected"]]
    assert selected == ["docling"]
    stage = _quality_stage(record, "docling")
    assert stage["selected"] is True
    assert "best candidate" in stage["message"]


def test_candidate_keeps_its_own_text_and_quality_report(tmp_path):
    result, policy, record = _run(
        tmp_path, [("tika", WARN_WORSE), ("pymupdf", WARN_BETTER)])
    expected = QualityChecker().check(
        WARN_BETTER, document_id="gate-test-0001",
        source_tool="pymupdf", attempt_id="attempt-2")
    quality = record["quality"]
    assert record["extracted_text"] == WARN_BETTER
    assert quality["text_health"]["score"] == expected.text_health.score
    assert quality["completeness"]["score"] == expected.completeness.score
    assert quality["reading_order"]["score"] == expected.reading_order.score
    attempt_quality = _attempt(record, "pymupdf")["quality"]
    assert attempt_quality["completeness"]["score"] == 58.0


def test_missing_sections_alone_do_not_fail_or_discard(tmp_path):
    """WARN_BETTER is WARNING only because sections are missing
    (Completeness 58); with no other extractor it must still be
    returned as the best candidate, with quality WARNING — not FAIL."""
    result, policy, record = _run(tmp_path, [("tika", WARN_BETTER)])
    assert record["success"] is True
    assert record["extractor_used"] == "tika"
    assert record["quality_status"] == "WARNING"
    assert record["quality"]["completeness"]["score"] == 58.0
    assert record["quality"]["completeness"]["missing_sections"]
    assert _attempt(record, "tika")["selected"] is True


def test_no_candidate_when_everything_fails(tmp_path):
    result, policy, record = _run(
        tmp_path, [("tika", FAIL), ("pymupdf", CORRUPT_TEXT)])
    assert record["success"] is False
    assert record["extractor_used"] is None
    assert not any(a["selected"] for a in record["attempts"])
    assert record["selection_reason"].startswith("No candidate was selected")
    assert policy.candidates == []


# Configuration surface -------------------------------------------------------

def test_health_exposes_quality_gate_config():
    body = client.get("/api/health").json()
    assert body["quality_gate"]["fail_below"] == 50
    assert body["quality_gate"]["warn_below"] == 75
    assert body["quality_gate"]["warning_continue_below"] == 75
