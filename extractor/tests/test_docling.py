"""Tests for the Docling extractor — converter is faked via its seam,
so no Docling installation or model download is required."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from extractor.exceptions import ExtractorUnavailableError
from extractor.extractors.docling import DoclingExtractor

GOOD_TEXT = "Jane Doe\nSoftware Engineer\nExperience in Python, APIs and data pipelines at scale."


@pytest.fixture()
def sample_file(tmp_path: Path) -> Path:
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-fake-bytes-for-mocked-converter")
    return path


def _fake_converter(text=GOOD_TEXT, raises=None):
    def convert(file_path):
        if raises is not None:
            raise raises
        document = SimpleNamespace(export_to_text=lambda: text)
        return SimpleNamespace(document=document, status="success", errors=[])
    return SimpleNamespace(convert=convert)


def test_successful_extraction(sample_file, monkeypatch):
    extractor = DoclingExtractor(config={"options": {"do_ocr": False}})
    monkeypatch.setattr(extractor, "_create_converter", lambda: _fake_converter())
    result = extractor.extract(sample_file)
    assert result.success is True
    assert result.text == GOOD_TEXT
    assert result.extractor_name == "docling"
    assert result.metadata["docling_options"] == {"do_ocr": False}
    assert result.metadata["conversion_status"] == "success"


def test_conversion_failure_is_reported(sample_file, monkeypatch):
    extractor = DoclingExtractor()
    monkeypatch.setattr(extractor, "_create_converter",
                        lambda: _fake_converter(raises=RuntimeError("model load failed")))
    result = extractor.extract(sample_file)
    assert result.success is False
    assert "Docling conversion failed" in result.error


def test_empty_extraction_is_failure(sample_file, monkeypatch):
    extractor = DoclingExtractor()
    monkeypatch.setattr(extractor, "_create_converter", lambda: _fake_converter(text="  "))
    result = extractor.extract(sample_file)
    assert result.success is False
    assert "no text" in result.error.lower()


def test_unavailable_package_is_reported(sample_file, monkeypatch):
    extractor = DoclingExtractor()

    def _raise():
        raise ExtractorUnavailableError("The 'docling' package is not installed.")

    monkeypatch.setattr(extractor, "_create_converter", _raise)
    result = extractor.extract(sample_file)
    assert result.success is False
    assert "not installed" in result.error


def test_invalid_file_path(tmp_path):
    result = DoclingExtractor().extract(tmp_path / "missing.pdf")
    assert result.success is False
    assert "not found" in result.error.lower()


def test_unsupported_file_type(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")
    result = DoclingExtractor().extract(path)
    assert result.success is False
    assert "does not support" in result.error
