"""Text normalization — deterministic rules, preservation of facts,
and the guarantee that Muse receives the normalized text while
the original selected-extractor output is never modified.
"""

from pathlib import Path

from api.pipeline import _apply_muse_stages
from api.text_normalization import (estimate_tokens, normalization_block,
                                    normalize_text)

MESSY = (
    "  Priya   Nair  \r\n"
    "  Python Developer   |   Bengaluru  \r\n"
    "\r\n\r\n\r\n"
    "SKILLS\r\n"
    "•   Python,   FastAPI,  SQL\r\n"
    "• LangChain  •  RAG\r\n"
    "\r\n"
    "Contact:  priya@example.com   |  +91  98765 43210\r\n"
    "Portfolio: https://example.com/priya%20nair?x=1\r\n"
    "Experience:  Jan 2023 – Present\r"
)


def test_multiple_spaces_and_tabs_collapse():
    assert normalize_text("a   b\t\tc  d") == "a b c d"


def test_excessive_blank_lines_become_one():
    assert normalize_text("a\n\n\n\n\nb") == "a\n\nb"
    assert normalize_text("a\n\nb") == "a\n\nb"  # one blank line kept


def test_windows_and_unix_line_endings_unified():
    assert normalize_text("a\r\nb\rc\nd") == "a\nb\nc\nd"


def test_leading_and_trailing_whitespace_removed():
    assert normalize_text("   hello world   \n   ") == "hello world"
    assert normalize_text("\n\n  x  \n\n") == "x"


def test_headings_and_bullets_preserved():
    out = normalize_text(MESSY)
    assert "SKILLS" in out
    assert "• Python, FastAPI, SQL" in out
    assert "• LangChain • RAG" in out
    assert "\n\nSKILLS\n" in out  # section separation preserved


def test_emails_urls_phones_dates_preserved():
    out = normalize_text(MESSY)
    assert "priya@example.com" in out
    assert "https://example.com/priya%20nair?x=1" in out
    assert "+91 98765 43210" in out
    assert "Jan 2023 – Present" in out
    assert "Priya Nair" in out


def test_empty_and_whitespace_only_input():
    assert normalize_text("") == ""
    assert normalize_text("   \n\t \r\n ") == ""
    assert estimate_tokens("") == 0


def test_unicode_and_special_characters_preserved():
    text = "Café ₹1,00,000 — naïve • C++ & C# · «quoted»"
    assert normalize_text(text) == text


def test_lines_are_never_joined_and_words_never_merged():
    out = normalize_text("First line\nSecond line")
    assert out == "First line\nSecond line"
    assert normalize_text("word\nword") == "word\nword"


def test_idempotent():
    once = normalize_text(MESSY)
    assert normalize_text(once) == once


def test_block_metrics_and_zero_division_guard():
    block = normalization_block(MESSY)
    assert block["status"] == "applied"
    assert block["source"] == "tika"
    assert block["normalized_chars"] < block["original_chars"]
    assert block["original_estimated_tokens"] > 0
    assert block["estimated_reduction_percent"] > 0
    empty = normalization_block("")
    assert empty["status"] == "unavailable"
    assert empty["estimated_reduction_percent"] == 0.0
    none_block = normalization_block(None)
    assert none_block["status"] == "unavailable"


def test_normalization_failure_is_bypassed_not_corrupted(monkeypatch):
    import api.text_normalization as module

    def boom(text):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(module, "normalize_with_steps", boom)
    block = module.normalization_block("some text")
    assert block["status"] == "bypassed"
    assert block["normalized_text"] is None
    assert "kaboom" in block["error"]


