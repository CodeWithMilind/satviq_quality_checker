"""PyMuPDF extractor — fast embedded-text extraction for PDFs.

Preserves page boundaries: each page's text is returned separately in
``page_texts`` (in page order, which is PyMuPDF's reading order) as well
as joined into ``text``.

Handles encrypted PDFs (fails cleanly unless a password is configured),
malformed PDFs (fails with the parse error), and scanned PDFs: a PDF
whose total embedded text is below ``min_meaningful_text_chars`` is
reported as a failure explaining that it likely has no embedded text,
so the manager can fall through to Docling / OCR.

OCR is deliberately NOT implemented here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..base import BaseExtractor
from ..exceptions import (
    EncryptedDocumentError,
    ExtractorUnavailableError,
    ExtractionFailedError,
)
from ..models import ExtractionResult

logger = logging.getLogger(__name__)


class PyMuPDFExtractor(BaseExtractor):
    """Extract embedded text from PDFs with PyMuPDF (``pymupdf``)."""

    name = "pymupdf"
    supported_extensions = ["pdf"]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self.min_meaningful_text_chars: int = int(
            self.config.get("min_meaningful_text_chars", 20)
        )
        self.password: str | None = self.config.get("password")

    def _load_library(self) -> Any:
        try:
            import pymupdf  # type: ignore
        except ImportError as exc:
            raise ExtractorUnavailableError(
                "The 'PyMuPDF' package is not installed. "
                "Install it with: pip install PyMuPDF"
            ) from exc
        return pymupdf

    def _extract(self, path: Path) -> ExtractionResult:
        pymupdf = self._load_library()
        try:
            document = pymupdf.open(str(path))
        except Exception as exc:
            raise ExtractionFailedError(
                f"PyMuPDF could not open this PDF (malformed or unsupported): "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        with document:
            if document.needs_pass:
                authenticated = False
                if self.password:
                    authenticated = bool(document.authenticate(self.password))
                if not authenticated:
                    raise EncryptedDocumentError(
                        "PDF is encrypted / password-protected and could not be "
                        "opened (no password configured, or the password failed)."
                    )
            if document.is_encrypted and not self.password:
                logger.warning("PyMuPDF: document reports encryption for %s", path.name)

            page_texts: list[str] = []
            try:
                for page in document:
                    page_texts.append(page.get_text("text"))
            except Exception as exc:
                raise ExtractionFailedError(
                    f"PyMuPDF failed while reading pages: {type(exc).__name__}: {exc}"
                ) from exc

            metadata = {str(k): v for k, v in (document.metadata or {}).items()
                        if v not in (None, "")}
            page_count = document.page_count

        full_text = "\n".join(t.strip() for t in page_texts).strip()
        meaningful_chars = len(full_text.replace("\n", "").strip())

        if meaningful_chars < self.min_meaningful_text_chars:
            # Distinguishable from a parse failure AND from silent empty
            # success: this PDF opened fine, it just has no embedded text.
            return self._result(
                path, success=False, text=full_text, page_texts=page_texts,
                page_count=page_count, metadata=metadata,
                warnings=["PDF contains no meaningful embedded text; "
                          "it is likely a scanned / image-only PDF."],
                error=("PyMuPDF found no meaningful embedded text "
                       f"({meaningful_chars} chars < {self.min_meaningful_text_chars} "
                       "minimum). This is likely a scanned PDF — OCR is required."),
            )

        return self._success(path, full_text, page_texts=page_texts,
                             page_count=page_count, metadata=metadata)
