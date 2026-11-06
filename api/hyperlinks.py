"""Deterministic PDF hyperlink (annotation) extraction.

Clickable links in a PDF live in the annotation layer — the URL is
NOT part of the page's text, so Tika, PyMuPDF text extraction,
Docling and OCR never see it (a resume can show "Portfolio" while
the real URL exists only as an annotation). This module reads that
layer directly with PyMuPDF and returns the actual annotations:

    {"page": 1, "url": "https://…", "visible_text": "Portfolio",
     "type": "uri", "rect": [x0, y0, x1, y1]}

Nothing here guesses: arbitrary visible text is never treated as a
hyperlink, and URLs are preserved byte-for-byte as stored in the
file. Extraction failures are returned as an error string with an
empty list — the pipeline records the failure and continues.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def extract_pdf_hyperlinks(path: str | Path) -> dict[str, Any]:
    """Return {"hyperlinks": [...], "error": str | None} for a PDF.

    Non-PDF files and PDFs without annotations return an empty
    list with no error. Duplicate annotations for the same
    (page, URL, visible text) are collapsed; every distinct
    annotation is kept — nothing is silently dropped.
    """
    path = Path(path)
    if path.suffix.lower() != ".pdf":
        return {"hyperlinks": [], "error": None}
    try:
        import fitz
    except Exception as exc:  # PyMuPDF missing — record, don't crash
        return {"hyperlinks": [],
                "error": f"PyMuPDF unavailable for hyperlink "
                         f"extraction: {exc}"}
    hyperlinks: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    try:
        with fitz.open(path) as document:
            for page_index, page in enumerate(document):
                for link in page.get_links():
                    uri = link.get("uri")
                    if not uri:
                        continue  # internal/GoTo links carry no URL
                    rect = link.get("from")
                    visible_text = ""
                    if rect is not None:
                        try:
                            visible_text = " ".join(
                                page.get_text(
                                    "text", clip=rect).split())
                        except Exception:
                            visible_text = ""
                    entry = {
                        "page": page_index + 1,
                        "url": uri,
                        "visible_text": visible_text,
                        "type": ("mailto" if uri.lower().startswith(
                            "mailto:") else "uri"),
                        "rect": ([round(float(v), 2) for v in (
                            rect.x0, rect.y0, rect.x1, rect.y1)]
                            if rect is not None else None),
                    }
                    key = (entry["page"], entry["url"],
                           entry["visible_text"])
                    if key in seen:
                        continue
                    seen.add(key)
                    hyperlinks.append(entry)
    except Exception as exc:
        logger.warning("Hyperlink extraction failed for %s: %s",
                       path, exc)
        return {"hyperlinks": [],
                "error": f"Hyperlink extraction failed: {exc}"}
    return {"hyperlinks": hyperlinks, "error": None}
