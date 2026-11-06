"""Strict pipeline rule: normalize ONLY non-Muse extracted text.

- Text normalization receives raw text from the extractor chain
  (Tika / PyMuPDF / Docling / OCR) and nothing else. Muse AI output
  must never be passed to a normalizer.
- The three outputs stay separate: extracted_text (raw chain text),
  normalized_text (normalized chain text), ai_output / resume_json
  (Muse's response, changed only by JSON parsing and schema
  validation).
- Records stored by older builds, whose extracted_text was
  overwritten with the Muse vision response, are repaired on read.

Run: python -m pytest tests/test_normalize_only_non_muse.py -v
"""

import json
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")

from api import text_normalization  # noqa: E402
from api.pipeline import ensure_pipeline_trace, process_resume_file  # noqa: E402
from muse_ai.schema import validate_resume_json  # noqa: E402

CLAUDE_MARKER = "CLAUDE-RAW-OUTPUT-MARKER"
DOC_MARKER = "ZZQDOCMARKER"
CLAUDE_RESUME_JSON = {
    "full_name": "Priya Nair",
    "email": "priya@example.com",
    "skills": {"technical_skills": ["Python"], "soft_skills": [],
               "tools": [], "taxonomy_skills": [], "other_skills": []},
}
CLAUDE_RAW = json.dumps({**CLAUDE_RESUME_JSON, "marker": CLAUDE_MARKER})


class FakeMuseService:
    provider_name = "Muse AI"
    model = "muse-spark-1.3"
    configured = True
    vision_enabled = True
    configuration_error = None

    def __init__(self):
        self.config = type("Cfg", (), {"context_window_tokens": None})()
        self.vision_kwargs = None

    def vision_extract(self, path, **kwargs):
        self.vision_kwargs = kwargs
        return {"ok": True, "error": None, "error_type": None,
                "resume_json": CLAUDE_RESUME_JSON, "validation": "valid",
                "provider": "Muse AI", "model": "muse-spark-1.3",
                "raw_text": CLAUDE_RAW, "usage": {"total_tokens": 42},
                "duration_ms": 12.5, "timeout_seconds": 20.0,
                "input_type": "pdf", "page_count": 1,
                "pages_processed": 1, "pages_skipped": 0,
                "visual_provided": True, "tika_text_provided": True,
                "tika_text_chars": 10, "hyperlinks_provided": False,
                "hyperlink_count": 0}

    def generate_resume_json(self, text, **kwargs):  # pragma: no cover
        raise AssertionError(
            "JSON generation must not run after a successful vision stage")


def _qc_failing_pdf(tmp_path: Path) -> Path:
    """A real PDF whose text extracts fine but cannot PASS the
    Quality Checker (no resume sections), so the Muse vision stage
    runs while the chain still produced genuine extracted text."""
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((50, 60), f"{DOC_MARKER} blorp zing wobble",
                     fontsize=10)
    page.insert_text((50, 80), "quux flarn drivel snozzle", fontsize=10)
    path = tmp_path / "failing.pdf"
    path.write_bytes(document.tobytes())
    document.close()
    return path


@pytest.fixture()
def normalization_inputs(monkeypatch):
    """Record every text handed to the normalization entry point."""
    calls: list = []
    real_block = text_normalization.normalization_block

    def spy(original_text, **kwargs):
        calls.append(original_text)
        return real_block(original_text, **kwargs)

    monkeypatch.setattr(
        text_normalization, "normalization_block", spy)
    return calls


def test_normalizer_never_receives_muse_output(
        tmp_path, normalization_inputs):
    pdf = _qc_failing_pdf(tmp_path)
    record = process_resume_file(
        pdf, resume_id="norm-rule-1", original_filename="failing.pdf",
        muse_service=FakeMuseService())

    # The vision stage ran and its output was accepted.
    assert record["extractor_used"] == "muse_ai"
    assert record["resume_json"] == CLAUDE_RESUME_JSON

    # 1) The normalizer was called (with the chain's real text)…
    assert normalization_inputs, "normalization never ran"
    assert any(isinstance(text, str) and DOC_MARKER in text
               for text in normalization_inputs)
    # …and NEVER with Muse's output, in whole or in part.
    for text in normalization_inputs:
        assert text is None or CLAUDE_MARKER not in text

    # 2) The three outputs stayed separate. (This chain FAILs the
    #    Quality Checker outright, so no attempt is held and the
    #    record-level extracted_text is empty — the normalization
    #    block still correctly holds the chain attempt's raw text.)
    assert CLAUDE_MARKER not in (record["extracted_text"] or "")
    block = record["text_normalization"]
    assert block["source"] == "pymupdf"
    assert DOC_MARKER in block["original_extracted_text"]
    assert block["normalized_text"] is not None
    assert DOC_MARKER in block["normalized_text"]
    assert CLAUDE_MARKER not in block["normalized_text"]

    # 3) Muse's raw response is preserved — in its own attempt only.
    muse_attempt = [a for a in record["attempts"]
                    if a["extractor_name"] == "muse_ai"][0]
    assert muse_attempt["extracted_text"] == CLAUDE_RAW


