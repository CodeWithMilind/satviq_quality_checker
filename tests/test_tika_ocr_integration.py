r"""Local integration tests: real Apache Tika 4.2.0 + real Tesseract OCR.

These tests exercise the REAL stack — Java subprocess, the project's
generated Tika config, Tesseract OCR, the Quality Checker and the
fallback pipeline — against a scanned (image-only) PDF and a PNG
image whose text exists only as pixels.

They are SKIPPED unless the environment provides the tools:

- ``TIKA_JAR_PATH`` (or ``SATVIQ_TIKA_JAR``) pointing at
  tika-app-4.2.0.jar (with its lib/ folder beside it), and
- ``java`` on PATH, and ``tesseract`` on PATH (or TESSERACT_PATH).

Run locally, e.g. on Windows:

    set TIKA_JAR_PATH=C:\Apache\Tika\tika-app-4.2.0.jar
    python -m pytest tests/test_tika_ocr_integration.py -v

Fixtures are generated with Pillow + PyMuPDF when available (the
scanned PDF is verified to contain zero embedded text before use).
"""

import os
import shutil
from pathlib import Path

import pytest

JAR = os.environ.get("TIKA_JAR_PATH") or os.environ.get("SATVIQ_TIKA_JAR")

pytestmark = pytest.mark.skipif(
    not JAR or not Path(JAR).is_file()
    or shutil.which("java") is None
    or (shutil.which("tesseract") is None and not os.environ.get("TESSERACT_PATH")),
    reason="local Tika 4.2.0 JAR / Java / Tesseract not available "
           "(set TIKA_JAR_PATH to run these integration tests)")

LINES = [
    "Priya Nair",
    "priya.nair@example.com | +91 98220 12345 | Pune, India",
    "Contact Information",
    "Email: priya.nair@example.com, Phone: +91 98220 12345, Location: Pune, India.",
    "Professional Summary",
    "Careful developer who writes Python programs and simple web pages for",
    "local clients. She tests her work, writes clear notes, and fixes",
    "defects methodically over time.",
    "Skills",
    "Python, FastAPI, SQL, Git, data pipelines and testing tools.",
]


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    pil = pytest.importorskip("PIL")
    fitz = pytest.importorskip("fitz")
    from PIL import Image, ImageDraw, ImageFont

    folder = tmp_path_factory.mktemp("ocr_fixtures")
    img = Image.new("RGB", (1240, 700), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 30)
    except Exception:
        font = ImageFont.load_default()
    y = 40
    for line in LINES:
        draw.text((60, y), line, fill="black", font=font)
        y += 55
    png = folder / "resume_image.png"
    img.save(str(png), dpi=(300, 300))
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, filename=str(png))
    scanned = folder / "resume_scanned.pdf"
    doc.save(str(scanned))
    assert fitz.open(str(scanned))[0].get_text() == "", \
        "fixture must be image-only (no text layer)"
    text_pdf = folder / "resume_text.pdf"
    doc2 = fitz.open()
    page2 = doc2.new_page()
    for i, line in enumerate(LINES):
        page2.insert_text((50, 60 + i * 22), line)
    doc2.save(str(text_pdf))
    return {"png": png, "scanned": scanned, "text_pdf": text_pdf}


def test_scanned_pdf_is_ocrd_by_tika(fixtures):
    from extractor.extractors.tika import TikaExtractor
    result = TikaExtractor().extract(fixtures["scanned"])
    assert result.success, result.error
    assert "Priya Nair" in result.text
    assert "priya.nair@example.com" in result.text


def test_image_resume_is_ocrd_by_tika(fixtures):
    from extractor.extractors.tika import TikaExtractor
    result = TikaExtractor().extract(fixtures["png"])
    assert result.success, result.error
    assert "Priya Nair" in result.text
    assert result.metadata["ocr_used"] is True


def test_text_pdf_still_uses_text_layer(fixtures):
    from extractor.extractors.tika import TikaExtractor
    result = TikaExtractor().extract(fixtures["text_pdf"])
    assert result.success, result.error
    assert "Priya Nair" in result.text


def test_scanned_pdf_flows_through_pipeline_with_attempt_preserved(fixtures):
    """End-to-end: manager + Quality Checker + record building; the
    Tika attempt (with its OCR text and OCR metadata) is preserved."""
    from api.pipeline import QualityGatePolicy, build_record_from_result
    from extractor import ExtractionManager
    from quality_checker import QualityChecker

    policy = QualityGatePolicy(QualityChecker(), document_id="ocr-int-1")
    manager = ExtractionManager(acceptance_policy=policy)
    result = manager.extract(fixtures["scanned"])
    record = build_record_from_result(
        resume_id="ocr-int-1", original_filename="resume_scanned.pdf",
        path=fixtures["scanned"], mime_type="application/pdf",
        file_size=fixtures["scanned"].stat().st_size,
        result=result, policy=policy, order=manager.order)
    tika_attempt = record["attempts"][0]
    assert tika_attempt["extractor_name"] == "tika"
    assert "Priya Nair" in tika_attempt["extracted_text"]
    assert tika_attempt["ocr"] is not None
    assert tika_attempt["ocr"]["ocr_enabled"] is True
    assert tika_attempt["quality_checker_report"] is not None
