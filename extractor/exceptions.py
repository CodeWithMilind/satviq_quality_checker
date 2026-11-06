"""Exceptions for the SATVIQ Document Extractor Engine.

Extractors convert these (and library exceptions) into an
``ExtractionResult(success=False, error=...)`` instead of letting them
crash the pipeline; the ExtractionManager also catches any exception an
extractor raises and records it as a failed attempt. The classes exist
so callers and custom extractors can raise / catch precise failures.
"""


class ExtractorError(Exception):
    """Base class for all extraction errors."""


class InvalidFileError(ExtractorError):
    """The input path is missing, is not a file, or cannot be read."""


class UnsupportedFileTypeError(ExtractorError):
    """This extractor does not support the given file type."""


class ExtractorUnavailableError(ExtractorError):
    """The extractor's library, server, or binary is not available.

    Examples: the ``tika`` package or Tika server is unreachable, the
    ``docling`` package is not installed, or the ``tesseract`` binary
    is not on PATH. Availability is never faked.
    """


class ExtractionFailedError(ExtractorError):
    """Extraction ran but failed (corrupt file, parse error, empty result)."""


class EncryptedDocumentError(ExtractionFailedError):
    """The document is encrypted / password-protected and cannot be opened."""


class ExtractionTimeoutError(ExtractorError):
    """An extractor or the whole manager run exceeded its time budget."""
