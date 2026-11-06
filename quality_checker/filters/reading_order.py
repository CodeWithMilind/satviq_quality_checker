"""Reading Order filter.

Evaluates *clues in the extracted text* that reading order may be
broken: excessive very-short lines, fragmented lines, suspicious
line-length variation, possible broken sentences, repeated adjacent
lines, and long text collapsed into very few lines (a common symptom
of multi-column extraction merging columns).

This filter does NOT claim to detect the original visual layout —
it only reports what the text itself suggests. Deterministic only.
"""

from __future__ import annotations

import logging
import statistics
from pathlib import Path
from typing import Any

from ..models import FilterResult
from ._config import invalid_input_result, load_config, scaled_penalty

logger = logging.getLogger(__name__)

_SENTENCE_END = set(".!?:;•·-–—)")


class ReadingOrderFilter:
    """Evaluate whether extracted text preserves a reasonable reading order."""

    name = "reading_order"

    def __init__(self, config: dict[str, Any] | None = None,
                 config_path: str | Path | None = None) -> None:
        self.config = load_config("reading_order.yaml", config=config, config_path=config_path)
        self.thresholds: dict[str, Any] = self.config.get("thresholds", {})
        self.penalties: dict[str, Any] = self.config.get("penalties", {})

    # -- public API -----------------------------------------------------
    def evaluate(self, text: object) -> FilterResult:
        if not isinstance(text, str):
            logger.warning("ReadingOrderFilter received non-string input: %s", type(text).__name__)
            return FilterResult(
                filter_name=self.name, score=0.0,
                flags=["INVALID_INPUT"],
                reasons=[f"Expected extracted text as str, got {type(text).__name__}."],
                metrics=invalid_input_result(self.name, text),
            )

        if not text.strip():
            return FilterResult(
                filter_name=self.name, score=0.0,
                flags=["EMPTY_TEXT"],
                reasons=["Extracted text is empty, so reading order cannot be evaluated."],
                metrics={"char_count": len(text), "word_count": 0, "line_count": 0,
                         "non_empty_line_count": 0},
            )

        t, p = self.thresholds, self.penalties
        lines = [line for line in (ln.strip() for ln in text.splitlines()) if line]
        line_count = len(lines)
        lengths = [len(line) for line in lines]
        word_counts = [len(line.split()) for line in lines]

        short_max_words = int(t.get("short_line_max_words", 2))
        short_lines = sum(1 for wc in word_counts if wc <= short_max_words)
        short_ratio = short_lines / line_count if line_count else 0.0

        frag_max_chars = int(t.get("fragmented_line_max_chars", 15))
        fragmented = sum(1 for ln in lengths if ln <= frag_max_chars)
        fragmented_ratio = fragmented / line_count if line_count else 0.0

        mean_len = statistics.fmean(lengths) if lengths else 0.0
        stdev_len = statistics.pstdev(lengths) if len(lengths) > 1 else 0.0
        cv = (stdev_len / mean_len) if mean_len else 0.0

        # Possible broken sentences: line does not end with sentence
        # punctuation and the next line starts with a lowercase letter.
        broken = 0
        transitions = 0
        for current, nxt in zip(lines, lines[1:]):
            transitions += 1
            if current and current[-1] not in _SENTENCE_END and nxt and nxt[0].islower():
                broken += 1
        broken_ratio = broken / transitions if transitions else 0.0

        repeated_adjacent = sum(1 for a, b in zip(lines, lines[1:]) if a == b)
        repeated_ratio = repeated_adjacent / transitions if transitions else 0.0

        metrics: dict[str, Any] = {
            "char_count": len(text),
            "word_count": len(text.split()),
            "line_count": len(text.splitlines()),
            "non_empty_line_count": line_count,
            "short_line_count": short_lines,
            "short_line_ratio": round(short_ratio, 6),
            "fragmented_line_count": fragmented,
            "fragmented_line_ratio": round(fragmented_ratio, 6),
            "mean_line_length": round(mean_len, 2),
            "median_line_length": float(statistics.median(lengths)) if lengths else 0.0,
            "line_length_stdev": round(stdev_len, 2),
            "line_length_cv": round(cv, 6),
            "broken_sentence_count": broken,
            "broken_sentence_ratio": round(broken_ratio, 6),
            "repeated_adjacent_count": repeated_adjacent,
            "repeated_adjacent_ratio": round(repeated_ratio, 6),
        }

        score = 100.0
        flags: list[str] = []
        reasons: list[str] = []

        penalty = scaled_penalty(short_ratio, t.get("short_line_warn_ratio", 0.40),
                                 t.get("short_line_max_ratio", 0.80),
                                 p.get("excessive_short_lines", 30))
        if penalty:
            score -= penalty
            flags.append("EXCESSIVE_SHORT_LINES")
            reasons.append(
                f"{short_ratio:.1%} of lines have {short_max_words} words or fewer — "
                "text may be fragmented or columns may have been split."
            )

        penalty = scaled_penalty(fragmented_ratio, t.get("fragmented_warn_ratio", 0.35),
                                 t.get("fragmented_max_ratio", 0.75),
                                 p.get("fragmented_lines", 25))
        if penalty:
            score -= penalty
            flags.append("FRAGMENTED_LINES")
            reasons.append(f"{fragmented_ratio:.1%} of lines are {frag_max_chars} characters or shorter.")

        penalty = scaled_penalty(cv, t.get("line_length_cv_warn", 1.0),
                                 t.get("line_length_cv_max", 2.0),
                                 p.get("line_length_variation", 15))
        if penalty:
            score -= penalty
            flags.append("HIGH_LINE_LENGTH_VARIATION")
            reasons.append(
                f"Line lengths vary unusually (coefficient of variation {cv:.2f}) — "
                "a possible sign of mixed or merged column text."
            )

        penalty = scaled_penalty(broken_ratio, t.get("broken_sentence_warn_ratio", 0.30),
                                 t.get("broken_sentence_max_ratio", 0.70),
                                 p.get("broken_sentences", 20))
        if penalty:
            score -= penalty
            flags.append("POSSIBLE_BROKEN_SENTENCES")
            reasons.append(
                f"{broken_ratio:.1%} of line transitions look like a sentence broken "
                "across lines without punctuation."
            )

        penalty = scaled_penalty(repeated_ratio, t.get("repeated_adjacent_warn_ratio", 0.05),
                                 t.get("repeated_adjacent_max_ratio", 0.30),
                                 p.get("repeated_adjacent_lines", 25))
        if penalty:
            score -= penalty
            flags.append("REPEATED_ADJACENT_LINES")
            reasons.append(f"{repeated_adjacent} adjacent line(s) are exact repeats of the previous line.")

        if len(text) >= int(t.get("long_text_char_count", 1000)) and \
                line_count < int(t.get("min_lines_for_long_text", 5)):
            score -= p.get("too_few_lines", 10)
            flags.append("TOO_FEW_LINES_FOR_LENGTH")
            reasons.append(
                f"Text is {len(text)} characters but only {line_count} non-empty lines — "
                "blocks or columns may have been merged."
            )

        logger.debug("ReadingOrderFilter score=%.2f flags=%s", max(0.0, score), flags)
        return FilterResult(filter_name=self.name, score=max(0.0, score),
                            flags=flags, reasons=reasons, metrics=metrics)


def evaluate_reading_order(text: object, **kwargs: Any) -> FilterResult:
    """Convenience function so the filter is usable without a class instance."""
    return ReadingOrderFilter(**kwargs).evaluate(text)
