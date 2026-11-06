"""Tests for the PyMuPDF extractor using real, generated PDFs."""

from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")

from extractor.extractors.pymupdf import PyMuPDFExtractor  # noqa: E402
from extractor.models import ExtractionResult  # noqa: E402

RESUME_LINE = "Jane Doe Software Engineer Python FastAPI REST APIs SQL data pipelines"


def _make_pdf(path: Path, lines: list[str]) -> Path:
    document = pymupdf.open()
    page = document.new_page()
    y = 72
    for line in lines:
        page.insert_text((72, y), line, fontsize=11)
        y += 20
    document.save(str(path))
    document.close()
    return path


@pytest.fixture()
def text_pdf(tmp_path: Path) -> Path:
    return _make_pdf(tmp_path / "resume.pdf", [RESUME_LINE, RESUME_LINE])


@pytest.fixture()
def blank_pdf(tmp_path: Path) -> Path:
    document = pymupdf.open()
    document.new_page()
    path = tmp_path / "scanned.pdf"
    document.save(str(path))
    document.close()
    return path


def test_successful_extraction_with_page_texts(text_pdf):
    result = PyMuPDFExtractor().extract(text_pdf)
    assert isinstance(result, ExtractionResult)
    assert result.success is True
    assert "Jane Doe" in result.text
    assert result.page_count == 1
    assert len(result.page_texts) == 1
    assert result.file_type == "pdf"
    assert result.error is None


def test_scanned_pdf_reports_no_meaningful_text(blank_pdf):
    result = PyMuPDFExtractor().extract(blank_pdf)
    assert result.success is False
    assert "no meaningful embedded text" in result.error.lower()
    assert any("scanned" in w.lower() for w in result.warnings)


def test_encrypted_pdf_fails_cleanly(tmp_path):
    path = tmp_path / "secret.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), RESUME_LINE, fontsize=11)
    document.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256,
                  user_pw="user-secret", owner_pw="owner-secret")
    document.close()
    result = PyMuPDFExtractor().extract(path)
    assert result.success is False
    assert "encrypted" in result.error.lower() or "password" in result.error.lower()


def test_encrypted_pdf_opens_with_configured_password(tmp_path):
    path = tmp_path / "secret2.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), RESUME_LINE + " " + RESUME_LINE, fontsize=11)
    document.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256,
                  user_pw="user-secret", owner_pw="owner-secret")
    document.close()
    result = PyMuPDFExtractor(config={"password": "user-secret"}).extract(path)
    assert result.success is True
    assert "Jane Doe" in result.text


def test_malformed_pdf_fails(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"this is not a pdf at all")
    result = PyMuPDFExtractor().extract(path)
    assert result.success is False
    assert result.error is not None


def test_invalid_file_path(tmp_path):
    result = PyMuPDFExtractor().extract(tmp_path / "missing.pdf")
    assert result.success is False
    assert "not found" in result.error.lower()


def test_unsupported_file_type(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")
    result = PyMuPDFExtractor().extract(path)
    assert result.success is False
    assert "does not support" in result.error
