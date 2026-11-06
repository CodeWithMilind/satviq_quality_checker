"""Tests for the Text Health filter."""

import pytest

from quality_checker.filters.text_health import TextHealthFilter, evaluate_text_health
from quality_checker.models import FilterResult
from quality_checker.tests.conftest import (
    CORRUPTED_TEXT,
    NORMAL_RESUME,
    SYMBOL_HEAVY_TEXT,
    WHITESPACE_HEAVY_TEXT,
)


@pytest.fixture()
def filt() -> TextHealthFilter:
    return TextHealthFilter()


def test_empty_text_scores_zero(filt):
    result = filt.evaluate("")
    assert result.score == 0.0
    assert "EMPTY_TEXT" in result.flags
    assert result.metrics["char_count"] == 0


def test_whitespace_only_text_is_nearly_empty(filt):
    result = filt.evaluate("   \n\t  ")
    assert result.score == 0.0
    assert "EMPTY_TEXT" in result.flags


def test_normal_clean_resume_scores_high(filt):
    result = filt.evaluate(NORMAL_RESUME)
    assert result.score >= 80
    assert result.flags == []
    assert result.metrics["char_count"] == len(NORMAL_RESUME)
    assert result.metrics["word_count"] == len(NORMAL_RESUME.split())
    assert result.metrics["replacement_char_ratio"] == 0.0


def test_corrupted_text_is_flagged(filt):
    result = filt.evaluate(CORRUPTED_TEXT)
    assert result.score < 80
    assert "EXCESSIVE_REPLACEMENT_CHARS" in result.flags
    assert result.metrics["replacement_char_count"] == 120
    assert any("replacement" in reason.lower() for reason in result.reasons)


def test_excessive_symbols_are_flagged(filt):
    result = filt.evaluate(SYMBOL_HEAVY_TEXT)
    assert "HIGH_SYMBOL_RATIO" in result.flags
    assert result.metrics["symbol_ratio"] > 0.10
    assert result.score < 100


def test_excessive_whitespace_is_flagged(filt):
    result = filt.evaluate(WHITESPACE_HEAVY_TEXT)
    assert "ABNORMAL_WHITESPACE_RATIO" in result.flags or "VERY_SHORT_TEXT" in result.flags
    assert result.metrics["whitespace_ratio"] > 0.35


def test_non_printable_characters_are_flagged(filt):
    text = ("A readable resume sentence with enough ordinary words to count properly. " * 4)
    text += "\x00\x01\x02\x03" * 30
    result = filt.evaluate(text)
    assert "EXCESSIVE_NON_PRINTABLE_CHARS" in result.flags
    assert result.metrics["non_printable_count"] == 120


def test_repeated_characters_are_flagged(filt):
    text = ("A readable resume sentence with enough ordinary words to count properly. " * 4)
    text += "a" * 80
    result = filt.evaluate(text)
    assert "EXCESSIVE_REPEATED_CHARACTERS" in result.flags
    assert result.metrics["max_repeated_char_run"] >= 80


def test_very_short_text_is_flagged(filt):
    result = filt.evaluate("John Doe\nPython developer.")
    assert result.score <= 40
    assert "VERY_SHORT_TEXT" in result.flags


@pytest.mark.parametrize("bad_input", [None, 123, 4.5, ["text"], {"text": "x"}, b"bytes"])
def test_invalid_input_is_handled_safely(filt, bad_input):
    result = filt.evaluate(bad_input)
    assert isinstance(result, FilterResult)
    assert result.score == 0.0
    assert "INVALID_INPUT" in result.flags


def test_result_model_structure(filt):
    result = evaluate_text_health(NORMAL_RESUME)
    assert isinstance(result, FilterResult)
    assert result.filter_name == "text_health"
    assert 0.0 <= result.score <= 100.0
    assert isinstance(result.flags, list)
    assert isinstance(result.reasons, list)
    assert isinstance(result.metrics, dict)
    dumped = result.model_dump()
    assert set(dumped) == {"filter_name", "score", "flags", "reasons", "metrics"}


def test_original_text_is_not_mutated(filt):
    original = NORMAL_RESUME
    snapshot = original[:]
    filt.evaluate(original)
    assert original == snapshot
