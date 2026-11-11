"""Contact Information Recovery — regression suite.

Headline case (real file, tests/fixtures/download_12.pdf =
"download (12).pdf", Soham Velhal): the PDF's text is stored as
vector outlines, so the text-layer extractors see almost nothing;
the selected Docling output carries the body but NOT the contact
header that is plainly on page 1:

    +91 7276968879 | sohamvelhal@gmail.com | Pune, Maharashtra 411068

The recovery stage must merge exactly those validated fields into
the selected normalized text sent to Muse, with provenance, and
the Completeness Checker re-run against the corrected text must
then detect Contact Information. Nothing may be invented, the
selected extractor and the original attempt texts stay untouched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.pipeline import process_resume_file_simple
from extractor.models import ExtractionResult
from muse_ai.schema import validate_resume_json
from pipeline import ExtractionPipeline, PipelineConfig
from pipeline.contact_recovery import (
    _first_page_text_layer,
    detect_present,
    extract_fields,
    recover_contact_information,
)

FIXTURE_PDF = Path(__file__).resolve().parent / "fixtures" / "download_12.pdf"
HEADER_LINE = ("+91 7276968879 | sohamvelhal@gmail.com | "
               "Pune, Maharashtra 411068")
EXPECTED = {"phone": "+91 7276968879",
            "email": "sohamvelhal@gmail.com",
            "location": "Pune, Maharashtra 411068"}

# Docling-style output for this resume: name + a garbled header
# line, then a complete body — but no usable contact fields.
DOCLING_TEXT = """Soham Velhal

9 m n  mmm   18

Professional Summary
Strategic and result-oriented Operations and Plant Head with 9.8 years of
progressive experience, growing from Production Engineer to leading plant
operations. Proven track record in scaling manufacturing performance,
implementing ERP systems, and enhancing customer satisfaction.

Skills
Production Planning, ERP Systems, Lean Manufacturing, ISO Compliance,
Quality Management, Team Leadership, Inventory Optimisation, and
Continuous Improvement are used daily across the plant.

Work Experience
Operations and Plant Head at Example Manufacturing from 2021 to 2025.
Led plant operations, scaled manufacturing performance, implemented ERP
systems, and improved customer satisfaction across all product lines.
Production Engineer at Example Industries from 2016 to 2021.
Supervised production lines, documented experiments, and wrote reports.

