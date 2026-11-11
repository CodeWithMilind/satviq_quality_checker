"""PDF hyperlink annotation extraction — deterministic, from the
file's own annotation layer (extracted text never contains these
URLs). Fixtures are built with PyMuPDF: real annotations, no mocks.

Note: the process_resume_file case pins the LEGACY pipeline
explicitly (pipeline_mode="legacy"); hyperlink capture into the
record happens in the legacy Muse stages.
"""

import fitz

from api.hyperlinks import extract_pdf_hyperlinks
from api.pipeline import process_resume_file

PORTFOLIO_URL = "https://example.com/portfolio?ref=resume%20v2"
LINKEDIN_URL = "https://www.linkedin.com/in/someone-123"


def _pdf_with_links(tmp_path, *, with_links=True):
    path = tmp_path / "resume.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Priya Nair", fontsize=14)
    page.insert_text((72, 110), "Portfolio", fontsize=11)
    page.insert_text((72, 140), "LinkedIn", fontsize=11)
    if with_links:
        page.insert_link({"kind": fitz.LINK_URI,
                          "from": fitz.Rect(72, 100, 140, 122),
                          "uri": PORTFOLIO_URL})
        page.insert_link({"kind": fitz.LINK_URI,
                          "from": fitz.Rect(72, 130, 140, 152),
                          "uri": LINKEDIN_URL})
        page.insert_link({"kind": fitz.LINK_URI,
                          "from": fitz.Rect(300, 100, 360, 122),
                          "uri": "mailto:priya@example.com"})
    doc.save(path)
    doc.close()
    return path


def test_extracts_annotations_with_page_text_and_rect(tmp_path):
    result = extract_pdf_hyperlinks(_pdf_with_links(tmp_path))
    assert result["error"] is None
    by_url = {h["url"]: h for h in result["hyperlinks"]}
    assert set(by_url) == {PORTFOLIO_URL, LINKEDIN_URL,
                           "mailto:priya@example.com"}
    portfolio = by_url[PORTFOLIO_URL]
    assert portfolio["page"] == 1
    assert portfolio["visible_text"] == "Portfolio"
    assert portfolio["type"] == "uri"
    assert portfolio["rect"] is not None and len(portfolio["rect"]) == 4
    assert by_url["mailto:priya@example.com"]["type"] == "mailto"


def test_url_preserved_exactly(tmp_path):
    result = extract_pdf_hyperlinks(_pdf_with_links(tmp_path))
    urls = [h["url"] for h in result["hyperlinks"]]
    assert PORTFOLIO_URL in urls  # query + percent-encoding untouched


def test_pdf_without_links_returns_empty_list(tmp_path):
    result = extract_pdf_hyperlinks(
        _pdf_with_links(tmp_path, with_links=False))
    assert result == {"hyperlinks": [], "error": None}


def test_broken_pdf_records_failure_and_continues(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF- not really a pdf at all")
    result = extract_pdf_hyperlinks(broken)
    assert result["hyperlinks"] == []
    assert result["error"] and "failed" in result["error"].lower()


def test_non_pdf_is_empty_without_error(tmp_path):
    png = tmp_path / "resume.png"
    png.write_bytes(b"\x89PNG")
    assert extract_pdf_hyperlinks(png) == {"hyperlinks": [],
                                           "error": None}


def test_visible_text_alone_is_not_a_hyperlink(tmp_path):
    """A page that merely PRINTS a URL as text has no annotations —
    nothing may be reported as a confirmed hyperlink."""
    path = tmp_path / "printed.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Portfolio: https://printed.example.com",
                     fontsize=11)
    doc.save(path)
    doc.close()
    assert extract_pdf_hyperlinks(path)["hyperlinks"] == []


def test_pipeline_record_carries_hyperlinks(tmp_path):
    class Unconfigured:
        configured = False
        vision_enabled = True
        model = "muse-spark-1.3"
        configuration_error = "MUSE_API_KEY is not set"
        config = None

    pdf = _pdf_with_links(tmp_path)
    record = process_resume_file(
        pdf, resume_id="links-test-1", original_filename="resume.pdf",
        muse_service=Unconfigured(), pipeline_mode="legacy")
    urls = {h["url"] for h in record["hyperlinks"]}
    assert PORTFOLIO_URL in urls and LINKEDIN_URL in urls
    assert record["hyperlink_error"] is None
