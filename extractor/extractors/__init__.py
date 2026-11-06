"""Concrete extractors, one per file, all sharing BaseExtractor."""

from .docling import DoclingExtractor
from .ocr import OCRExtractor
from .pymupdf import PyMuPDFExtractor
from .tika import TikaExtractor

__all__ = [
    "DoclingExtractor",
    "OCRExtractor",
    "PyMuPDFExtractor",
    "TikaExtractor",
]
