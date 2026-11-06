"""Deterministic text normalization for extracted resume text.

Pipeline position:

    Selected extractor text -> normalize_text() -> Muse AI

This module:
- Uses deterministic Python rules only; no LLM or network calls.
- Preserves the original text; callers store it separately and it
  is never modified here.
- Removes invisible characters and non-printing control
  characters.
- Normalizes whitespace, tabs, and excessive blank lines while
  preserving meaningful paragraph breaks and line boundaries.
- Removes lines containing only decorative separators (they carry
  no information); table rows and other structured lines are kept.
- Does not correct OCR spelling, summarize, paraphrase, translate,
  or rewrite wording — facts and content pass through unchanged.
- Is idempotent: normalizing normalized text changes nothing.
- Provides approximate token metrics, not actual Muse API usage.

``compact_resume_text`` is retained as a compatibility alias for
``normalize_text``. An earlier variant dropped EVERY blank line as
a token-compaction step; that destroyed meaningful paragraph
breaks, so the stage no longer compacts — it only normalizes.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# Invisible extraction noise:
# BOM, zero-width space/non-joiner/joiner, soft hyphen.
_INVISIBLE_RE = re.compile(r"[\ufeff\u200b\u200c\u200d\u00ad]")

# C0 control characters except newline and tab, plus DEL.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Consecutive spaces or tabs.
_SPACE_RUN_RE = re.compile(r"[ \t]+")

# Three or more newlines become one blank line.
_BLANK_RUN_RE = re.compile(r"\n{3,}")

# Lines consisting only of decorative separator characters.
# "|" is deliberately NOT in this set: a line such as
# "| --- | --- |" is a table delimiter (structure worth keeping),
# not decoration.
_DECORATIVE_LINE_RE = re.compile(r"^[\s•·—–─\-_=~*#]+$")


class TextNormalizationError(ValueError):
    """Raised when text cannot be normalized safely."""


def _is_decorative_line(line: str) -> bool:
    """True when a (trimmed) line is only separator characters —
    a visual divider with no meaningful information."""
    return bool(line) and _DECORATIVE_LINE_RE.fullmatch(line) is not None


def normalize_with_steps(text: str) -> tuple[str, list[str]]:
    """Normalize ``text`` and report the transformations that were
    actually applied, in order, as ``normalization_steps``.

    Raises:
        TextNormalizationError: If input is not a string.
    """
    if not isinstance(text, str):
        raise TextNormalizationError(
            f"Expected str, got {type(text).__name__}"
        )

    if not text:
        return "", []

    steps: list[str] = []
    out = text

    # Standardize line endings.
    new = out.replace("\r\n", "\n").replace("\r", "\n")
    if new != out:
        steps.append("line_endings_normalized")
        out = new

    # Remove invisible extraction noise.
    new = _INVISIBLE_RE.sub("", out)
    if new != out:
        steps.append("invisible_characters_removed")
        out = new
    new = _CONTROL_RE.sub("", out)
    if new != out:
        steps.append("control_characters_removed")
        out = new

    # Standardize non-breaking spaces.
    new = out.replace("\u00a0", " ").replace("\u202f", " ")
    if new != out:
        steps.append("non_breaking_spaces_normalized")
        out = new

    # Collapse spaces/tabs within each line, then trim each line.
    # Lines are never joined and words are never merged.
    lines = out.split("\n")
    collapsed = [_SPACE_RUN_RE.sub(" ", line) for line in lines]
    if collapsed != lines:
        steps.append("whitespace_runs_collapsed")
    trimmed = [line.strip() for line in collapsed]
    if trimmed != collapsed:
        steps.append("lines_trimmed")

    # Remove decorative separator lines (no information content).
    kept = [line for line in trimmed if not _is_decorative_line(line)]
    if len(kept) != len(trimmed):
        steps.append("decorative_separator_lines_removed")
    out = "\n".join(kept)

    # Preserve one blank line for meaningful section separation.
    new = _BLANK_RUN_RE.sub("\n\n", out)
    if new != out:
        steps.append("excessive_blank_lines_collapsed")
        out = new

    new = out.strip()
    if new != out:
        steps.append("text_trimmed")
        out = new

    return out, steps


def normalize_text(text: str) -> str:
    """Clean whitespace and invisible noise without rewriting
    content. See :func:`normalize_with_steps` for the step list."""
    normalized, _steps = normalize_with_steps(text)
    return normalized


def compact_resume_text(text: str) -> str:
    """Compatibility alias for :func:`normalize_text`.

    Kept so existing imports keep working. It no longer drops
    blank lines: meaningful paragraph breaks are preserved.
    """
    if not isinstance(text, str):
        raise TextNormalizationError(
            f"Expected str, got {type(text).__name__}"
        )
    return normalize_text(text)


def estimate_tokens(text: str) -> int:
    """Estimate tokens using ~4 characters/token.

    This is only a display estimate. Actual Muse token usage must
    come from the API response.
    """
    if not text:
        return 0

    return max(1, round(len(text) / 4))


def _metrics(original: str, normalized: str) -> dict[str, Any]:
    """Build character and approximate token metrics."""
    original_tokens = estimate_tokens(original)
    normalized_tokens = estimate_tokens(normalized)

    reduction = 0.0
    if original_tokens > 0:
        reduction = round(
            (original_tokens - normalized_tokens)
            / original_tokens
            * 100,
            1,
        )

    return {
        "original_chars": len(original),
        "normalized_chars": len(normalized),
        "original_estimated_tokens": original_tokens,
        "normalized_estimated_tokens": normalized_tokens,
        "estimated_reduction_percent": reduction,
    }


def normalization_block(
    original_text: str | None,
    *,
    source: str = "tika",
) -> dict[str, Any]:
    """Return the record's text_normalization block for one source
    text. Never raises:

    - no source text  → status "unavailable", normalized_text None
    - success         → status "applied", normalized_text set,
      ``original_extracted_text`` holding the exact input,
      ``normalization_applied`` telling whether anything changed,
      and ``normalization_steps`` listing the transformations
      actually applied
    - failure         → status "bypassed", normalized_text None and
      the error recorded; the caller must then use the ORIGINAL
      text unchanged (a controlled, visible fallback — corrupted
      output is never passed on).
    """
    block: dict[str, Any] = {
        "source": source,
        "status": "unavailable",
        "error": None,
        "original_extracted_text": None,
        "normalized_text": None,
        "normalization_applied": False,
        "normalization_steps": [],
        **_metrics("", ""),
    }

    if original_text is None or original_text == "":
        return block

    if not isinstance(original_text, str):
        block["status"] = "bypassed"
        block["error"] = (
            f"Normalization failed: expected str, "
            f"got {type(original_text).__name__}"
        )
        return block

    block["original_extracted_text"] = original_text

    try:
        normalized, steps = normalize_with_steps(original_text)

    except Exception as exc:
        logger.warning("Text normalization failed: %s", exc)
        block.update(
            status="bypassed",
            error=f"Normalization failed: {exc}",
            **_metrics(original_text, original_text),
        )
        return block

    block.update(
        status="applied",
        normalized_text=normalized,
        normalization_applied=normalized != original_text,
        normalization_steps=steps,
        **_metrics(original_text, normalized),
    )

    return block
