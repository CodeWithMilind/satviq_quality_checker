"""Per-attempt normalized texts for the Inspector's per-tool viewer.

The Extracted Text tab lets the user pick a tool (Tika / PyMuPDF /
Docling / OCR) and switch between that tool's raw extracted text and
its normalized text. The backend supplies ``attempt["normalized_text"]``
on read via ``ensure_pipeline_trace`` — computed from each attempt's
OWN stored text with the existing deterministic normalizer.

Strict rule preserved: Muse AI attempts are NEVER normalized (their
text is the model's raw response); they get ``normalized_text = None``.
Originals are never modified.
"""

from api.pipeline import _ensure_attempt_normalized_texts, ensure_pipeline_trace
from api.text_normalization import normalize_text

RAW = "Jane Doe\r\n\r\n\r\nPython   Developer\nSkills: Python, FastAPI"


def _record():
    return {
        "resume_id": "per-tool-1",
        "attempts": [
            {"extractor_name": "tika", "success": True,
             "text": RAW, "extracted_text": RAW},
            {"extractor_name": "pymupdf", "success": True,
             "text": "Jane Doe\nPyMuPDF text", "extracted_text": "Jane Doe\nPyMuPDF text"},
            {"extractor_name": "docling", "success": False,
             "text": "", "extracted_text": "", "error": "Docling unavailable"},
            {"extractor_name": "muse_ai", "success": True,
             "text": '{"full_name": "Jane Doe"}',
             "extracted_text": '{"full_name": "Jane Doe"}'},
        ],
        "pipeline_trace": [{"stage": "existing"}],
        "extractor_used": "tika",
        "success": True,
    }


def test_each_chain_attempt_gets_its_own_normalized_text():
    record = _record()
    _ensure_attempt_normalized_texts(record)
    by_name = {a["extractor_name"]: a for a in record["attempts"]}
    assert by_name["tika"]["normalized_text"] == normalize_text(RAW)
    assert by_name["tika"]["normalized_text"] != RAW  # CRLF/blank runs cleaned
    assert by_name["pymupdf"]["normalized_text"] == "Jane Doe\nPyMuPDF text"
    # A failed attempt with no text gets an empty normalized text, not an error.
    assert by_name["docling"]["normalized_text"] == ""


def test_muse_attempt_is_never_normalized():
    record = _record()
    _ensure_attempt_normalized_texts(record)
    muse = next(a for a in record["attempts"] if a["extractor_name"] == "muse_ai")
    assert muse["normalized_text"] is None
    # And its stored text is untouched.
    assert muse["text"] == '{"full_name": "Jane Doe"}'


def test_original_texts_are_never_modified():
    record = _record()
    _ensure_attempt_normalized_texts(record)
    tika = record["attempts"][0]
    assert tika["text"] == RAW and tika["extracted_text"] == RAW


def test_ensure_pipeline_trace_attaches_normalized_texts_on_read():
    record = _record()
    ensure_pipeline_trace(record)
    by_name = {a["extractor_name"]: a for a in record["attempts"]}
    assert by_name["tika"]["normalized_text"] == normalize_text(RAW)
    assert by_name["muse_ai"]["normalized_text"] is None


def test_frontend_has_per_tool_viewer():
    from pathlib import Path

    app_js = (Path(__file__).resolve().parent.parent
              / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    # One shared extractor selector above the tabs drives both the
    # Extracted and Normalized views (superseded the per-tab tool
    # picker from the first viewer iteration).
    assert 'id="extractorSelect"' in app_js
    assert "normalized_text" in app_js
    # Muse attempts are excluded from the tool picker in the UI too.
    assert 'a.extractor_name !== "muse_ai"' in app_js
