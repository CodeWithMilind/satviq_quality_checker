"""Common extractor interface.

All extractors implement :class:`BaseExtractor` and return the same
:class:`~extractor.models.ExtractionResult`. Shared behaviour — input
validation, file-type detection, timing, failure-result construction
and logging — lives here so each concrete extractor only contains the
logic specific to its library.

Contract for concrete extractors:
- Implement ``_extract(path)`` and return an ``ExtractionResult``.
- On failure, return ``self._failure(path, message)`` (or raise an
  exception from ``extractor.exceptions`` — ``extract()`` converts it
  into a failure result). Never return silent empty success for a
  failure, and never fabricate text.
- Never log document contents.
"""

from __future__ import annotations

import logging
import time
import traceback
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .exceptions import ExtractorError, InvalidFileError, UnsupportedFileTypeError
from .models import ExtractionResult

logger = logging.getLogger(__name__)


class BaseExtractor(ABC):
    """Abstract base class for all document extractors."""

    #: Stable extractor name, e.g. "tika". Set by each subclass.
    name: str = "base"

    #: Extensions (without dot, lowercase) this extractor supports.
    #: Empty list = no extension restriction (used by Tika).
    supported_extensions: list[str] = []

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = config or {}
        self.timeout_seconds: float | None = self.config.get("timeout_seconds")

    # -- public API -----------------------------------------------------
    def extract(self, file_path: str | Path) -> ExtractionResult:
        """Extract text from ``file_path``. Never raises for I/O or
        library failures — those are returned as ``success=False``
        results with ``error`` set."""
        started = time.perf_counter()
        path = Path(file_path) if file_path is not None else None
        logger.info("Extractor %s started: file=%s", self.name,
                    path.name if path is not None else None)
        try:
            if path is None:
                raise InvalidFileError("file_path is None.")
            self._validate_file(path)
            self._validate_type(path)
            result = self._extract(path)
        except ExtractorError as exc:
            result = self._failure(path, str(exc), started)
            result.error_type = type(exc).__name__
            result.error_traceback = traceback.format_exc()
        except Exception as exc:  # library errors must not crash the pipeline
            logger.exception("Extractor %s raised unexpectedly", self.name)
            result = self._failure(path, f"{type(exc).__name__}: {exc}", started)
            result.error_type = type(exc).__name__
            result.error_traceback = traceback.format_exc()

        if result.duration_ms == 0.0:
            result.duration_ms = round((time.perf_counter() - started) * 1000, 2)
        if result.success:
            logger.info("Extractor %s completed: file=%s chars=%d duration_ms=%.1f",
                        self.name, path.name if path else None,
                        len(result.text), result.duration_ms)
        else:
            logger.warning("Extractor %s failed: file=%s error=%s",
                           self.name, path.name if path else None, result.error)
        return result

    # -- to be implemented by subclasses --------------------------------
    @abstractmethod
    def _extract(self, path: Path) -> ExtractionResult:
        raise NotImplementedError

    # -- shared helpers ---------------------------------------------------
    @staticmethod
    def detect_file_type(path: Path) -> str | None:
        suffix = path.suffix.lower().lstrip(".")
        return suffix or None

    def _validate_file(self, path: Path) -> None:
        if not path.exists():
            raise InvalidFileError(f"File not found: {path}")
        if not path.is_file():
            raise InvalidFileError(f"Path is not a file: {path}")

    def _validate_type(self, path: Path) -> None:
        if not self.supported_extensions:
            return
        file_type = self.detect_file_type(path)
        if file_type not in self.supported_extensions:
            raise UnsupportedFileTypeError(
                f"{self.name} does not support file type '{file_type}'. "
                f"Supported: {', '.join(self.supported_extensions)}."
            )

    def _result(self, path: Path | None, **kwargs: Any) -> ExtractionResult:
        kwargs.setdefault("extractor_name", self.name)
        if path is not None:
            kwargs.setdefault("file_type", self.detect_file_type(path))
        return ExtractionResult(**kwargs)

    def _failure(self, path: Path | None, message: str,
                 started: float | None = None) -> ExtractionResult:
        duration = round((time.perf_counter() - started) * 1000, 2) if started else 0.0
        return self._result(path, success=False, text="", error=message,
                            duration_ms=duration)

    def _success(self, path: Path, text: str, *, page_texts: list[str] | None = None,
                 page_count: int | None = None, metadata: dict[str, Any] | None = None,
                 warnings: list[str] | None = None) -> ExtractionResult:
        return self._result(
            path, success=True, text=text,
            page_texts=page_texts or [],
            page_count=page_count if page_count is not None
            else (len(page_texts) if page_texts else None),
            metadata=metadata or {}, warnings=warnings or [],
        )
