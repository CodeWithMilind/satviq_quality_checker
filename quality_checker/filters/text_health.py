"""Text Health filter.

Deterministic checks on whether extracted text is technically readable:
emptiness, replacement characters (�), non-printable characters,
abnormal whitespace / symbol ratios, and repeated-character corruption.

No LLM is used. The original text is never mutated; analysis works on
the string as given (counts only) — no normalised copy is needed here.
"""

from __future__ import annotations

import logging
import re
import string
import unicodedata
from pathlib import Path
from typing import Any

from ..models import FilterResult
from ._config import invalid_input_result, load_config, scaled_penalty

logger = logging.getLogger(__name__)

REPLACEMENT_CHAR = "�"
# Tab, LF, CR and form-feed are legitimate in extracted text.
_ALLOWED_WHITESPACE = set(" \t\n\r\f\v")
_BASIC_PUNCTUATION = set(string.punctuation)


def _is_non_printable(ch: str) -> bool:
    if ch in _ALLOWED_WHITESPACE:
        return False
    return unicodedata.category(ch).startswith("C")


def _is_symbol(ch: str) -> bool:
    """Symbol = not alphanumeric, not whitespace, not basic punctuation."""
    if ch.isalnum() or ch.isspace() or ch in _BASIC_PUNCTUATION:
        return False
    # Letters from any script (category L*) are text, not symbols.
    return not unicodedata.category(ch).startswith("L")


def _max_repeated_run(text: str) -> int:
    max_run = run = 1 if text else 0
    prev: str | None = None
    for ch in text:
        run = run + 1 if ch == prev else 1
        prev = ch
        max_run = max(max_run, run)
    return max_run


def _repeated_line_ratio(lines: list[str]) -> float:
    """Share of lines that consist almost entirely of one repeated char."""
    candidates = [ln for ln in lines if len(ln.strip()) >= 8]
    if not candidates:
        return 0.0
    repeated = 0
    for line in candidates:
        stripped = line.strip()
        most_common = max(stripped.count(c) for c in set(stripped))
        if most_common / len(stripped) >= 0.8:
            repeated += 1
    return repeated / len(candidates)