class _FakeMuse:
    provider_name = "Muse AI"
    model = "muse-spark-1.3"
    configured = True
    vision_enabled = True
    configuration_error = None

    def __init__(self):
        self.config = type("Cfg", (), {
            "timeout_seconds": 20.0, "context_window_tokens": None})()
        self.vision_kwargs = None
        self.json_text = None

    def vision_extract(self, path, **kwargs):
        self.vision_kwargs = kwargs
        return {"ok": True, "error": None, "error_type": None,
                "resume_json": {"full_name": "Priya Nair"},
                "validation": "valid", "provider": "Muse AI",
                "model": "muse-spark-1.3", "raw_text": "{}",
                "usage": {}, "usage_available": False,
                "request_id": None, "duration_ms": 1.0,
                "timeout_seconds": 20.0, "input_type": "image",
                "visual_provided": True, "tika_text_provided": True,
                "tika_text_chars": len(kwargs.get("tika_text") or ""),
                "hyperlinks_provided": False, "hyperlink_count": 0,
                "page_count": 1, "pages_processed": 1,
                "pages_skipped": 0}

    def generate_resume_json(self, text, **kwargs):
        self.json_text = text
        return {"ok": True, "error": None, "error_type": None,
                "resume_json": {"full_name": "Priya Nair"},
                "validation": "valid", "provider": "Muse AI",
                "model": "muse-spark-1.3", "raw_text": "{}",
                "usage": {}, "usage_available": False,
                "request_id": None, "duration_ms": 1.0,
                "timeout_seconds": 20.0, "input_type": "text",
                "visual_provided": False, "tika_text_provided": False,
                "tika_text_chars": 0, "hyperlinks_provided": False,
                "hyperlink_count": 0}


def _record_with_messy_tika():
    return {"resume_id": "norm-test-1", "attempts": [
                {"extractor_name": "tika", "extracted_text": MESSY,
                 "text": MESSY, "success": False}],
            "attempt_count": 1, "pipeline_trace": [],
            "final_quality_status": "FAIL", "extracted_text": MESSY,
            "resume_json": None, "resume_json_source": None,
            "resume_json_meta": None, "extractor_used": None,
            "selection_reason": "", "hyperlinks": [],
            "hyperlink_error": None, "muse_requests": [],
            "text_normalization": None}


def test_muse_vision_receives_normalized_tika_text():
    record = _record_with_messy_tika()
    service = _FakeMuse()
    _apply_muse_stages(record, Path("resume.png"), service, None)
    sent = service.vision_kwargs["tika_text"]
    assert sent == normalize_text(MESSY)
    assert "\r" not in sent and "   " not in sent
    # The block on the record holds exactly what was sent.
    assert record["text_normalization"]["normalized_text"] == sent
    assert record["text_normalization"]["status"] == "applied"
    meta = record["attempts"][-1]["metadata"]
    assert meta["tika_text_normalization"]["status"] == "applied"
    assert meta["tika_text_normalization"]["estimated_reduction_percent"] > 0


def test_original_tika_attempt_text_is_never_modified():
    record = _record_with_messy_tika()
    _apply_muse_stages(record, Path("resume.png"), _FakeMuse(), None)
    tika_attempt = record["attempts"][0]
    assert tika_attempt["extracted_text"] == MESSY
    assert tika_attempt["text"] == MESSY


def test_json_generation_receives_normalized_text():
    record = _record_with_messy_tika()
    record["final_quality_status"] = "PASS"  # skip the vision stage
    record["extractor_used"] = "tika"
    service = _FakeMuse()
    _apply_muse_stages(record, Path("resume.png"), service, None)
    assert service.json_text == normalize_text(MESSY)
    meta = record["resume_json_meta"]["text_normalization"]
    assert meta["status"] == "applied"
    assert meta["source"] == "tika"


