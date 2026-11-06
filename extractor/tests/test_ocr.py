"""Tests for the OCR extractor — Tesseract is never required:
availability failures are asserted for real, and successful OCR is
exercised through the extractor's ``_image_to_text`` seam.
"""

from pathlib import Path

import pytest

from extractor.extractors.ocr import OCRExtractor

OCR_TEXT = "Jane Doe Software Engineer Python FastAPI experience in building APIs"


@pytest.fixture()
def image_file(tmp_path: Path) -> Path:
    Image = pytest.importorskip("PIL.Image")
    path = tmp_path / "resume.png"
    Image.new("RGB", (800, 200), color="white").save(str(path))
    return path


def _make_working_ocr(monkeypatch, extractor: OCRExtractor, text: str = OCR_TEXT):
    monkeypatch.setattr(extractor, "_load_pytesseract", lambda: object())
    monkeypatch.setattr(extractor, "_image_to_text", lambda image: text)


def test_missing_tesseract_is_reported_honestly(image_file, monkeypatch):
    """With the binary lookup forced to None and no pytesseract stub,
    OCR must fail as unavailable — it must never claim success."""
    extractor = OCRExtractor()
    monkeypatch.setattr(extractor, "_tesseract_path", lambda: None)
    result = extractor.extract(image_file)
    assert result.success is False
    assert "not installed" in result.error or "unavailable" in result.error.lower()


def test_successful_image_ocr(image_file, monkeypatch):
    extractor = OCRExtractor(config={"language": "eng", "dpi": 200})
    _make_working_ocr(monkeypatch, extractor)
    result = extractor.extract(image_file)
    assert result.success is True
    assert result.text == OCR_TEXT
    assert result.extractor_name == "ocr"
    assert result.page_count == 1
    assert result.page_texts == [OCR_TEXT]
    assert result.metadata["ocr_language"] == "eng"
    assert result.metadata["ocr_dpi"] == 200


def test_empty_ocr_result_is_failure(image_file, monkeypatch):
    extractor = OCRExtractor()
    _make_working_ocr(monkeypatch, extractor, text="   ")
    result = extractor.extract(image_file)
    assert result.success is False
    assert "recognised no text" in result.error


def test_pdf_ocr_page_by_page(tmp_path, monkeypatch):
    pymupdf = pytest.importorskip("pymupdf")
    path = tmp_path / "scanned.pdf"
    document = pymupdf.open()
    document.new_page()
    document.new_page()
    document.save(str(path))
    document.close()

    extractor = OCRExtractor()
    _make_working_ocr(monkeypatch, extractor)
    monkeypatch.setattr(extractor, "_render_pdf_pages", lambda p: [object(), object()])
    result = extractor.extract(path)
    assert result.success is True
    assert result.page_count == 2
    assert len(result.page_texts) == 2


def test_ocr_error_is_reported(image_file, monkeypatch):
    extractor = OCRExtractor()
    monkeypatch.setattr(extractor, "_load_pytesseract", lambda: object())

    def _boom(image):
        from extractor.exceptions import ExtractionFailedError
        raise ExtractionFailedError("Tesseract OCR failed on a page/image: boom")

    monkeypatch.setattr(extractor, "_image_to_text", _boom)
    result = extractor.extract(image_file)
    assert result.success is False
    assert "OCR failed" in result.error


def test_invalid_file_path(tmp_path):
    result = OCRExtractor().extract(tmp_path / "missing.png")
    assert result.success is False
    assert "not found" in result.error.lower()


def test_unsupported_file_type(tmp_path):
    path = tmp_path / "resume.docx"
    path.write_bytes(b"fake")
    result = OCRExtractor().extract(path)
    assert result.success is False
    assert "does not support" in result.error


def test_language_and_dpi_are_configurable():
    extractor = OCRExtractor(config={"language": "eng+hin", "dpi": 150})
    assert extractor.language == "eng+hin"
    assert extractor.dpi == 150
