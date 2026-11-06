"""Shared Pydantic v2 result models for the SATVIQ Quality Checker.

Every filter returns a :class:`FilterResult`. The Completeness filter
returns the subclass :class:`CompletenessFilterResult`, which carries
the detected / missing section lists in typed fields (they are also
mirrored into ``metrics`` for generic consumers).

The three scores are deliberately *not* aggregated into an overall
score — that decision belongs to the caller, not to this module.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class FilterResult(BaseModel):
    """Result of a single, independent quality filter."""

    filter_name: str = Field(..., description="Stable name of the filter, e.g. 'text_health'.")
    score: float = Field(..., description="Score from 0 to 100 for this filter only.")
    flags: list[str] = Field(default_factory=list, description="Machine-readable issue flags.")
    reasons: list[str] = Field(default_factory=list, description="Human-readable explanations.")
    metrics: dict[str, Any] = Field(default_factory=dict, description="Raw metrics behind the score.")

    @field_validator("score")
    @classmethod
    def _clamp_score(cls, value: float) -> float:
        return round(max(0.0, min(100.0, float(value))), 2)


class CompletenessFilterResult(FilterResult):
    """FilterResult plus typed section information for Completeness."""

    filter_name: str = "completeness"
    detected_sections: list[str] = Field(
        default_factory=list, description="Display names of sections that were detected."
    )
    missing_sections: list[str] = Field(
        default_factory=list, description="Display names of sections that were not detected."
    )
    missing_required: list[str] = Field(default_factory=list)
    missing_recommended: list[str] = Field(default_factory=list)
    missing_optional: list[str] = Field(default_factory=list)


class QualityReport(BaseModel):
    """Structured quality report returned to the caller.

    Each filter result stays independently accessible. There is
    intentionally no overall / aggregated score field.
    """

    document_id: str = Field(..., description="Caller-supplied document identifier.")
    source_tool: str = Field(
        ..., description="Extraction tool that produced the text, e.g. 'pymupdf', 'tika', 'docling', 'ocr'."
    )
    attempt_id: str = Field(..., description="Caller-supplied extraction attempt identifier.")
    text_health: FilterResult
    completeness: CompletenessFilterResult
    reading_order: FilterResult
