"""Calibration Phase 1: ground-truth import parsing + admin gate."""

from __future__ import annotations

import json

from calibration.auth import admin_token_configured, check_admin_token
from calibration.datasets import (
    content_hash, parse_ground_truth_csv, parse_ground_truth_json)

ROW = {
    "filename": "a.pdf",
    "expected_present_sections": ["Education"],
    "expected_missing_sections": ["Projects"],
    "expected_completeness_score": 70,
    "expected_quality_score": None,
    "expected_suitable_roles": None,
    "career_stage": "Student",
    "professional_domain": "Engineering",
}


def test_content_hash_known_vector():
    assert content_hash(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a"
        "9cb410ff61f20015ad")


def test_json_import_valid():
    annotations, errors = parse_ground_truth_json(json.dumps([ROW]))
    assert errors == []
    assert annotations["a.pdf"]["expected_present_sections"] == ["Education"]
    assert annotations["a.pdf"]["expected_suitable_roles"] is None


def test_json_import_collects_bad_rows():
    bad = dict(ROW, filename="b.pdf", career_stage="Wizard")
    annotations, errors = parse_ground_truth_json(json.dumps([ROW, bad]))
    assert list(annotations) == ["a.pdf"]
    assert len(errors) == 1 and errors[0]["row"] == "b.pdf"


def test_json_import_rejects_garbage():
    annotations, errors = parse_ground_truth_json("{not json")
    assert annotations == {} and errors


def test_json_import_duplicate_filename_flagged():
    annotations, errors = parse_ground_truth_json(json.dumps([ROW, ROW]))
    assert list(annotations) == ["a.pdf"]
    assert any("duplicate" in e["error"] for e in errors)


CSV_TEXT = (
    "filename,career_stage,professional_domain,present_sections,"
    "missing_sections,completeness_score,quality_score,suitable_roles,"
    "notes\n"
    "a.pdf,Student,Engineering,Education;Projects,Work Experience,70,,"
    "Data Analyst,first\n"
    "b.pdf,Fresher,Data,Education,Projects,,55,NONE,\n"
    "c.pdf,Fresher,Data,Education,Projects,,55,,\n"
)


def test_csv_import_semantics():
    annotations, errors = parse_ground_truth_csv(CSV_TEXT)
    assert errors == []
    assert annotations["a.pdf"]["expected_present_sections"] == [
        "Education", "Projects"]
    assert annotations["a.pdf"]["expected_suitable_roles"] == [
        "Data Analyst"]
    # NONE = labelled, no suitable role; empty = unlabelled.
    assert annotations["b.pdf"]["expected_suitable_roles"] == []
    assert annotations["c.pdf"]["expected_suitable_roles"] is None
    assert annotations["a.pdf"]["expected_quality_score"] is None


def test_admin_token_gate():
    env = {"SATVIQ_CALIBRATION_ADMIN_TOKEN": "s3cret"}
    assert admin_token_configured(env)
    assert check_admin_token("s3cret", env)
    assert not check_admin_token("wrong", env)
    assert not check_admin_token(None, env)
    # Fail closed when nothing is configured.
    assert not admin_token_configured({})
    assert not check_admin_token("s3cret", {})
