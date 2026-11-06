"""SATVIQ Resume Extraction Quality Checker.

Evaluates extracted resume text with three independent, deterministic
filters — Text Health, Completeness, Reading Order — and returns a
structured :class:`QualityReport`. No extraction, no overall score,
no acceptance decision, no LLM.
"""

import logging

from .checker import QualityChecker, check_quality
from .models import CompletenessFilterResult, FilterResult, QualityReport

logging.getLogger(__name__).addHandler(logging.NullHandler())

__version__ = "1.0.0"

__all__ = [
    "CompletenessFilterResult",
    "FilterResult",
    "QualityChecker",
    "QualityReport",
    "check_quality",
]