def test_frontend_displays_both_text_versions():
    """Production UI (v14): the inspector has Extracted and Normalized
    tabs fed by text_normalization, with a side-by-side comparison."""
    root = Path(__file__).resolve().parent.parent
    js = (root / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    assert "text_normalization" in js
    assert "normalized_text" in js
    assert "estimated_reduction_percent" in js
    assert "Extracted Text" in js and "Normalized Text" in js
    assert "Original extracted text" in js  # comparison pane heading


# ---------------------------------------------------------------------------
# Decorative separators, traceability fields, selected-extractor source
# ---------------------------------------------------------------------------

from api.text_normalization import compact_resume_text  # noqa: E402


def test_decorative_separator_lines_removed():
    text = ("Priya Nair\n"
            "----------------------\n"
            "SKILLS\n"
            "======================\n"
            "Python, FastAPI\n"
            "**********************\n"
            "• • •\n"
            "______________________\n"
            "EXPERIENCE\n"
            "Software Engineer")
    out = normalize_text(text)
    assert out == ("Priya Nair\nSKILLS\nPython, FastAPI\n"
                   "EXPERIENCE\nSoftware Engineer")


def test_table_structure_is_preserved():
    text = ("| Skill | Years |\n"
            "| --- | --- |\n"
            "| Python | 3 |")
    assert normalize_text(text) == text


def test_meaningful_content_never_treated_as_separator():
    # Lines containing letters/digits are content, not decoration —
    # even when they also contain separator characters.
    text = "C++ & C#\nJan 2023 – Present\n+91 98765 43210"
    assert normalize_text(text) == text


def test_block_preserves_meaningful_paragraph_breaks():
    # Regression guard: the block must NOT strip every blank line
    # (the removed compaction behaviour) — one blank line between
    # paragraphs is meaningful and stays.
    block = normalization_block("Para one.\n\n\n\nPara two.")
    assert block["status"] == "applied"
    assert block["normalized_text"] == "Para one.\n\nPara two."
    assert compact_resume_text("Para one.\n\n\n\nPara two.") == \
        "Para one.\n\nPara two."


def test_block_traceability_fields():
    block = normalization_block(MESSY)
    assert block["original_extracted_text"] == MESSY
    assert block["normalization_applied"] is True
    steps = block["normalization_steps"]
    assert "line_endings_normalized" in steps
    assert "whitespace_runs_collapsed" in steps
    assert "excessive_blank_lines_collapsed" in steps


def test_block_steps_record_decorative_removal():
    block = normalization_block("Name\n-----\nSkills")
    assert block["normalization_applied"] is True
    assert "decorative_separator_lines_removed" in \
        block["normalization_steps"]
    assert block["normalized_text"] == "Name\nSkills"


def test_clean_text_reports_no_change():
    clean = "Priya Nair\nPython Developer\n\nSKILLS\nPython, FastAPI"
    block = normalization_block(clean)
    assert block["status"] == "applied"
    assert block["normalization_applied"] is False
    assert block["normalization_steps"] == []
    assert block["normalized_text"] == clean
    assert block["estimated_reduction_percent"] == 0.0


def test_whitespace_only_block():
    block = normalization_block("   \n\t \n")
    assert block["status"] == "applied"
    assert block["normalized_text"] == ""
    assert block["normalization_applied"] is True
    assert block["original_extracted_text"] == "   \n\t \n"


def test_idempotent_with_separators_and_noise():
    text = "  A   B \r\n-----\r\n\r\n\r\n•  Python  Dev "
    once = normalize_text(text)
    assert normalize_text(once) == once
    assert "-----" not in once


PYMUPDF_MESSY = (
    "  Aarav   Shah  \r\n"
    "--------------------\r\n"
    "Python   Developer\r\n"
    "\r\n\r\n"
    "Skills:  Python,   FastAPI,  SQL\r\n"
    "Contact: aarav@example.com\r\n"
)


def _record_with_selected_pymupdf():
    return {"resume_id": "norm-test-2", "attempts": [
                {"extractor_name": "tika",
                 "extracted_text": "Tika version of the text",
                 "text": "Tika version of the text", "success": True,
                 "is_final_selected": False},
                {"extractor_name": "pymupdf",
                 "extracted_text": PYMUPDF_MESSY,
                 "text": PYMUPDF_MESSY, "success": True,
                 "is_final_selected": True}],
            "attempt_count": 2, "pipeline_trace": [],
            "final_quality_status": "PASS",
            "extracted_text": PYMUPDF_MESSY,
            "final_extracted_text": PYMUPDF_MESSY,
            "resume_json": None, "resume_json_source": None,
            "resume_json_meta": None, "extractor_used": "pymupdf",
            "final_extractor": "pymupdf",
            "selection_reason": "", "hyperlinks": [],
            "hyperlink_error": None, "muse_requests": [],
            "text_normalization": None}


def test_selected_extractor_is_normalized_not_tika():
    # The pipeline selected PyMuPDF, so the record block, the JSON
    # generation input, and the traceability fields all describe
    # the PyMuPDF text — Tika is not assumed.
    record = _record_with_selected_pymupdf()
    service = _FakeMuse()
    _apply_muse_stages(record, Path("resume.png"), service, None)
    assert service.json_text == normalize_text(PYMUPDF_MESSY)
    assert "--------------------" not in service.json_text
    block = record["text_normalization"]
    assert block["source"] == "pymupdf"
    assert block["original_extracted_text"] == PYMUPDF_MESSY
    assert block["normalized_text"] == service.json_text
    assert "decorative_separator_lines_removed" in \
        block["normalization_steps"]
    meta = record["resume_json_meta"]["text_normalization"]
    assert meta["source"] == "pymupdf"
    assert meta["normalization_applied"] is True
    # Both attempts keep their exact original text.
    assert record["attempts"][0]["extracted_text"] == \
        "Tika version of the text"
    assert record["attempts"][1]["extracted_text"] == PYMUPDF_MESSY


# ---------------------------------------------------------------------------
# Regression: vision-only records (no chain text) and stale blocks
# ---------------------------------------------------------------------------

from api.pipeline import ensure_pipeline_trace  # noqa: E402


def _record_no_chain_text():
    return {"resume_id": "norm-test-3", "attempts": [
                {"extractor_name": "tika", "extracted_text": "",
                 "text": "", "success": False,
                 "is_final_selected": False},
                {"extractor_name": "pymupdf", "extracted_text": "",
                 "text": "", "success": False,
                 "is_final_selected": False},
                {"extractor_name": "ocr", "extracted_text": "",
                 "text": "", "success": False,
                 "is_final_selected": False}],
            "attempt_count": 3, "pipeline_trace": [],
            "final_quality_status": "FAIL", "extracted_text": "",
            "final_extracted_text": "",
            "resume_json": None, "resume_json_source": None,
            "resume_json_meta": None, "extractor_used": None,
            "final_extractor": None,
            "selection_reason": "", "hyperlinks": [],
            "hyperlink_error": None, "muse_requests": [],
            "text_normalization": None}


def test_vision_only_record_never_uses_resume_json_as_text():
    # Every chain extractor returned empty text; Muse vision
    # produced the Resume JSON from the file itself. The
    # normalization block must report that honestly (unavailable,
    # no source extractor) and must NEVER present the Muse
    # attempt's raw output (the Resume JSON) as extracted text.
    record = _record_no_chain_text()
    _apply_muse_stages(record, Path("resume.png"), _FakeMuse(), None)
    assert record["resume_json"] is not None  # Resume JSON exists
    block = record["text_normalization"]
    assert block["status"] == "unavailable"
    assert block["source"] == "none"
    assert block["original_extracted_text"] is None
    assert block["normalized_text"] is None
    assert block["original_chars"] == 0
    muse_attempt = record["attempts"][-1]
    assert muse_attempt["extractor_name"] == "muse_ai"
    assert muse_attempt["extracted_text"] == "{}"  # raw JSON output
    assert block["original_extracted_text"] != \
        muse_attempt["extracted_text"]


def test_stale_unavailable_block_recalculated_from_real_text():
    # Metadata missing/stale while actual extracted text exists:
    # the block is recalculated from the selected extractor's
    # text instead of showing false zeros.
    record = _record_with_selected_pymupdf()
    record["text_normalization"] = {
        "source": "none", "status": "unavailable", "error": None,
        "original_extracted_text": None, "normalized_text": None,
        "normalization_applied": False, "normalization_steps": [],
        "original_chars": 0, "normalized_chars": 0,
        "original_estimated_tokens": 0,
        "normalized_estimated_tokens": 0,
        "estimated_reduction_percent": 0.0}
    ensure_pipeline_trace(record)
    block = record["text_normalization"]
    assert block["status"] == "applied"
    assert block["source"] == "pymupdf"
    assert block["original_extracted_text"] == PYMUPDF_MESSY
    assert block["original_chars"] == len(PYMUPDF_MESSY)
    assert block["original_estimated_tokens"] > 0
    assert block["normalized_text"] == normalize_text(PYMUPDF_MESSY)


def test_frontend_original_text_never_from_muse_attempt():
    """Production UI (v14): the inspector's Extracted Text tab reads
    record.extracted_text — the backend-selected chain text — and
    never derives the original text from an attempt (so a Muse
    vision attempt can never become the displayed original)."""
    root = Path(__file__).resolve().parent.parent
    js = (root / "frontend" / "js" / "app.js").read_text(
        encoding="utf-8")
    assert 'const extractedText = record.extracted_text || "";' in js
    assert "chainAttempts" not in js
    assert "attempts.find" not in js

