"""Tests for the Completeness filter."""

import pytest

from quality_checker.filters.completeness import CompletenessFilter, evaluate_completeness
from quality_checker.models import CompletenessFilterResult, FilterResult
from quality_checker.tests.conftest import (
    ALTERNATIVE_HEADINGS_RESUME,
    NORMAL_RESUME,
    PARTIAL_RESUME,
)


@pytest.fixture()
def filt() -> CompletenessFilter:
    return CompletenessFilter()


def test_empty_text_scores_zero(filt):
    result = filt.evaluate("")
    assert result.score == 0.0
    assert "EMPTY_TEXT" in result.flags
    assert result.detected_sections == []
    assert "Skills" in result.missing_sections


def test_normal_resume_detects_common_sections(filt):
    result = filt.evaluate(NORMAL_RESUME)
    for expected in ["Contact Information", "Skills", "Work Experience",
                     "Education", "Projects", "Certifications"]:
        assert expected in result.detected_sections, result.detected_sections
    assert result.score >= 80


def test_resume_with_some_sections_missing(filt):
    result = filt.evaluate(PARTIAL_RESUME)
    assert "Skills" in result.detected_sections
    assert "Education" in result.detected_sections
    assert "Work Experience" in result.missing_sections
    assert "Projects" in result.missing_sections
    # Missing sections are possible omissions, not extraction failures:
    assert result.score > 0
    assert any("not by itself proof" in reason for reason in result.reasons)
    assert result.missing_recommended or result.missing_optional


def test_alternative_section_names_are_detected(filt):
    result = filt.evaluate(ALTERNATIVE_HEADINGS_RESUME)
    assert "Skills" in result.detected_sections           # "Technical Skills"
    assert "Work Experience" in result.detected_sections  # "Professional Experience"
    assert "Education" in result.detected_sections        # "Academic Background"


def test_employment_history_alias():
    text = (
        "Sam Lee\nsam@example.com\n\nEmployment History\n"
        "Analyst at Example Corp from 2023 to 2025 working on reports and dashboards.\n"
    )
    result = evaluate_completeness(text)
    assert "Work Experience" in result.detected_sections


def test_contact_detected_from_email_without_heading(filt):
    text = "Alex Ray\nalex.ray@example.com\n\nEducation\nBSc Computing, completed in 2023 at a recognised university.\n"
    result = filt.evaluate(text)
    assert "Contact Information" in result.detected_sections


def test_missing_optional_sections_cost_less_than_required(filt):
    # Resume missing only optional sections (Projects / Certifications / Summary
    # all absent is covered in PARTIAL_RESUME too) should keep a decent score
    # when required + recommended sections are present.
    # The credential wording is neutralised too: since the
    # content-evidence patterns were added, a stated credential
    # ("NASSCOM Certified Data Scientist") counts as Certifications
    # evidence even under a renamed heading — which is the intended
    # behaviour, so this fixture must remove the evidence itself to
    # keep testing genuinely-absent optional sections.
    text = NORMAL_RESUME.replace("Projects", "Portfolio Work").replace(
        "Certifications", "Awards and Honours").replace(
        "Professional Summary", "Overview").replace("Certified", "Awarded")
    result = filt.evaluate(text)
    assert "Projects" in result.missing_optional
    assert "Certifications" in result.missing_optional
    assert result.score >= 85


def test_sections_are_configurable():
    custom = {
        "sections": {
            "skills": {"display_name": "Skills", "importance": "required",
                       "aliases": ["skills", "toolkit"]},
        },
        "penalties_by_importance": {"required": 50, "recommended": 10, "optional": 1},
    }
    filt = CompletenessFilter(config=custom)
    assert filt.evaluate("Toolkit\nPython and SQL.\n").detected_sections == ["Skills"]
    missing = filt.evaluate("Nothing relevant here at all.")
    assert missing.score == 50.0
    assert missing.missing_required == ["Skills"]


@pytest.mark.parametrize("bad_input", [None, 42, ["Skills"], b"Skills"])
def test_invalid_input_is_handled_safely(filt, bad_input):
    result = filt.evaluate(bad_input)
    assert isinstance(result, FilterResult)
    assert result.score == 0.0
    assert "INVALID_INPUT" in result.flags
    assert result.detected_sections == []


def test_result_model_structure(filt):
    result = filt.evaluate(NORMAL_RESUME)
    assert isinstance(result, CompletenessFilterResult)
    assert isinstance(result, FilterResult)  # subclass of the shared model
    assert result.filter_name == "completeness"
    assert 0.0 <= result.score <= 100.0
    assert result.metrics["detected_sections"] == result.detected_sections
    assert result.metrics["missing_sections"] == result.missing_sections
    assert result.metrics["detected_count"] == len(result.detected_sections)
