"""Tests for the Reading Order filter."""

import pytest

from quality_checker.filters.reading_order import ReadingOrderFilter, evaluate_reading_order
from quality_checker.models import FilterResult
from quality_checker.tests.conftest import FRAGMENTED_TEXT, NORMAL_RESUME


@pytest.fixture()
def filt() -> ReadingOrderFilter:
    return ReadingOrderFilter()


def test_empty_text_scores_zero(filt):
    result = filt.evaluate("")
    assert result.score == 0.0
    assert "EMPTY_TEXT" in result.flags
    assert result.metrics["non_empty_line_count"] == 0


def test_normal_resume_scores_high(filt):
    result = filt.evaluate(NORMAL_RESUME)
    assert result.score >= 80, result
    assert result.metrics["non_empty_line_count"] > 10
    assert result.metrics["repeated_adjacent_count"] == 0


def test_fragmented_lines_are_flagged(filt):
    result = filt.evaluate(FRAGMENTED_TEXT)
    assert result.score < 70
    assert "EXCESSIVE_SHORT_LINES" in result.flags
    assert "FRAGMENTED_LINES" in result.flags
    assert result.metrics["short_line_ratio"] == 1.0


def test_repeated_adjacent_lines_are_flagged(filt):
    line = "This is a complete sentence describing relevant work experience in detail."
    text = "\n".join([line] * 12)
    result = filt.evaluate(text)
    assert "REPEATED_ADJACENT_LINES" in result.flags
    assert result.metrics["repeated_adjacent_count"] == 11
    assert result.score < 100


def test_broken_sentence_clues_are_reported(filt):
    # Many lines ending mid-sentence, continuing lowercase on the next line.
    text = "\n".join([
        "the candidate worked on a large",
        "project involving data pipelines and",
        "automation for the reporting team and",
        "delivered results every single week and",
        "documented everything carefully for",
        "future maintainers of the system and",
        "stakeholders across the whole company",
    ])
    result = filt.evaluate(text)
    assert result.metrics["broken_sentence_ratio"] > 0.3
    assert "POSSIBLE_BROKEN_SENTENCES" in result.flags or result.score < 100


def test_non_adjacent_repeats_are_not_penalised_as_adjacent(filt):
    text = "\n".join([
        "First section describes the candidate background and education history fully.",
        "Second section describes projects, tools, outcomes, and measurable results clearly.",
        "First section describes the candidate background and education history fully.",
    ])
    result = filt.evaluate(text)
    assert result.metrics["repeated_adjacent_count"] == 0
    assert "REPEATED_ADJACENT_LINES" not in result.flags


@pytest.mark.parametrize("bad_input", [None, 123, ["line"], {"a": 1}, b"bytes"])
def test_invalid_input_is_handled_safely(filt, bad_input):
    result = filt.evaluate(bad_input)
    assert isinstance(result, FilterResult)
    assert result.score == 0.0
    assert "INVALID_INPUT" in result.flags


def test_result_model_structure(filt):
    result = evaluate_reading_order(NORMAL_RESUME)
    assert isinstance(result, FilterResult)
    assert result.filter_name == "reading_order"
    assert 0.0 <= result.score <= 100.0
    assert isinstance(result.flags, list)
    assert isinstance(result.reasons, list)
    assert isinstance(result.metrics, dict)
    for key in ["short_line_ratio", "fragmented_line_ratio", "line_length_cv",
                "broken_sentence_ratio", "repeated_adjacent_ratio"]:
        assert key in result.metrics