class TextHealthFilter:
    """Evaluate the technical health of extracted text."""

    name = "text_health"

    def __init__(self, config: dict[str, Any] | None = None,
                 config_path: str | Path | None = None) -> None:
        self.config = load_config("text_health.yaml", config=config, config_path=config_path)
        self.thresholds: dict[str, Any] = self.config.get("thresholds", {})
        self.penalties: dict[str, Any] = self.config.get("penalties", {})

    # -- public API -----------------------------------------------------
    def evaluate(self, text: object) -> FilterResult:
        if not isinstance(text, str):
            logger.warning("TextHealthFilter received non-string input: %s", type(text).__name__)
            return FilterResult(
                filter_name=self.name, score=0.0,
                flags=["INVALID_INPUT"],
                reasons=[f"Expected extracted text as str, got {type(text).__name__}."],
                metrics=invalid_input_result(self.name, text),
            )

        t, p = self.thresholds, self.penalties
        char_count = len(text)
        words = text.split()
        word_count = len(words)

        if char_count == 0 or word_count == 0:
            logger.info("TextHealthFilter: empty text")
            return FilterResult(
                filter_name=self.name, score=0.0,
                flags=["EMPTY_TEXT"],
                reasons=["Extracted text is empty."],
                metrics={"char_count": char_count, "word_count": word_count,
                         "replacement_char_count": 0, "replacement_char_ratio": 0.0,
                         "non_printable_count": 0, "non_printable_ratio": 0.0,
                         "whitespace_ratio": 0.0, "symbol_ratio": 0.0,
                         "max_repeated_char_run": 0, "repeated_line_ratio": 0.0},
            )

        replacement_count = text.count(REPLACEMENT_CHAR)
        non_printable_count = sum(1 for c in text if _is_non_printable(c))
        whitespace_count = sum(1 for c in text if c.isspace())
        symbol_count = sum(1 for c in text if _is_symbol(c))
        max_run = _max_repeated_run(text)
        lines = text.splitlines()
        repeated_line_ratio = _repeated_line_ratio(lines)

        metrics: dict[str, Any] = {
            "char_count": char_count,
            "word_count": word_count,
            "line_count": len(lines),
            "replacement_char_count": replacement_count,
            "replacement_char_ratio": round(replacement_count / char_count, 6),
            "non_printable_count": non_printable_count,
            "non_printable_ratio": round(non_printable_count / char_count, 6),
            "whitespace_ratio": round(whitespace_count / char_count, 6),
            "symbol_count": symbol_count,
            "symbol_ratio": round(symbol_count / char_count, 6),
            "max_repeated_char_run": max_run,
            "repeated_line_ratio": round(repeated_line_ratio, 6),
        }

        score = 100.0
        flags: list[str] = []
        reasons: list[str] = []

        # Length
        if char_count < t.get("min_char_count", 200) or word_count < t.get("min_word_count", 30):
            score -= p.get("very_short_text", 60)
            flags.append("VERY_SHORT_TEXT")
            reasons.append(
                f"Text is nearly empty: {char_count} characters / {word_count} words "
                f"(minimum expected {t.get('min_char_count', 200)} / {t.get('min_word_count', 30)})."
            )
        elif char_count < t.get("good_char_count", 500) or word_count < t.get("good_word_count", 80):
            score -= p.get("short_text", 25)
            flags.append("SHORT_TEXT")
            reasons.append(f"Text is short for a resume: {char_count} characters / {word_count} words.")

        # Replacement characters
        penalty = scaled_penalty(metrics["replacement_char_ratio"],
                                 t.get("replacement_char_warn_ratio", 0.001),
                                 t.get("replacement_char_max_ratio", 0.02),
                                 p.get("replacement_chars", 35))
        if penalty:
            score -= penalty
            flags.append("EXCESSIVE_REPLACEMENT_CHARS")
            reasons.append(
                f"{replacement_count} replacement characters (�) found "
                f"({metrics['replacement_char_ratio']:.2%} of text) — likely a decoding problem."
            )

        # Non-printable characters
        penalty = scaled_penalty(metrics["non_printable_ratio"],
                                 t.get("non_printable_warn_ratio", 0.001),
                                 t.get("non_printable_max_ratio", 0.05),
                                 p.get("non_printable_chars", 30))
        if penalty:
            score -= penalty
            flags.append("EXCESSIVE_NON_PRINTABLE_CHARS")
            reasons.append(
                f"{non_printable_count} non-printable characters found "
                f"({metrics['non_printable_ratio']:.2%} of text)."
            )

        # Whitespace ratio
        penalty = scaled_penalty(metrics["whitespace_ratio"],
                                 t.get("whitespace_warn_ratio", 0.35),
                                 t.get("whitespace_max_ratio", 0.70),
                                 p.get("abnormal_whitespace", 25))
        if penalty:
            score -= penalty
            flags.append("ABNORMAL_WHITESPACE_RATIO")
            reasons.append(f"Whitespace makes up {metrics['whitespace_ratio']:.1%} of the text.")

        # Symbol ratio
        penalty = scaled_penalty(metrics["symbol_ratio"],
                                 t.get("symbol_warn_ratio", 0.10),
                                 t.get("symbol_max_ratio", 0.35),
                                 p.get("high_symbol_ratio", 25))
        if penalty:
            score -= penalty
            flags.append("HIGH_SYMBOL_RATIO")
            reasons.append(f"Symbols make up {metrics['symbol_ratio']:.1%} of the text.")

        # Repeated characters / corrupted-looking lines
        run_warn, run_max = t.get("repeated_char_run_warn", 12), t.get("repeated_char_run_max", 40)
        run_penalty = scaled_penalty(float(max_run), float(run_warn), float(run_max),
                                     p.get("repeated_characters", 20))
        line_penalty = scaled_penalty(repeated_line_ratio,
                                      t.get("repeated_line_ratio_warn", 0.10),
                                      t.get("repeated_line_ratio_max", 0.40),
                                      p.get("repeated_characters", 20))
        penalty = max(run_penalty, line_penalty)
        if penalty:
            score -= penalty
            flags.append("EXCESSIVE_REPEATED_CHARACTERS")
            reasons.append(
                f"Repeated-character corruption suspected: longest run is {max_run} characters, "
                f"{repeated_line_ratio:.1%} of long lines are single-character repeats."
            )

        logger.debug("TextHealthFilter score=%.2f flags=%s", max(0.0, score), flags)
        return FilterResult(filter_name=self.name, score=max(0.0, score),
                            flags=flags, reasons=reasons, metrics=metrics)


def evaluate_text_health(text: object, **kwargs: Any) -> FilterResult:
    """Convenience function so the filter is usable without a class instance."""
    return TextHealthFilter(**kwargs).evaluate(text)
