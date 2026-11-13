"""Calibration Phase 1: ground-truth annotation schema validation."""

from __future__ import annotations

import pytest

from calibration.schemas import (
    GroundTruthAnnotation, canonical_section, validate_annotation)


def _base(**overrides):
    raw = {
        "expected_present_sections": ["Education", "Projects"],
        "expected_missing_sections": ["Work Experience"],
        "expected_completeness_score": 72,
        "expected_quality_score": "65.5",
        "expected_suitable_roles": None,
        "career_stage": "Student",
        "professional_domain": "Software Engineering",
    }
    raw.update(overrides)
    return raw


def test_valid_annotation_normalises():
    model = GroundTruthAnnotation(**_base())
    assert model.expected_completeness_score == 72.0
    assert model.expected_quality_score == 65.5
    assert model.expected_suitable_roles is None  # unlabelled


def test_section_names_canonicalised_and_deduped():
    model = GroundTruthAnnotation(**_base(
        expected_present_sections=[" education ", "EDUCATION", "projects"]))
    assert model.expected_present_sections == ["Education", "Projects"]


def test_canonical_section_lookup():
    assert canonical_section("technical   skills") == "Technical Skills"
    assert canonical_section("Basket Weaving") is None
    assert canonical_section(None) is None


def test_unknown_section_rejected():
    with pytest.raises(Exception):
        GroundTruthAnnotation(**_base(
            expected_present_sections=["Basket Weaving"]))


def test_present_missing_overlap_rejected():
    with pytest.raises(Exception):
        GroundTruthAnnotation(**_base(
            expected_missing_sections=["Education"]))


def test_score_out_of_range_rejected():
    with pytest.raises(Exception):
        GroundTruthAnnotation(**_base(expected_completeness_score=140))


def test_empty_score_becomes_none():
    model = GroundTruthAnnotation(**_base(expected_quality_score=""))
    assert model.expected_quality_score is None


def test_roles_labelled_empty_is_not_unlabelled():
    model = GroundTruthAnnotation(**_base(expected_suitable_roles=[]))
    assert model.expected_suitable_roles == []


def test_career_stage_required_and_known():
    with pytest.raises(Exception):
        GroundTruthAnnotation(**_base(career_stage=""))
    with pytest.raises(Exception):
        GroundTruthAnnotation(**_base(career_stage="Wizard"))


def test_domain_required():
    with pytest.raises(Exception):
        GroundTruthAnnotation(**_base(professional_domain="  "))


def test_validate_annotation_returns_error_not_raise():
    clean, error = validate_annotation(_base())
    assert error is None and clean is not None
    clean, error = validate_annotation(_base(career_stage="Wizard"))
    assert clean is None and error
