"""Tests for the QualityChecker orchestrator and QualityReport model."""

import pytest

from quality_checker import QualityChecker, check_quality
from quality_checker.models import CompletenessFilterResult, FilterResult, QualityReport
from quality_checker.tests.conftest import NORMAL_RESUME


@pytest.fixture()
def checker() -> QualityChecker:
    return QualityChecker()


def _report(checker, text):
    return checker.check(text, document_id="doc-001", source_tool="pymupdf", attempt_id="attempt-1")


def test_report_structure_and_metadata(checker):
    report = _report(checker, NORMAL_RESUME)
    assert isinstance(report, QualityReport)
    assert report.document_id == "doc-001"
    assert report.source_tool == "pymupdf"
    assert report.attempt_id == "attempt-1"


def test_each_filter_result_is_independently_accessible(checker):
    report = _report(checker, NORMAL_RESUME)
    assert isinstance(report.text_health, FilterResult)
    assert isinstance(report.completeness, CompletenessFilterResult)
    assert isinstance(report.reading_order, FilterResult)
    assert report.text_health.filter_name == "text_health"
    assert report.completeness.filter_name == "completeness"
    assert report.reading_order.filter_name == "reading_order"
    for result in (report.text_health, report.completeness, report.reading_order):
        assert 0.0 <= result.score <= 100.0


def test_no_overall_score_is_produced(checker):
    report = _report(checker, NORMAL_RESUME)
    assert not hasattr(report, "overall_score")
    assert "overall_score" not in report.model_dump()
    assert "overall" not in report.model_dump()


def test_normal_resume_scores_high_on_all_filters(checker):
    report = _report(checker, NORMAL_RESUME)
    assert report.text_health.score >= 80
    assert report.completeness.score >= 80
    assert report.reading_order.score >= 80


def test_empty_text_report(checker):
    report = _report(checker, "")
    assert report.text_health.score == 0.0
    assert report.completeness.score == 0.0
    assert report.reading_order.score == 0.0
    assert "EMPTY_TEXT" in report.text_health.flags


@pytest.mark.parametrize("bad_input", [None, 123, ["text"], b"bytes"])
def test_invalid_input_produces_report_not_exception(checker, bad_input):
    report = _report(checker, bad_input)
    assert isinstance(report, QualityReport)
    assert report.text_health.score == 0.0
    assert "INVALID_INPUT" in report.text_health.flags
    assert "INVALID_INPUT" in report.completeness.flags
    assert "INVALID_INPUT" in report.reading_order.flags


def test_original_text_is_not_mutated(checker):
    original = NORMAL_RESUME
    snapshot = original[:]
    _report(checker, original)
    assert original == snapshot


def test_report_is_json_serialisable(checker):
    report = _report(checker, NORMAL_RESUME)
    payload = report.model_dump_json()
    restored = QualityReport.model_validate_json(payload)
    assert restored == report


def test_check_quality_convenience_function():
    report = check_quality(NORMAL_RESUME, document_id="d1",
                           source_tool="tika", attempt_id="a1")
    assert report.source_tool == "tika"
    assert report.text_health.score >= 80


def test_filters_are_independent_of_each_other():
    """Corrupting characters hurts Text Health but section detection still works."""
    corrupted = NORMAL_RESUME + ("\n" + "�" * 200)
    checker = QualityChecker()
    report = checker.check(corrupted, document_id="d2", source_tool="ocr", attempt_id="a2")
    assert report.text_health.score < 100
    assert "Skills" in report.completeness.detected_sections
