"""Tests for the Tika extractor — Apache Tika 4.2.0 via Java subprocess.

No real Tika JAR and no Java runtime are required: the subprocess
seam (``_run_cli``) is replaced with a fake completed process, and
the two error mappings inside the real ``_run_cli`` (Java missing,
timeout) are exercised by stubbing ``subprocess.run`` in the tika
module's namespace.

Covered: successful CLI extraction, JAR path resolution (config /
environment / unconfigured), JAR not found, Java not installed, CLI
non-zero exit, empty output, timeout, version parsing — and the
Tesseract OCR integration: generated Tika config structure,
tesseractPath/tessdataPath pairing, OCR-disabled behaviour,
external config handling, Tesseract-missing diagnostics, and the
per-attempt OCR metadata (real OCR runs live in
tests/test_tika_ocr_integration.py, env-gated).
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from extractor.extractors import tika as tika_module
from extractor.extractors.tika import TikaExtractor
from extractor.models import ExtractionResult

GOOD_TEXT = "Jane Doe\nSoftware Engineer with experience in Python, APIs and data pipelines."


@pytest.fixture()
def sample_file(tmp_path: Path) -> Path:
    path = tmp_path / "resume.txt"
    path.write_text(GOOD_TEXT, encoding="utf-8")
    return path


@pytest.fixture()
def fake_jar(tmp_path: Path) -> Path:
    jar = tmp_path / "tika-app-4.2.0.jar"
    jar.write_bytes(b"fake-jar-for-tests")
    return jar


def _completed(returncode=0, stdout=GOOD_TEXT, stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _stub_cli(extractor, monkeypatch, completed=None, capture=None):
    def fake_run_cli(argv, timeout=None):
        if capture is not None:
            capture["argv"] = argv
            capture["timeout"] = timeout
            # The generated --config file exists only during the run;
            # read it now so tests can assert its structure.
            for arg in argv:
                if arg.startswith("--config="):
                    cfg_path = Path(arg.split("=", 1)[1])
                    capture["config_path"] = cfg_path
                    if cfg_path.is_file():
                        capture["config"] = json.loads(
                            cfg_path.read_text(encoding="utf-8"))
        return completed if completed is not None else _completed()
    monkeypatch.setattr(extractor, "_run_cli", fake_run_cli)


def test_successful_extraction_via_cli(sample_file, fake_jar, monkeypatch):
    capture = {}
    extractor = TikaExtractor(config={"jar_path": str(fake_jar),
                                      "timeout_seconds": 45})
    _stub_cli(extractor, monkeypatch, capture=capture)
    result = extractor.extract(sample_file)
    assert isinstance(result, ExtractionResult)
    assert result.success is True
    assert result.text == GOOD_TEXT
    assert result.extractor_name == "tika"
    assert result.file_type == "txt"
    assert result.error is None
    assert result.duration_ms >= 0
    # The CLI contract: java -jar <jar> [--config=<ocr config>] --text <file>
    # (--config must precede the extraction options; OCR is on by default.)
    argv = capture["argv"]
    assert argv[0] == "java"
    assert argv[1] == "-jar"
    assert argv[2] == str(fake_jar)
    assert argv[3].startswith("--config=")
    assert argv[4] == "--text"
    assert argv[5] == str(sample_file)
    # The configured extraction-only safety cap is passed through
    # to the subprocess (spec §5).
    assert capture["timeout"] == 45.0
    # Version comes from the JAR filename and rides in the metadata.
    assert extractor.version == "4.2.0"
    assert result.metadata["tika_version"] == "4.2.0"
    # The generated config file is cleaned up after the run.
    assert not capture["config_path"].exists()


def test_jar_path_from_environment(sample_file, fake_jar, monkeypatch):
    monkeypatch.setenv("TIKA_JAR_PATH", str(fake_jar))
    extractor = TikaExtractor()
    assert extractor.jar_path == str(fake_jar)
    assert extractor.version == "4.2.0"
    _stub_cli(extractor, monkeypatch)
    assert extractor.extract(sample_file).success is True


def test_jar_not_found_is_precise_failure(sample_file, tmp_path, monkeypatch):
    missing = tmp_path / "tika-app-4.2.0.jar"  # never created
    extractor = TikaExtractor(config={"jar_path": str(missing)})
    called = []
    monkeypatch.setattr(extractor, "_run_cli",
                        lambda argv, timeout: called.append(argv))
    result = extractor.extract(sample_file)
    assert result.success is False
    assert "JAR not found" in result.error
    assert str(missing) in result.error
    assert result.error_type == "ExtractorUnavailableError"
    assert result.error_traceback  # traceback preserved for inspection
    assert called == [], "subprocess must not run when the JAR is missing"


def test_jar_path_not_configured(sample_file, monkeypatch):
    monkeypatch.delenv("TIKA_JAR_PATH", raising=False)
    monkeypatch.delenv("TIKA_APP_JAR", raising=False)
    monkeypatch.setattr(tika_module, "_default_jar_path", lambda: None)
    extractor = TikaExtractor(config={})
    assert extractor.jar_path is None
    result = extractor.extract(sample_file)
    assert result.success is False
    assert "TIKA_JAR_PATH" in result.error
    assert result.error_type == "ExtractorUnavailableError"


def test_java_not_installed(sample_file, fake_jar, monkeypatch):
    def raising_run(*args, **kwargs):
        raise FileNotFoundError("No such file or directory: 'java'")
    monkeypatch.setattr(tika_module, "subprocess", SimpleNamespace(
        run=raising_run, TimeoutExpired=subprocess.TimeoutExpired,
        CompletedProcess=subprocess.CompletedProcess))
    extractor = TikaExtractor(config={"jar_path": str(fake_jar)})
    result = extractor.extract(sample_file)
    assert result.success is False
    assert "Java executable not found" in result.error
    assert result.error_type == "ExtractorUnavailableError"


def test_cli_nonzero_exit_is_reported(sample_file, fake_jar, monkeypatch):
    extractor = TikaExtractor(config={"jar_path": str(fake_jar)})
    _stub_cli(extractor, monkeypatch,
              completed=_completed(returncode=1, stdout="",
                                   stderr="SEVERE: could not parse document"))
    result = extractor.extract(sample_file)
    assert result.success is False
    assert "exit code 1" in result.error
    assert "could not parse document" in result.error
    assert result.error_type == "ExtractionFailedError"


def test_empty_output_is_failure_not_silent_success(sample_file, fake_jar, monkeypatch):
    extractor = TikaExtractor(config={"jar_path": str(fake_jar)})
    _stub_cli(extractor, monkeypatch, completed=_completed(stdout="   \n"))
    result = extractor.extract(sample_file)
    assert result.success is False
    assert "no text" in result.error.lower()
    assert result.text == ""


def test_cli_runs_without_timeout_when_unconfigured(sample_file, fake_jar, monkeypatch):
    # With no timeout_seconds configured, the real _run_cli invokes
    # subprocess.run with timeout=None (wait however long it
    # takes). The cap exists only when configured (spec §5).
    captured = {}
    def recording_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            args=args[0] if args else [], returncode=0,
            stdout=GOOD_TEXT, stderr="")
    monkeypatch.setattr(tika_module, "subprocess", SimpleNamespace(
        run=recording_run, TimeoutExpired=subprocess.TimeoutExpired,
        CompletedProcess=subprocess.CompletedProcess))
    extractor = TikaExtractor(config={"jar_path": str(fake_jar)})
    result = extractor.extract(sample_file)
    assert result.success is True
    assert captured.get("timeout") is None


def test_invalid_file_path(tmp_path, fake_jar):
    result = TikaExtractor(config={"jar_path": str(fake_jar)}).extract(
        tmp_path / "does-not-exist.pdf")
    assert result.success is False
    assert "not found" in result.error.lower()


def test_java_bin_and_jar_are_configurable(fake_jar):
    extractor = TikaExtractor(config={"jar_path": str(fake_jar),
                                      "java_bin": "/opt/jdk21/bin/java"})
    assert extractor.jar_path == str(fake_jar)
    assert extractor.java_bin == "/opt/jdk21/bin/java"


def test_no_server_architecture_remains():
    """The old server-client design must be gone: no server_url, no
    _load_parser seam, no tika pip package import."""
    extractor = TikaExtractor(config={"jar_path": "x/tika-app-4.2.0.jar"})
    assert not hasattr(extractor, "server_url")
    assert not hasattr(extractor, "_load_parser")


# ---------------------------------------------------------------------------
# OCR (Tesseract) integration — mocked subprocess, no real Tesseract needed.
# ---------------------------------------------------------------------------

def test_generated_ocr_config_structure(sample_file, fake_jar, monkeypatch):
    capture = {}
    extractor = TikaExtractor(config={"jar_path": str(fake_jar)})
    _stub_cli(extractor, monkeypatch, capture=capture)
    assert extractor.extract(sample_file).success is True
    config = capture["config"]
    # Both load-bearing pieces verified against real Tika 4.2.0:
    # text-recognizers (image OCR) and the default-parser entry.
    assert config["text-recognizers"] == [{"engine": "tesseract"}]
    parsers = config["parsers"]
    assert {"default-parser": {}} in parsers
    pdf = next(p["pdf-parser"] for p in parsers if "pdf-parser" in p)
    assert pdf["pages"]["text"] == "auto"
    assert pdf["pages"]["render"]["dpi"] == 300
    ocr_parser = config["engines"]["tesseract"]["tesseract-ocr-parser"]
    assert ocr_parser["language"] == "eng"
    # Timeouts were removed: Tika requires a timeoutMillis value,
    # so it is set to 24h — effectively no cap.
    assert ocr_parser["timeoutMillis"] == 86400000


def test_ocr_disabled_runs_without_config(sample_file, fake_jar, monkeypatch):
    capture = {}
    extractor = TikaExtractor(config={"jar_path": str(fake_jar),
                                      "ocr_enabled": False})
    _stub_cli(extractor, monkeypatch, capture=capture)
    result = extractor.extract(sample_file)
    assert result.success is True
    argv = capture["argv"]
    assert not any(a.startswith("--config=") for a in argv)
    assert argv[3] == "--text"
    assert result.metadata["ocr_enabled"] is False
    assert result.metadata["ocr_used"] is False


def test_tesseract_path_and_tessdata_are_paired(sample_file, tmp_path, monkeypatch):
    """With an explicit install layout, the generated config carries
    tesseractPath AND tessdataPath (Tika looks for data under
    <tesseractPath>/tessdata once tesseractPath is set)."""
    tess_root = tmp_path / "Tesseract-OCR"
    (tess_root / "tessdata").mkdir(parents=True)
    (tess_root / "tesseract.exe").write_bytes(b"fake")
    (tess_root / "tessdata" / "eng.traineddata").write_bytes(b"fake")
    capture = {}
    extractor = TikaExtractor(config={
        "jar_path": str(tmp_path / "tika-app-4.2.0.jar"),
        "tesseract_path": str(tess_root / "tesseract.exe"),  # exe form accepted
    })
    (tmp_path / "tika-app-4.2.0.jar").write_bytes(b"fake")
    _stub_cli(extractor, monkeypatch, capture=capture)
    assert extractor.extract(sample_file).success is True
    ocr_parser = capture["config"]["engines"]["tesseract"]["tesseract-ocr-parser"]
    assert ocr_parser["tesseractPath"] == str(tess_root)
    assert ocr_parser["tessdataPath"] == str(tess_root / "tessdata")


def test_tesseract_not_found_is_a_clear_diagnostic(sample_file, fake_jar, monkeypatch):
    monkeypatch.setattr(tika_module.shutil, "which", lambda _: None)
    monkeypatch.delenv("TESSERACT_PATH", raising=False)
    extractor = TikaExtractor(config={"jar_path": str(fake_jar)})
    monkeypatch.setattr(extractor, "_resolve_tesseract_dir", lambda: None)
    capture = {}
    _stub_cli(extractor, monkeypatch, completed=_completed(stdout=""),
              capture=capture)
    result = extractor.extract(sample_file)
    # Empty output stays a structured failure (no crash)...
    assert result.success is False
    # ...and with text returned, the OCR-unavailable diagnostic is
    # recorded as a warning on the attempt.
    _stub_cli(extractor, monkeypatch)
    result = extractor.extract(sample_file)
    assert result.success is True
    assert any("Tesseract was not found" in w for w in result.warnings)
    assert result.metadata["ocr_enabled"] is True
    assert result.metadata["ocr_tesseract_path"] is None


def test_external_tika_config_is_used_as_is(sample_file, fake_jar, tmp_path, monkeypatch):
    external = tmp_path / "tika-config.json"
    external.write_text('{"engines": {}}', encoding="utf-8")
    capture = {}
    extractor = TikaExtractor(config={"jar_path": str(fake_jar),
                                      "tika_config_path": str(external)})
    _stub_cli(extractor, monkeypatch, capture=capture)
    assert extractor.extract(sample_file).success is True
    assert f"--config={external}" in capture["argv"]
    assert external.exists(), "an external config must never be deleted"


def test_missing_external_config_warns_and_continues(sample_file, fake_jar,
                                                    tmp_path, monkeypatch):
    capture = {}
    extractor = TikaExtractor(config={
        "jar_path": str(fake_jar),
        "tika_config_path": str(tmp_path / "does-not-exist.json")})
    _stub_cli(extractor, monkeypatch, capture=capture)
    result = extractor.extract(sample_file)
    assert result.success is True
    assert not any(a.startswith("--config=") for a in capture["argv"])
    assert any("not found" in w for w in result.warnings)


def test_ocr_metadata_for_image_and_pdf_inputs(tmp_path, fake_jar, monkeypatch):
    extractor = TikaExtractor(config={"jar_path": str(fake_jar)})
    _stub_cli(extractor, monkeypatch)
    png = tmp_path / "resume.png"
    png.write_bytes(b"\x89PNG-fake")
    result = extractor.extract(png)
    assert result.success is True
    # For a raster image, extracted text can only be OCR output.
    assert result.metadata["ocr_used"] is True
    assert "OCR" in result.metadata["ocr_evidence"]
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-fake")
    result = extractor.extract(pdf)
    assert result.success is True
    # For a PDF, per-page OCR use is not evidenced by --text output:
    # it is recorded as unknown, never claimed.
    assert result.metadata["ocr_used"] is None
    assert result.metadata["ocr_enabled"] is True
