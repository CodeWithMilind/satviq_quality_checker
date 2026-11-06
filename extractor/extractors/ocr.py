"""OCR extractor — the final fallback, using Tesseract via pytesseract.

Handles scanned PDFs and image documents. PDF pages are rendered to
images with PyMuPDF at the configured DPI, then OCR'd page by page so
``page_texts`` preserves page boundaries. Image files are OCR'd
directly with Pillow + pytesseract.

If Tesseract is not installed, this extractor reports itself
unavailable (``success=False``, error naming the missing binary) — it
never claims OCR succeeded without Tesseract, and nothing is installed
automatically. Language and DPI are configurable.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from ..base import BaseExtractor
from ..exceptions import ExtractorUnavailableError, ExtractionFailedError
from ..models import ExtractionResult

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = ["png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"]


class OCRExtractor(BaseExtractor):
    """OCR scanned PDFs and images with Tesseract."""

    name = "ocr"
    supported_extensions = ["pdf"] + IMAGE_EXTENSIONS

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self.language: str = str(self.config.get("language", "eng"))
        self.dpi: int = int(self.config.get("dpi", 300))
        self.tesseract_cmd: str | None = self.config.get("tesseract_cmd")

    # -- availability -----------------------------------------------------
    def _tesseract_path(self) -> str | None:
        if self.tesseract_cmd:
            return self.tesseract_cmd if Path(self.tesseract_cmd).exists() else None
        return shutil.which("tesseract")

    def _load_pytesseract(self) -> Any:
        try:
            import pytesseract  # type: ignore
        except ImportError as exc:
            raise ExtractorUnavailableError(
                "The 'pytesseract' package is not installed. "
                "Install it with: pip install pytesseract"
            ) from exc
        if self._tesseract_path() is None:
            raise ExtractorUnavailableError(
                "Tesseract OCR is not installed or not on PATH, so OCR is "
                "unavailable. Install Tesseract separately (this package "
                "never installs system software automatically)."
            )
        if self.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = self.tesseract_cmd
        return pytesseract

    # Seam for tests: OCR one PIL image and return its text.
    def _image_to_text(self, image: Any) -> str:
        pytesseract = self._load_pytesseract()
        try:
            return str(pytesseract.image_to_string(image, lang=self.language))
        except Exception as exc:
            raise ExtractionFailedError(
                f"Tesseract OCR failed on a page/image: {type(exc).__name__}: {exc}"
            ) from exc

    def _render_pdf_pages(self, path: Path) -> list[Any]:
        """Render each PDF page to a PIL image at the configured DPI."""
        try:
            import pymupdf  # type: ignore
            from PIL import Image  # type: ignore
        except ImportError as exc:
            raise ExtractorUnavailableError(
                "OCR of PDFs needs the 'PyMuPDF' and 'Pillow' packages. "
                "Install them with: pip install PyMuPDF Pillow"
            ) from exc
        images: list[Any] = []
        try:
            with pymupdf.open(str(path)) as document:
                if document.needs_pass:
                    raise ExtractionFailedError(
                        "PDF is encrypted / password-protected; OCR cannot open it."
                    )
                zoom = self.dpi / 72.0
                matrix = pymupdf.Matrix(zoom, zoom)
                for page in document:
                    pixmap = page.get_pixmap(matrix=matrix)
                    images.append(
                        Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
                    )
        except ExtractionFailedError:
            raise
        except Exception as exc:
            raise ExtractionFailedError(
                f"Could not render PDF pages for OCR: {type(exc).__name__}: {exc}"
            ) from exc
        return images

    def _load_image(self, path: Path) -> Any:
        try:
            from PIL import Image  # type: ignore
        except ImportError as exc:
            raise ExtractorUnavailableError(
                "OCR of images needs the 'Pillow' package. "
                "Install it with: pip install Pillow"
            ) from exc
        try:
            return Image.open(str(path))
        except Exception as exc:
            raise ExtractionFailedError(
                f"Could not open image file: {type(exc).__name__}: {exc}"
            ) from exc

    # -- extraction ---------------------------------------------------------
    def _extract(self, path: Path) -> ExtractionResult:
        # Fail fast and honestly when Tesseract is unavailable.
        self._load_pytesseract()

        file_type = self.detect_file_type(path)
        if file_type == "pdf":
            images = self._render_pdf_pages(path)
            if not images:
                raise ExtractionFailedError("PDF has no pages to OCR.")
            page_texts = [self._image_to_text(image).strip() for image in images]
        else:
            page_texts = [self._image_to_text(self._load_image(path)).strip()]

        full_text = "\n".join(t for t in page_texts).strip()
        if not full_text:
            raise ExtractionFailedError(
                "OCR completed but recognised no text in this document."
            )

        metadata = {"ocr_language": self.language, "ocr_dpi": self.dpi,
                    "tesseract_path": self._tesseract_path()}
        warnings: list[str] = []
        empty_pages = [i + 1 for i, t in enumerate(page_texts) if not t]
        if empty_pages:
            warnings.append(f"OCR recognised no text on page(s): {empty_pages}.")

        return self._success(path, full_text, page_texts=page_texts,
                             metadata=metadata, warnings=warnings)
