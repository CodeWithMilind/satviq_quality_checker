"""Apache Tika extractor — the primary, general-purpose extractor.

Runs the locally installed **Apache Tika 4.2.0 application JAR**
(``tika-app-4.2.0.jar``) as a Java subprocess and captures the plain
text it prints to stdout::

    java -jar <tika-app.jar> [--config=<tika-config.json>] --text <document>

There is deliberately **no Tika server and no Python tika client**:
no REST endpoint, no ``pip install tika``, nothing to keep running.
Each extraction is one short-lived Java process.

OCR (Tesseract) integration
---------------------------
OCR runs *inside* Tika — there is no separate OCR attempt. When OCR
is enabled (the default), the extractor generates a Tika 4 JSON
configuration for the run that:

- registers the Tesseract text-recognizer engine
  (``engines.tesseract.tesseract-ocr-parser`` with the resolved
  Tesseract install directory, language and timeout), and
- sets the PDF parser's page-text strategy (``pages.text``, default
  ``auto``: the text layer is used when present, rendered pages are
  OCR'd when a page has little or no extractable text).

Scanned/image-only PDFs and raster images (PNG/JPG/TIFF/…) are
therefore OCR'd by Tika itself, while normal text PDFs keep using
their text layer. An externally managed config can be used instead
via ``tika_config_path`` / ``TIKA_CONFIG_PATH`` (e.g. the
``tika-config.json`` of a Tika Server installation — the format is
the same).

What the attempt record claims about OCR is evidence-based: for
raster-image inputs a successful Tika extraction can only have come
from OCR, so ``ocr_used`` is recorded as true with that reason; for
PDFs the ``--text`` CLI output carries no per-page OCR marker, so
``ocr_used`` is recorded as unknown with the configured strategy —
OCR is never claimed for a PDF page without evidence.

Configuration (``extractor/config/extractor.yaml`` → ``extractors.tika``
or programmatic config):

- ``jar_path`` — path to ``tika-app-*.jar``. Resolution order:
  explicit config → ``TIKA_JAR_PATH`` environment variable →
  ``TIKA_APP_JAR`` environment variable → platform default
  (``C:\\Apache\\Tika\\tika-app-4.2.0.jar`` on Windows; unset on
  Linux/macOS, where the environment variable is the supported way).
- ``java_bin`` — the Java executable (config → ``JAVA_BIN`` env →
  ``"java"`` from PATH). Tika 4.x requires Java 17+.
- ``timeout_seconds`` — hard cap for the Java process.
- ``ocr_enabled`` — default ``true``. When false, no ``--config`` is
  generated and Tika runs with its built-in defaults.
- ``ocr_language`` — Tesseract language (config → ``TIKA_OCR_LANGUAGE``
  env → ``"eng"``).
- ``ocr_strategy`` — PDF page-text strategy: ``auto`` (default),
  ``ocr_only``, ``ocr_and_text`` or ``no_ocr``.
- ``ocr_dpi`` — render DPI for OCR'd PDF pages (default 300).
- ``ocr_timeout_seconds`` — Tesseract's own per-call timeout
  (default 120).
- ``tesseract_path`` — the Tesseract executable *or its install
  directory* (config → ``TESSERACT_PATH`` env → PATH lookup →
  Windows default ``C:\\Program Files\\Tesseract-OCR``). Tika's
  ``tesseractPath`` setting itself takes the **directory** containing
  the executable; the extractor resolves and validates it.
- ``tessdata_path`` — optional explicit ``tessdata`` directory
  (config → ``TESSDATA_PATH`` env). Only needed when the language
  data does not live in Tesseract's default location.
- ``tika_config_path`` — use an existing Tika JSON config as-is
  instead of generating one (config → ``TIKA_CONFIG_PATH`` env).

Supports every format Tika supports: PDF, DOCX, DOC, PPTX, XLSX, TXT,
HTML, and hundreds more, so this extractor imposes no extension
restriction unless one is set in configuration
(``supported_extensions``).

Failure behaviour: a missing JAR, a missing Java runtime, a non-zero
Tika exit code, a timeout, or empty output all produce
``success=False`` with a precise error (the exception type and, for
unexpected errors, a traceback are preserved on the result by
``BaseExtractor``) — never a silent empty success, and never a
started-but-unmanaged server.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..base import BaseExtractor
from ..exceptions import ExtractorUnavailableError, ExtractionFailedError
from ..models import ExtractionResult

logger = logging.getLogger(__name__)

#: Default install location on Windows (Milind's machine:
#: C:\Apache\Tika\tika-app-4.2.0.jar, with lib/ and plugins/ beside it).
WINDOWS_DEFAULT_JAR = r"C:\Apache\Tika\tika-app-4.2.0.jar"

#: Default Tesseract install directory on Windows (the executable
#: itself is C:\Program Files\Tesseract-OCR\tesseract.exe).
WINDOWS_DEFAULT_TESSERACT_DIR = r"C:\Program Files\Tesseract-OCR"

#: Raster image types: for these, any text Tika returns can only have
#: been produced by OCR — Tika has no text layer to read in an image.
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "tif", "tiff", "bmp", "gif",
                    "webp", "pnm", "pbm", "ppm"}


def _tesseract_exe_name() -> str:
    return "tesseract.exe" if os.name == "nt" else "tesseract"

_VERSION_RE = re.compile(r"tika-app-(\d+\.\d+(?:\.\d+)?)", re.IGNORECASE)


def _default_jar_path() -> str | None:
    """Platform default JAR location (Windows only — on Linux the
    TIKA_JAR_PATH environment variable is the deployment mechanism)."""
    if os.name == "nt":
        return WINDOWS_DEFAULT_JAR
    return None


class TikaExtractor(BaseExtractor):
    """Extract text via the local Apache Tika application JAR (CLI)."""

    name = "tika"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        configured = self.config.get("jar_path") or \
            os.environ.get("TIKA_JAR_PATH") or \
            os.environ.get("TIKA_APP_JAR") or \
            _default_jar_path()
        self.jar_path: str | None = str(configured) if configured else None
        self.java_bin: str = str(
            self.config.get("java_bin") or os.environ.get("JAVA_BIN") or "java")
        # --- OCR (Tesseract) settings --------------------------------
        self.ocr_enabled: bool = bool(self.config.get("ocr_enabled", True))
        self.ocr_language: str = str(
            self.config.get("ocr_language")
            or os.environ.get("TIKA_OCR_LANGUAGE") or "eng")
        self.ocr_strategy: str = str(
            self.config.get("ocr_strategy") or "auto").lower()
        self.ocr_dpi: int = int(self.config.get("ocr_dpi") or 300)
        self.ocr_timeout_seconds: float = float(
            self.config.get("ocr_timeout_seconds") or 120)
        tesseract = self.config.get("tesseract_path") or \
            os.environ.get("TESSERACT_PATH")
        self.tesseract_path: str | None = str(tesseract) if tesseract else None
        tessdata = self.config.get("tessdata_path") or \
            os.environ.get("TESSDATA_PATH")
        self.tessdata_path: str | None = str(tessdata) if tessdata else None
        tika_config = self.config.get("tika_config_path") or \
            os.environ.get("TIKA_CONFIG_PATH")
        self.tika_config_path: str | None = \
            str(tika_config) if tika_config else None
        configured_exts = self.config.get("supported_extensions") or []
        # Instance-level override only when config explicitly restricts it.
        self.supported_extensions = [str(e).lower().lstrip(".") for e in configured_exts]

    @property
    def version(self) -> str | None:
        """Tika version parsed from the JAR filename
        (``tika-app-4.2.0.jar`` → ``"4.2.0"``); None when the
        configured filename carries no version."""
        if not self.jar_path:
            return None
        match = _VERSION_RE.search(Path(self.jar_path).name)
        return match.group(1) if match else None

    # -- internals ------------------------------------------------------

    def _require_jar(self) -> Path:
        if not self.jar_path:
            raise ExtractorUnavailableError(
                "Apache Tika JAR path is not configured. Set the "
                "TIKA_JAR_PATH environment variable to the full path of "
                "tika-app-4.2.0.jar (on Windows the default "
                r"C:\Apache\Tika\tika-app-4.2.0.jar is used when present).")
        jar = Path(self.jar_path)
        if not jar.is_file():
            raise ExtractorUnavailableError(
                f"Apache Tika JAR not found at the configured path: "
                f"{self.jar_path}. Install Apache Tika 4.2.0 there, or "
                "point jar_path / the TIKA_JAR_PATH environment variable "
                "at your tika-app-4.2.0.jar.")
        return jar

    # -- OCR (Tesseract) --------------------------------------------------

    def _resolve_tesseract_dir(self) -> Path | None:
        """Locate the directory containing the Tesseract executable.

        Accepts a configured executable path or install directory.
        Resolution: ``tesseract_path`` config / ``TESSERACT_PATH`` env
        → ``tesseract`` on PATH → Windows default install folder.
        Returns None when Tesseract cannot be found anywhere.
        """
        candidates: list[Path] = []
        if self.tesseract_path:
            candidates.append(Path(self.tesseract_path))
        on_path = shutil.which("tesseract")
        if on_path:
            candidates.append(Path(on_path))
        if os.name == "nt":
            candidates.append(Path(WINDOWS_DEFAULT_TESSERACT_DIR))
        for candidate in candidates:
            exe = candidate if candidate.is_file() \
                else candidate / _tesseract_exe_name()
            if exe.is_file():
                return exe.parent
        return None

    def _resolve_tessdata_dir(self, tesseract_dir: Path | None,
                              ) -> Path | None:
        """Locate the ``tessdata`` directory holding the configured
        language's ``<lang>.traineddata``.

        This matters because of a Tika/Tesseract interaction verified
        against Tika 4.2.0: as soon as ``tesseractPath`` is set in the
        config, Tesseract looks for its language data under
        ``<tesseractPath>/tessdata`` — on a layout where the data
        lives elsewhere, OCR then fails hard ("Error opening data
        file .../tessdata/eng.traineddata") and takes the whole
        extraction down with it. So ``tesseractPath`` is only written
        into the generated config together with a resolved
        ``tessdataPath``.
        """
        trained = f"{self.ocr_language}.traineddata"
        if self.tessdata_path:
            configured = Path(self.tessdata_path)
            return configured if (configured / trained).is_file() else None
        candidates: list[Path] = []
        if tesseract_dir is not None:
            candidates.append(tesseract_dir / "tessdata")
        candidates += [
            Path("/usr/share/tesseract-ocr/5/tessdata"),
            Path("/usr/share/tesseract-ocr/4.00/tessdata"),
            Path("/usr/share/tessdata"),
        ]
        for candidate in candidates:
            if (candidate / trained).is_file():
                return candidate
        return None

    def _build_tika_config(self, tesseract_dir: Path | None,
                           ) -> tuple[Path | None, bool, list[str]]:
        """Return ``(config_path, is_generated, warnings)`` for the run.

        An externally managed config (``tika_config_path``) is used
        as-is and never deleted by us. Otherwise a Tika 4 JSON config
        is generated into a temp file (the caller deletes it): PDF
        page-text strategy + the Tesseract engine registration. The
        structure follows Tika 4's own config converter — note the
        ``text-recognizers`` key is REQUIRED for raster-image OCR;
        without it the engine registration is ignored for images.
        """
        warnings: list[str] = []
        if self.tika_config_path:
            external = Path(self.tika_config_path)
            if external.is_file():
                return external, False, warnings
            warnings.append(
                f"Configured Tika config file not found: "
                f"{self.tika_config_path}. Continuing without it — "
                "OCR settings from that file will not apply.")
            logger.warning("Tika OCR: configured tika_config_path %s "
                           "not found", self.tika_config_path)
            return None, False, warnings
        ocr_parser: dict[str, Any] = {
            "language": self.ocr_language,
            "timeoutMillis": int(self.ocr_timeout_seconds * 1000),
        }
        if tesseract_dir is not None:
            tessdata_dir = self._resolve_tessdata_dir(tesseract_dir)
            if tessdata_dir is not None:
                ocr_parser["tesseractPath"] = str(tesseract_dir)
                ocr_parser["tessdataPath"] = str(tessdata_dir)
            elif self.tesseract_path:
                # Explicitly configured location: honour it, but warn
                # — without a locatable tessdata directory Tesseract
                # may fail to initialize (see _resolve_tessdata_dir).
                ocr_parser["tesseractPath"] = str(tesseract_dir)
                warnings.append(
                    "Tesseract was found at the configured location "
                    f"{tesseract_dir}, but no tessdata directory with "
                    f"{self.ocr_language}.traineddata could be located "
                    "under it — OCR may fail to initialize. Set "
                    "tessdata_path / TESSDATA_PATH if the language data "
                    "lives elsewhere.")
            else:
                # Found on PATH / default folder but its language data
                # could not be located: leave tesseractPath unset so
                # Tesseract uses its own built-in data location (the
                # behaviour Tika's defaults rely on).
                warnings.append(
                    f"Tesseract was found at {tesseract_dir}, but its "
                    "tessdata directory could not be located; leaving "
                    "tesseractPath unset so Tesseract uses its built-in "
                    "language-data location.")
        payload = {
            # The empty "default-parser" entry is load-bearing
            # (verified against Tika 4.2.0): a config whose "parsers"
            # section lists ONLY "pdf-parser" silently breaks OCR of
            # standalone images (empty result, exit 0) — the default
            # parser composition must be kept explicitly.
            "parsers": [
                {"default-parser": {}},
                {"pdf-parser": {"pages": {
                    "text": self.ocr_strategy,
                    "render": {"dpi": self.ocr_dpi}}}},
            ],
            "engines": {"tesseract": {"tesseract-ocr-parser": ocr_parser}},
            "text-recognizers": [{"engine": "tesseract"}],
        }
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix="satviq-tika-config-",
            delete=False, encoding="utf-8")
        try:
            json.dump(payload, handle)
        finally:
            handle.close()
        return Path(handle.name), True, warnings

    def _ocr_metadata(self, path: Path,
                      tesseract_dir: Path | None) -> dict[str, Any]:
        """OCR facts for the attempt record — evidence-based only."""
        if not self.ocr_enabled:
            return {"ocr_enabled": False, "ocr_used": False,
                    "ocr_evidence": "OCR is disabled in configuration; "
                                    "this attempt ran without a Tika "
                                    "OCR configuration."}
        file_type = path.suffix.lower().lstrip(".")
        base: dict[str, Any] = {
            "ocr_enabled": True,
            "ocr_language": self.ocr_language,
            "ocr_strategy": self.ocr_strategy,
            "ocr_tesseract_path": str(tesseract_dir) if tesseract_dir else None,
        }
        if file_type in IMAGE_EXTENSIONS:
            base["ocr_used"] = True
            base["ocr_evidence"] = (
                "Raster image input — Tika's only source of text in an "
                "image is the Tesseract OCR engine, so a successful "
                "extraction is OCR output.")
        else:
            base["ocr_used"] = None
            base["ocr_evidence"] = (
                f"OCR was available to Tika for this attempt (PDF "
                f"page-text strategy '{self.ocr_strategy}'); the "
                "--text CLI output carries no per-page OCR marker, so "
                "whether specific pages were OCR'd is not reported.")
        return base

    def _run_cli(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess:
        """Seam for tests: run the Tika CLI process and return the
        completed process. Java-not-found and timeouts are mapped to
        precise extractor errors here."""
        try:
            return subprocess.run(
                argv, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout)
        except FileNotFoundError as exc:
            raise ExtractorUnavailableError(
                f"Java executable not found ('{self.java_bin}'). Apache "
                "Tika 4.x requires Java 17 or newer on PATH (or set "
                "java_bin / the JAVA_BIN environment variable).") from exc
        except subprocess.TimeoutExpired as exc:
            raise ExtractionFailedError(
                f"Apache Tika CLI timed out after {timeout:.0f}s and was "
                "terminated.") from exc

    def _extract(self, path: Path) -> ExtractionResult:
        jar = self._require_jar()
        timeout = float(self.timeout_seconds or 120)
        warnings: list[str] = []
        argv = [self.java_bin, "-jar", str(jar)]
        tesseract_dir: Path | None = None
        config_file: Path | None = None
        config_generated = False
        if self.ocr_enabled:
            tesseract_dir = self._resolve_tesseract_dir()
            if tesseract_dir is None:
                warnings.append(
                    "OCR is enabled but Tesseract was not found "
                    "(checked the tesseract_path setting, the "
                    "TESSERACT_PATH environment variable, the system "
                    "PATH and the default install folder) — scanned or "
                    "image documents will produce no text in this "
                    "attempt.")
                logger.warning(
                    "Tika OCR: Tesseract executable not found; OCR "
                    "unavailable for this attempt.")
            config_file, config_generated, config_warnings = \
                self._build_tika_config(tesseract_dir)
            warnings.extend(config_warnings)
            if config_file is not None:
                # --config must come before the extraction options.
                argv.append(f"--config={config_file}")
        argv += ["--text", str(path)]
        try:
            completed = self._run_cli(argv, timeout)
        finally:
            if config_generated and config_file is not None:
                config_file.unlink(missing_ok=True)

        stderr = (completed.stderr or "").strip()
        if completed.returncode != 0:
            detail = stderr.splitlines()[-1] if stderr else "no error output"
            raise ExtractionFailedError(
                f"Apache Tika CLI failed with exit code "
                f"{completed.returncode}: {detail}")

        text = (completed.stdout or "").strip()
        if not text:
            # Not silent: an empty Tika result is reported as a failure
            # so the manager can fall back instead of accepting nothing.
            raise ExtractionFailedError(
                "Tika extracted no text from this document (empty result).")

        if stderr and ("ERROR" in stderr or "Exception" in stderr):
            first = next((ln.strip() for ln in stderr.splitlines()
                          if "ERROR" in ln or "Exception" in ln), "")
            if first:
                warnings.append(f"Tika reported: {first[:300]}")

        return self._success(
            path, text, warnings=warnings,
            metadata={"tika_jar": str(jar),
                      "tika_version": self.version,
                      **self._ocr_metadata(path, tesseract_dir)})
