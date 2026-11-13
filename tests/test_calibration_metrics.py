"""Calibration Phase 1: metrics engine against a hand-computed
fixture. Every expected number below was derived by hand from the
three fixture resumes, not from the implementation."""

from __future__ import annotations

import math

from calibration.metrics import (
    aggregate_metrics, compare_resume, error_records)

GT_A = {
    "expected_present_sections": ["Education", "Projects",
                                  "Technical Skills"],
    "expected_missing_sections": ["Work Experience"],
    "expected_completeness_score": 80, "expected_quality_score": 70,
    "expected_suitable_roles": ["Data Analyst"],
    "career_stage": "Student", "professional_domain": "Engineering",
}
AI_A = {
    "matching_sections": [{"section": "Education"},
                          {"section": "Projects"}],
    "missing_sections": [{"section": "Work Experience"}],
    "completeness": {"score": 75}, "quality": {"score": 78},
    "suitable_roles": ["Data Analyst"],
    "role_suitability": [{"role": "Data Analyst", "score": 80},
                         {"role": "Software Engineer", "score": 40}],
}
GT_B = {
    "expected_present_sections": ["Education", "Work Experience"],
    "expected_missing_sections": ["Projects", "Certifications"],
    "expected_completeness_score": 60, "expected_quality_score": 55,
    "expected_suitable_roles": None,
    "career_stage": "Fresher", "professional_domain": "Engineering",
}
AI_B = {
    "matching_sections": [{"section": "Education"},
                          {"section": "Projects"}],
    "missing_sections": [{"section": "Certifications"}],
    "completeness": {"score": 90}, "quality": {"score": 50},
    "suitable_roles": [], "role_suitability": [],
}
GT_C = {
    "expected_present_sections": ["Education"],
    "expected_missing_sections": [],
    "expected_completeness_score": None, "expected_quality_score": None,
    "expected_suitable_roles": [],
    "career_stage": "Student", "professional_domain": "Data",
}
AI_C = {
    "matching_sections": [{"section": "Education"}],
    "missing_sections": [],
    "completeness": {"score": 50}, "quality": {"score": 50},
    "suitable_roles": ["Data Analyst"],
    "role_suitability": [{"role": "Data Analyst", "score": 60}],
}


def _all():
    return [compare_resume(GT_A, AI_A), compare_resume(GT_B, AI_B),
            compare_resume(GT_C, AI_C)]


def test_compare_resume_a_outcomes():
    comp = compare_resume(GT_A, AI_A)
    outcomes = {s: r["outcome"] for s, r in comp["sections"].items()}
    assert outcomes == {"Education": "tp", "Projects": "tp",
                        "Technical Skills": "fn",
                        "Work Experience": "tn"}
    assert comp["missing_flags"]["Work Experience"]["outcome"] == "tp"
    assert comp["score_errors"]["completeness"]["error"] == -5.0
    assert comp["roles"]["top1_hit"] is True


def test_section_prf_overall():
    overall = aggregate_metrics(_all())["sections"]["overall"]
    assert (overall["tp"], overall["fp"], overall["fn"],
            overall["tn"]) == (4, 1, 2, 2)
    assert overall["precision"] == 0.8
    assert math.isclose(overall["recall"], 4 / 6)
    assert math.isclose(overall["f1"], 2 * 0.8 * (4 / 6) / (0.8 + 4 / 6))


def test_missing_section_detection():
    missing = aggregate_metrics(_all())["missing_sections"]
    assert (missing["tp"], missing["fp"], missing["fn"]) == (2, 0, 1)
    assert missing["precision"] == 1.0
    assert math.isclose(missing["recall"], 2 / 3)


def test_score_agreement_completeness():
    block = aggregate_metrics(_all())["scores"]["completeness"]
    assert block["n"] == 2  # resume C has no human score — excluded
    assert block["mae"] == 17.5
    assert math.isclose(block["rmse"], math.sqrt(462.5))
    assert block["bias"] == 12.5
    assert block["within_5_pct"] == 0.5
    assert block["within_10_pct"] == 0.5
    assert block["pearson"] == -1.0
    assert block["spearman"] == -1.0


def test_score_agreement_quality():
    block = aggregate_metrics(_all())["scores"]["quality"]
    assert block["mae"] == 6.5
    assert block["bias"] == 1.5
    assert block["within_10_pct"] == 1.0


def test_role_metrics_only_from_labelled():
    roles = aggregate_metrics(_all())["roles"]
    assert roles["labelled_resumes"] == 2  # B is unlabelled
    assert roles["precision"] == 0.5
    assert roles["recall"] == 1.0
    assert roles["top1_accuracy"] == 0.5
    assert roles["top3_accuracy"] == 0.5


def test_per_section_and_stage_breakdowns():
    metrics = aggregate_metrics(_all())
    skills = metrics["sections"]["per_section"]["Technical Skills"]
    assert skills["recall"] == 0.0 and skills["precision"] is None
    projects = metrics["sections"]["per_section"]["Projects"]
    assert (projects["tp"], projects["fp"]) == (1, 1)
    student = metrics["sections"]["per_career_stage"]["Student"]
    assert (student["tp"], student["fn"], student["tn"]) == (3, 1, 1)


def test_insufficient_data_is_none_never_zero():
    metrics = aggregate_metrics([])
    assert metrics["resumes_compared"] == 0
    assert metrics["sections"]["overall"]["precision"] is None
    assert metrics["scores"]["completeness"] is None
    assert metrics["roles"] is None


def test_unlabelled_roles_resume_alone_gives_no_role_block():
    metrics = aggregate_metrics([compare_resume(GT_B, AI_B)])
    assert metrics["roles"] is None


def test_error_records_for_b():
    records = error_records(compare_resume(GT_B, AI_B), "resume-b")
    kinds = sorted(r["error_type"] for r in records)
    assert kinds == ["false_negative_section", "false_positive_section",
                     "missed_missing_section", "score_deviation"]
    by_type = {r["error_type"]: r for r in records}
    assert by_type["false_negative_section"]["section_name"] == \
        "Work Experience"
    assert by_type["false_negative_section"]["severity"] == "high"
    assert by_type["score_deviation"]["severity"] == "high"
    assert all(r["calibration_resume_id"] == "resume-b" for r in records)


def test_no_errors_for_perfect_match():
    gt = {"expected_present_sections": ["Education"],
          "expected_missing_sections": ["Projects"],
          "expected_completeness_score": 90,
          "expected_quality_score": 90,
          "expected_suitable_roles": None,
          "career_stage": "Student", "professional_domain": "Data"}
    ai = {"matching_sections": [{"section": "Education"}],
          "missing_sections": [{"section": "Projects"}],
          "completeness": {"score": 92}, "quality": {"score": 88},
          "suitable_roles": [], "role_suitability": []}
    assert error_records(compare_resume(gt, ai), "r") == []
