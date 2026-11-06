"""Standard extraction result models (Pydantic v2).

Every extractor — Tika, PyMuPDF, Docling, OCR — and the
ExtractionManager return exactly this model. There are no
extractor-specific return formats.

``success=False`` always carries an ``error`` message, so an extraction
*failure* is clearly distinguishable from a *successful* extraction of
a document that simply contains little or no text (``success=True``
with empty/near-empty ``text`` and an explanatory warning).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AttemptRecord(BaseModel):
    """One extractor attempt, as recorded by the ExtractionManager.

    Every attempt is preserved in full — including the actual text
    that extractor produced (``text``), even when the attempt failed
    the acceptance policy. An attempt's text is never overwritten by
    a later extractor's output.
    """

    extractor_name: str
    success: bool
    accepted: bool = False
    error: str | None = Field(
        default=None,
        description="Failure or rejection reason for this attempt, if any.",
    )
    duration_ms: float = 0.0
    text: str = Field(
        default="",
        description="The full text THIS extractor produced on this attempt "
        "(empty when extraction itself failed). Preserved per attempt so "
        "every tool's output stays inspectable after the run.",
    )
    extractor_version: str | None = Field(
        default=None,
        description="Version of the tool that ran (e.g. '4.2.0' for Tika), "
        "when the extractor reports one.",
    )
    started_at: str | None = Field(
        default=None, description="ISO-8601 UTC start of this attempt.")
    completed_at: str | None = Field(
        default=None, description="ISO-8601 UTC end of this attempt.")
    error_type: str | None = Field(
        default=None,
        description="Exception class name behind a failed attempt, if any.",
    )
    error_traceback: str | None = Field(
        default=None,
        description="Traceback for a failed attempt, when one was captured.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="The extractor result's metadata for this attempt "
        "(e.g. Tika's JAR/version and OCR facts), preserved per attempt.",
    )


class ExtractionResult(BaseModel):
    """The one standard result returned by every extractor."""

    text: str = Field(default="", description="Extracted text. Never mutated after creation by the manager.")
    extractor_name: str = Field(..., description="Name of the extractor that produced this result.")
    success: bool = Field(..., description="True only if extraction itself succeeded.")
    file_type: str | None = Field(
        default=None, description="Detected file type, normally the lowercase extension, e.g. 'pdf'."
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Basic document metadata, if available.")
    page_count: int | None = Field(default=None, description="Number of pages, if known.")
    page_texts: list[str] = Field(
        default_factory=list, description="Per-page text, in page order, where the extractor provides it."
    )
    warnings: list[str] = Field(default_factory=list, description="Non-fatal issues encountered.")
    error: str | None = Field(default=None, description="Fatal error message when success is False.")
    error_type: str | None = Field(
        default=None,
        description="Exception class name behind a failure (e.g. "
        "'ExtractorUnavailableError'), set by BaseExtractor when the "
        "failure came from an exception.",
    )
    error_traceback: str | None = Field(
        default=None,
        description="Traceback captured for a failure, when available.",
    )
    duration_ms: float = Field(default=0.0, description="Wall-clock duration of this extraction attempt.")
    attempts: list[AttemptRecord] = Field(
        default_factory=list,
        description="Attempt history. Empty for a single extractor; "
        "populated by the ExtractionManager with every attempt and its failure reason.",
    )

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def char_count(self) -> int:
        return len(self.text)
