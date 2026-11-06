"""Quality Checker orchestrator.

Runs the three independent filters — Text Health, Completeness and
Reading Order — over extracted resume text and returns a structured
:class:`QualityReport`. It deliberately does NOT:

- extract text (no Tika / PyMuPDF / Docling / OCR),
- aggregate the three scores into an overall score,
- make acceptance / rejection decisions,
- route fallbacks, run LLM/SLM recovery, or match resumes to JDs.

The original extracted text is never mutated.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .filters.completeness import CompletenessFilter
from .filters.reading_order import ReadingOrderFilter
from .filters.text_health import TextHealthFilter
from .models import QualityReport

logger = logging.getLogger(__name__)


class QualityChecker:
    """Evaluate extracted resume text with the three quality filters.

    Parameters allow tests / callers to inject per-filter configs or
    config file paths; by default the bundled YAML files are used.
    """

    def __init__(
        self,
        text_health_config: dict[str, Any] | None = None,
        completeness_config: dict[str, Any] | None = None,
        reading_order_config: dict[str, Any] | None = None,
        text_health_config_path: str | Path | None = None,
        completeness_config_path: str | Path | None = None,
        reading_order_config_path: str | Path | None = None,
    ) -> None:
        self.text_health_filter = TextHealthFilter(
            config=text_health_config, config_path=text_health_config_path
        )
        self.completeness_filter = CompletenessFilter(
            config=completeness_config, config_path=completeness_config_path
        )
        self.reading_order_filter = ReadingOrderFilter(
            config=reading_order_config, config_path=reading_order_config_path
        )

    def check(
        self,
        text: object,
        *,
        document_id: str,
        source_tool: str,
        attempt_id: str,
    ) -> QualityReport:
        """Run all three filters and return the quality report.

        Invalid (non-string) input is handled safely: every filter
        returns a zero-score result with an ``INVALID_INPUT`` flag
        instead of raising.
        """
        logger.info(
            "Quality check started document_id=%s source_tool=%s attempt_id=%s",
            document_id, source_tool, attempt_id,
        )
        text_health = self.text_health_filter.evaluate(text)
        completeness = self.completeness_filter.evaluate(
            text, extraction_quality=text_health.score if isinstance(text, str) else None
        )
        reading_order = self.reading_order_filter.evaluate(text)

        report = QualityReport(
            document_id=document_id,
            source_tool=source_tool,
            attempt_id=attempt_id,
            text_health=text_health,
            completeness=completeness,
            reading_order=reading_order,
        )
        logger.info(
            "Quality check finished document_id=%s attempt_id=%s "
            "text_health=%.2f completeness=%.2f reading_order=%.2f",
            document_id, attempt_id,
            text_health.score, completeness.score, reading_order.score,
        )
        return report


def check_quality(
    text: object,
    *,
    document_id: str,
    source_tool: str,
    attempt_id: str,
    checker: QualityChecker | None = None,
) -> QualityReport:
    """One-call convenience wrapper around :class:`QualityChecker`."""
    return (checker or QualityChecker()).check(
        text, document_id=document_id, source_tool=source_tool, attempt_id=attempt_id
    )