def test_vision_receives_normalized_chain_text_only(tmp_path):
    service = FakeMuseService()
    pdf = _qc_failing_pdf(tmp_path)
    record = process_resume_file(
        pdf, resume_id="norm-rule-2", original_filename="failing.pdf",
        muse_service=service)
    tika_text = service.vision_kwargs["tika_text"]
    if tika_text is not None:
        assert CLAUDE_MARKER not in tika_text
    block = record["text_normalization"]
    assert block["normalized_text"] is None or \
        CLAUDE_MARKER not in block["normalized_text"]


def test_stored_vision_record_is_repaired_on_read():
    """Old storage wrote Muse's raw response into extracted_text.
    On read it must be restored to the chain's raw text; Muse's
    output remains only in its attempt and resume_json."""
    chain_text = "Jane Doe\nPython developer\njane@example.com"
    record = {
        "resume_id": "norm-rule-3",
        "extractor_used": "muse_ai", "final_extractor": "muse_ai",
        "extracted_text": CLAUDE_RAW, "final_extracted_text": CLAUDE_RAW,
        "attempts": [
            {"extractor_name": "tika", "extracted_text": chain_text,
             "text": chain_text, "success": True,
             "is_final_selected": False},
            {"extractor_name": "muse_ai", "extracted_text": CLAUDE_RAW,
             "text": CLAUDE_RAW, "success": True,
             "is_final_selected": True},
        ],
        "quality": None, "pipeline_trace": [{"type": "final"}],
        "text_normalization": None, "resume_json": CLAUDE_RESUME_JSON,
    }
    repaired = ensure_pipeline_trace(dict(record))
    assert repaired["extracted_text"] == chain_text
    assert repaired["final_extracted_text"] == chain_text
    block = repaired["text_normalization"]
    assert block["source"] == "tika"
    assert block["original_extracted_text"] == chain_text
    assert CLAUDE_MARKER not in (block["normalized_text"] or "")
    muse_attempt = repaired["attempts"][-1]
    assert muse_attempt["extracted_text"] == CLAUDE_RAW  # unchanged


# ---------------------------------------------------------------------
# Muse's response must survive parsing + validation unchanged
# ---------------------------------------------------------------------

FULL_PAYLOAD = {
    "full_name": "Priya Nair",
    "email": "priya@example.com",
    "phone": "+91 98765 43210",
    "location": "Pune, India",
    "summary": "Python developer.",
    "skills": {
        "technical_skills": ["Python", "Machine Learning"],
        "soft_skills": ["Communication"],
        "tools": ["Docker"],
        "taxonomy_skills": ["Python"],
        "other_skills": ["Mentoring"],
    },
    "experience": [{
        "title": "Data Scientist Intern", "company": "Example AI",
        "location": "Pune", "start_date": "May 2025",
        "end_date": "Nov 2025", "highlights": ["Built models."],
    }],
    "education": [{
        "degree": "MCA", "institution": "Example University",
        "location": "Pune", "start_date": "2025", "end_date": "2027",
    }],
    "certifications": ["Certified Data Scientist"],
    "languages": ["English", "Hindi"],
    "links": ["https://example.com/priya"],
}


def test_muse_response_unchanged_by_parsing_and_validation():
    raw = json.dumps(FULL_PAYLOAD)
    parsed, error = validate_resume_json(raw)
    assert error is None
    # Every valid field and value survives exactly — nothing
    # normalized, cleaned, reordered in content, or discarded.
    assert parsed == FULL_PAYLOAD


def test_service_preserves_raw_response_text():
    from muse_ai.config import MuseConfig
    from muse_ai.service import MuseService

    raw = json.dumps(FULL_PAYLOAD)

    class FakeClient:
        def chat_text(self, instructions, user_text):
            return {"text": raw, "usage": {}, "request_id": "r1",
                    "duration_ms": 1.0, "model": "muse-spark-1.3"}

    service = MuseService(MuseConfig(api_key="test"), client=FakeClient())
    outcome = service.generate_resume_json("some extracted text")
    assert outcome["raw_text"] == raw          # verbatim, not cleaned
    assert outcome["resume_json"] == FULL_PAYLOAD
