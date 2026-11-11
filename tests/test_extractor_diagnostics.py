"""One-click extractor check — Settings → Extractor Status.

The probe verifies each extractor's installation using the
extractor's OWN resolution logic (Tika: Java + JAR; PyMuPDF /
Docling: package; OCR: Tesseract binary), and the live test really
extracts the bundled sample through the production code paths.
Everything external is faked here — no real Tika/Docling/OCR runs.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from api import extractor_diagnostics as diag
from api.main import app

client = TestClient(app)


# -- probes ---------------------------------------------------------

def test_probe_pymupdf_available():
    row = diag.probe_pymupdf()
    assert row["name"] == "pymupdf"
    assert row["status"] == "available"  # hard dependency of the app
    assert "PyMuPDF" in row["detail"]


def test_probe_tika_missing_jar_is_unavailable(tmp_path):
    row = diag.probe_tika({"jar_path": str(tmp_path)})
    assert row["status"] == "unavailable"
    assert "not found" in (row["error"] or "")


def test_probe_tika_jar_present_but_java_missing(tmp_path):
    jar = tmp_path / "tika-app-9.9.9.jar"
    jar.write_bytes(b"fake")
    row = diag.probe_tika({"jar_path": str(jar),
                           "java_bin": "definitely-missing-java-xyz"})
    assert row["status"] == "unavailable"
    assert "Java" in (row["error"] or "")
    assert "9.9.9" in row["detail"]  # the JAR itself was found


def test_probe_ocr_missing_tesseract_is_unavailable():
    row = diag.probe_ocr({"tesseract_cmd": "Z:\\nope\\tesseract.exe"})
    assert row["status"] == "unavailable"
    assert row["error"]


def test_probe_all_shape_and_order():
    payload = diag.probe_extractors()
    names = [row["name"] for row in payload["extractors"]]
    assert names == ["tika", "pymupdf", "docling", "ocr"]
    for row in payload["extractors"]:
        assert row["status"] in ("available", "unavailable")
        assert row["label"]
    by_name = {row["name"]: row for row in payload["extractors"]}
    assert by_name["pymupdf"]["status"] == "available"


def test_status_endpoint():
    response = client.get("/api/extractors/status")
    assert response.status_code == 200
    body = response.json()
    assert [r["name"] for r in body["extractors"]] == [
        "tika", "pymupdf", "docling", "ocr"]
    assert body["checked_at"]


# -- live test mapping ------------------------------------------------

def test_live_tests_map_production_results(monkeypatch, tmp_path):
    sample = tmp_path / "sample_resume.pdf"
    sample.write_bytes(b"%PDF-fake")

    def fake_run(file_path, extractors=None, timeout_seconds=None):
        assert timeout_seconds == 600.0  # the pipeline's configured cap
        return [
            {"extractor": "tika", "status": "success", "text": "x" * 10,
             "character_count": 10, "execution_time": 1.5, "error": None},
            {"extractor": "pymupdf", "status": "success", "text": "y" * 20,
             "character_count": 20, "execution_time": 0.25, "error": None},
            {"extractor": "docling", "status": "failed", "text": "",
             "character_count": 0, "execution_time": 0.5,
             "error": "The 'docling' package is not installed."},
        ]

    class FakeOCR:
        def __init__(self, config=None):
            pass

        def extract(self, path):
            return SimpleNamespace(success=True, text="abc",
                                   duration_ms=42.0, error=None)

    monkeypatch.setattr("pipeline.extractors.run_extractors", fake_run)
    monkeypatch.setattr("pipeline.extractors._default_extractors",
                        lambda: {})
    monkeypatch.setattr("extractor.extractors.ocr.OCRExtractor", FakeOCR)

    payload = diag.run_live_tests(sample)
    assert payload["file"] == "sample_resume.pdf"
    results = payload["results"]
    assert list(results) == ["tika", "pymupdf", "docling", "ocr"]
    assert results["tika"] == {"status": "success", "chars": 10,
                                "duration_ms": 1500, "error": None}
    assert results["docling"]["status"] == "failed"
    assert "not installed" in results["docling"]["error"]
    assert results["ocr"] == {"status": "success", "chars": 3,
                               "duration_ms": 42, "error": None}


def test_test_endpoint_uses_bundled_sample(monkeypatch):
    monkeypatch.setattr(
        diag, "run_live_tests",
        lambda path: {"file": path.name, "ran_at": "t",
                      "results": {"pymupdf": {"status": "success",
                                              "chars": 5,
                                              "duration_ms": 3,
                                              "error": None}}})
    response = client.post("/api/extractors/test")
    assert response.status_code == 200
    assert response.json()["file"] == "sample_resume.pdf"


def test_test_endpoint_missing_sample_is_409(monkeypatch, tmp_path):
    monkeypatch.setattr(diag, "sample_path",
                        lambda: tmp_path / "nope.pdf")
    response = client.post("/api/extractors/test")
    assert response.status_code == 409
    assert "sample_resume.pdf" in response.json()["detail"] or \
        "nope.pdf" in response.json()["detail"]


# -- frontend wiring ----------------------------------------------------

def test_frontend_wiring():
    root = Path(__file__).resolve().parent.parent
    api_js = (root / "frontend" / "js" / "api.js").read_text(encoding="utf-8")
    app_js = (root / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    assert "/api/extractors/status" in api_js
    assert "/api/extractors/test" in api_js
    assert "extractorCheckBtn" in app_js
    assert "extractorTestBtn" in app_js
    assert "Run live test" in app_js
