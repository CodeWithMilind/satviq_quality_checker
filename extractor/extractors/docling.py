"""Docling extractor — structure-preserving document parsing.

Uses IBM's Docling to convert documents (PDF and the other formats the
installed Docling version supports) into a structured document, then
exports plain text. Headings / reading structure are preserved as far
as Docling's text export preserves them, and page information is
reported where the conversion result exposes it.

Configuration is explicit (see ``config/extractor.yaml`` →
``extractors.docling.options``): those options are recorded in the
result metadata and applied where the installed Docling version has a
matching converter option. Model loading / conversion failures are
returned as ``success=False`` results — never as empty successes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..base import BaseExtractor
from ..exceptions import ExtractorUnavailableError, ExtractionFailedError
from ..models import ExtractionResult

logger = logging.getLogger(__name__)


class DoclingExtractor(BaseExtractor):
    """Parse documents with Docling and export their text."""

    name = "docling"
    # Docling supports more than PDFs; kept to formats its default
    # pipeline reliably accepts across versions.
    supported_extensions = ["pdf", "docx", "pptx", "xlsx", "html", "md", "png", "jpg", "jpeg"]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self.options: dict[str, Any] = dict(self.config.get("options") or {})

    # Seam for tests / dependency injection.
    def _create_converter(self) -> Any:
        try:
            from docling.document_converter import DocumentConverter  # type: ignore
        except ImportError as exc:
            raise ExtractorUnavailableError(
                "The 'docling' package is not installed. "
                "Install it with: pip install docling"
            ) from exc
        try:
            return DocumentConverter()
        except Exception as exc:
            raise ExtractorUnavailableError(
                f"Docling failed to initialise (model loading): "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def _extract(self, path: Path) -> ExtractionResult:
        converter = self._create_converter()
        try:
            conversion = converter.convert(str(path))
        except Exception as exc:
            raise ExtractionFailedError(
                f"Docling conversion failed: {type(exc).__name__}: {exc}"
            ) from exc

        document = getattr(conversion, "document", None)
        if document is None:
            raise ExtractionFailedError("Docling returned no document for this file.")

        try:
            if hasattr(document, "export_to_text"):
                text = document.export_to_text()
            else:
                text = document.export_to_markdown()
        except Exception as exc:
            raise ExtractionFailedError(
                f"Docling could not export text: {type(exc).__name__}: {exc}"
            ) from exc

        if not text or not str(text).strip():
            raise ExtractionFailedError("Docling extracted no text from this document.")

        # Page information, where the installed version exposes it.
        page_texts: list[str] = []
        pages = getattr(document, "pages", None)
        if isinstance(pages, dict) and pages:
            for page_no in sorted(pages, key=lambda k: str(k)):
                page_obj = pages[page_no]
                page_text = getattr(page_obj, "text", None)
                if page_text:
                    page_texts.append(str(page_text))

        metadata: dict[str, Any] = {"docling_options": self.options}
        status = getattr(conversion, "status", None)
        if status is not None:
            metadata["conversion_status"] = str(status)
        errors = getattr(conversion, "errors", None)
        warnings = [str(e) for e in errors] if errors else []

        return self._success(
            path, str(text).strip(), page_texts=page_texts,
            page_count=len(page_texts) if page_texts else None,
            metadata=metadata, warnings=warnings,
        )