Education
Bachelor of Engineering, Example University, 2016.
Diploma in Mechanical Engineering, Example Polytechnic, 2013.
"""

TIKA_TEXT = "Soham Velhal  Operations and Plant Head  " + ("x" * 160)


# -- detection / validation -------------------------------------------

def test_detect_and_extract_header_line():
    assert detect_present(HEADER_LINE) == {
        "phone": True, "email": True, "location": True}
    assert extract_fields(HEADER_LINE, ["phone", "email", "location"]) \
        == EXPECTED


def test_detect_missing_on_docling_text():
    assert detect_present(DOCLING_TEXT) == {
        "phone": False, "email": False, "location": False}


def test_validation_rejects_bad_candidates():
    # Malformed email, a date range (not a phone), a bare PIN (not a
    # phone) — none may be recovered or counted as present.
    text = "Contact: not-an-email@x, 2021-01-05 to 2025-01-05, PIN 411068"
    found = extract_fields(text, ["phone", "email"])
    assert "email" not in found
    assert "phone" not in found


# -- the stage -----------------------------------------------------------

def _results(docling=DOCLING_TEXT, tika="", pymupdf=""):
    out = []
    for name, text in (("tika", tika), ("pymupdf", pymupdf),
                       ("docling", docling)):
        if text:
            out.append({"extractor": name, "status": "success",
                        "extracted_text": text, "text": text})
    return out


def test_not_needed_when_contact_present(tmp_path):
    text = "Soham Velhal\n" + HEADER_LINE + "\n\nProfessional Summary\nBody."
    corrected, block = recover_contact_information(
        normalized_text=text, selected_extractor="docling",
        results=_results(), file_path=tmp_path / "x.pdf")
    assert corrected == text
    assert block["status"] == "not_needed"
    assert block["sources_tried"] == []
    assert block["applied"] is False


def test_recovery_from_another_extractors_output(tmp_path):
    corrected, block = recover_contact_information(
        normalized_text=DOCLING_TEXT, selected_extractor="docling",
        results=_results(pymupdf="Soham Velhal\n" + HEADER_LINE),
        file_path=tmp_path / "x.pdf")
    assert block["status"] == "recovered"
    for field in ("phone", "email", "location"):
        assert block["recovered"][field]["value"] == EXPECTED[field]
        assert block["recovered"][field]["source"] == "PyMuPDF"
        assert block["recovered"][field]["method"] == "extractor_output"
    # Merged once, at the top, under a Contact Information heading;
    # the original text follows unchanged.
    assert corrected.startswith("Contact Information\n")
    for value in EXPECTED.values():
        assert corrected.count(value) == 1
    assert corrected.endswith(DOCLING_TEXT)
    assert block["pre_recovery_normalized_text"] == DOCLING_TEXT
    # Cheaper sources sufficed — no text-layer or OCR attempts.
    assert [s["method"] for s in block["sources_tried"]] == [
        "extractor_output"]


def test_recovery_from_first_page_text_layer(tmp_path):
    corrected, block = recover_contact_information(
        normalized_text=DOCLING_TEXT, selected_extractor="docling",
        results=_results(),
        file_path=tmp_path / "x.pdf",
        text_layer_reader=lambda path: "Soham Velhal\n" + HEADER_LINE,
        ocr_runner=lambda path: (_ for _ in ()).throw(
            AssertionError("OCR must not run when the text layer works")))
    assert block["status"] == "recovered"
    assert block["recovered"]["email"]["source"] == "PyMuPDF"
    assert block["recovered"]["email"]["method"] == \
        "first_page_text_layer"


def test_recovery_from_ocr_when_text_layer_empty(tmp_path):
    corrected, block = recover_contact_information(
        normalized_text=DOCLING_TEXT, selected_extractor="docling",
        results=_results(),
        file_path=tmp_path / "x.pdf",
        text_layer_reader=lambda path: "  \n  ",
        ocr_runner=lambda path: (HEADER_LINE, None))
    assert block["status"] == "recovered"
    for field in ("phone", "email", "location"):
        assert block["recovered"][field]["source"] == "OCR"
        assert block["recovered"][field]["value"] == EXPECTED[field]
    assert [s["method"] for s in block["sources_tried"]] == [
        "extractor_output", "first_page_text_layer", "ocr_first_page"]


def test_partial_recovery_invents_nothing(tmp_path):
    corrected, block = recover_contact_information(
        normalized_text=DOCLING_TEXT, selected_extractor="docling",
        results=_results(),
        file_path=tmp_path / "x.pdf",
        text_layer_reader=lambda path: "",
        ocr_runner=lambda path: ("Reach me at sohamvelhal@gmail.com", None))
    assert block["status"] == "partial"
    assert block["still_missing"] == ["phone", "location"]
    assert "sohamvelhal@gmail.com" in corrected
    assert "7276968879" not in corrected
    assert "411068" not in corrected


# -- the real file --------------------------------------------------------

def test_real_pdf_text_layer_has_no_contact():
    """download (12).pdf stores its text as vector outlines: the
    first-page text layer genuinely contains none of the contact
    fields — which is exactly why the stage falls through to OCR
    for this file."""
    layer = _first_page_text_layer(FIXTURE_PDF)
    assert detect_present(layer) == {
        "phone": False, "email": False, "location": False}


class _FakeExtractor:
    def __init__(self, name, text="", success=True):
        self.name = name
        self._text = text
        self._success = success

    def extract(self, path):
        if not self._success:
            return ExtractionResult(
                extractor_name=self.name, success=False, text="",
                error=f"{self.name} unavailable",
                error_type="ExtractionFailedError", duration_ms=5.0,
                page_count=None, page_texts=[], metadata={})
        return ExtractionResult(
            extractor_name=self.name, success=True, text=self._text,
            error=None, duration_ms=5.0, page_count=3,
            page_texts=[self._text] if self._text else [], metadata={})


class _FakeMuseService:
    configured = True
    model = "fake-model"
    provider_name = "Muse AI"
    configuration_error = None

    def __init__(self):
        self.calls: list[str] = []

    def generate_resume_json(self, text, hyperlinks=None):
        self.calls.append(text)
        parsed, _err = validate_resume_json(json.dumps({
            "full_name": "Soham Velhal",
            "email": "sohamvelhal@gmail.com",
            "phone": "+91 7276968879", "location": "Pune, Maharashtra",
            "summary": "Operations head", "skills": ["ERP"],
            "experience": [], "education": [], "certifications": [],
            "languages": [], "links": []}))
        assert parsed is not None
        return {"ok": True, "error": None, "error_type": None,
                "resume_json": parsed, "validation": "valid",
                "provider": "Muse AI", "model": "fake-model",
                "raw_text": "{}", "usage": {"prompt_tokens": 10,
                "completion_tokens": 5, "total_tokens": 15},
                "duration_ms": 12.0, "request_id": "req-test-1"}


def test_regression_download_12_full_flow(monkeypatch):
    """The real PDF + the real extractor outputs from the stored
    record; OCR (faked — no Tesseract in this environment) reads
    the page-1 header. Everything the spec requires must hold."""
    monkeypatch.setattr(
        "pipeline.contact_recovery._ocr_first_page_text",
        lambda path: (HEADER_LINE, None))
    service = _FakeMuseService()
    pipe = ExtractionPipeline(
        config=PipelineConfig(),
        extractors={
            "tika": _FakeExtractor("tika", TIKA_TEXT),
            "pymupdf": _FakeExtractor("pymupdf", success=False),
            "docling": _FakeExtractor("docling", DOCLING_TEXT),
        },
        llm_service=service)
    record = process_resume_file_simple(
        FIXTURE_PDF, resume_id="contact-recovery-0012",
        original_filename="download_12_.pdf", pipeline=pipe)

    # Docling still selected; its stored attempt text is untouched.
    assert record["selected_extractor"] == "docling"
    docling_attempt = next(a for a in record["attempts"]
                           if a["extractor_name"] == "docling")
    assert "7276968879" not in docling_attempt["extracted_text"]

    # The text Muse received contains all three expected details,
    # and the stored selected normalized text is byte-identical.
    assert len(service.calls) == 1
    sent = service.calls[0]
    for value in EXPECTED.values():
        assert value in sent
    assert record["text_normalization"]["normalized_text"] == sent

    # Provenance: recovered from OCR, all three fields.
    recovery = record["contact_recovery"]
    assert recovery["status"] == "recovered"
    assert recovery["missing_before"] == ["phone", "email", "location"]
    for field in ("phone", "email", "location"):
        assert recovery["recovered"][field]["value"] == EXPECTED[field]
        assert recovery["recovered"][field]["source"] == "OCR"
    assert recovery["pre_recovery_normalized_text"] is not None
    assert "7276968879" not in recovery["pre_recovery_normalized_text"]

    # Completeness re-run against the corrected text: Contact
    # Information flips from missing to detected, score improves,
    # and the selected attempt + record quality show the update.
    before = recovery["completeness_before"]
    after = recovery["completeness_after"]
    assert before["contact_information_detected"] is False
    assert "Contact Information" in before["missing_sections"]
    assert after["contact_information_detected"] is True
    assert "Contact Information" not in after["missing_sections"]
    assert after["score"] > before["score"]
    comp = docling_attempt["quality_checker_report"]["completeness"]
    assert "Contact Information" in comp["metrics"]["detected_sections"]

    # The trace tells the story.
    stages = {s["stage"]: s for s in record["pipeline_trace"]}
    assert stages["Contact Information Recovery"]["status"] == "PASS"
    assert "OCR" in stages["Contact Information Recovery"]["message"]


def test_frontend_shows_recovery_card():
    root = Path(__file__).resolve().parent.parent
    app_js = (root / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    assert "contactRecoveryHtml" in app_js
    assert "Contact Information Recovery" in app_js
