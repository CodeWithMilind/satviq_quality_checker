"""SATVIQ Modular Document Extractor Engine.

Standalone extraction package: Tika (primary) → PyMuPDF → Docling →
OCR (final fallback), behind one standard ExtractionResult model and a
configurable ExtractionManager. Designed to integrate later with the
separate SATVIQ Quality Checker package — which this package does not
import, recreate, or modify.
"""

import logging

from .base import BaseExtractor
from .exceptions import (
    EncryptedDocumentError,
    ExtractionFailedError,
    ExtractionTimeoutError,
    ExtractorError,
    ExtractorUnavailableError,
    InvalidFileError,
    UnsupportedFileTypeError,
)
from .manager import BasicAcceptancePolicy, ExtractionManager, extract_document
from .models import AttemptRecord, ExtractionResult

logging.getLogger(__name__).addHandler(logging.NullHandler())

__version__ = "1.0.0"

__all__ = [
    "AttemptRecord",
    "BaseExtractor",
    "BasicAcceptancePolicy",
    "EncryptedDocumentError",
    "ExtractionFailedError",
    "ExtractionManager",
    "ExtractionResult",
    "ExtractionTimeoutError",
    "ExtractorError",
    "ExtractorUnavailableError",
    "InvalidFileError",
    "UnsupportedFileTypeError",
    "extract_document",
]
