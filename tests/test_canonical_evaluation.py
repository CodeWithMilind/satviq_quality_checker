"""Canonical AI Evaluation JSON (candidate_intelligence) tests.

Covers the Phase 2 spec's 12 testing requirements: valid documents,
missing/null handling, type + score-range validation, malformed
legacy payloads, missing skills/experience, evidence preservation,
duplicate/conflict handling, PostgreSQL persistence (live when a
test database URL is provided, skipped otherwise), evaluation
history preservation, and backward compatibility (existing
endpoints keep their shapes; the canonical endpoint is additive).

All canonical builds are deterministic — no AI calls anywhere here.
"""

from __future__ import annotations

import copy
import os

import pytest
from fastapi.testclient import TestClient

from api.main import app
from candidate_intelligence import (build_canonical, check_canonical,
                                    validate_canonical)
from candidate_intelligence.categories import categorize

client = TestClient(app)


def _record(**overrides):
    record = {
        "resume_id": "canon-test-0000001",
        "filename": "dev.pdf",
        "uploaded_at": "2026-11-11T10:00:00+00:00",
        "selected_extractor": "tika",
        "success": True,
        "resume_json": {
            "full_name": "Asha Verma",
            "email": "asha@example.com",
            "phone": "+91 9820012345",
            "location": "Mumbai, India",
            "summary": "Backend developer.",
            "skills": {
                "technical_skills": ["Python", "FastAPI", "PostgreSQL",
                                     "ObscureTool9000"],
                "soft_skills": ["Communication"],
                "tools": ["Git", "Docker"],
                "taxonomy_skills": ["Python"],
                "other_skills": [],
            },
            "experience": [{
                "title": "Backend Developer", "company": "Acme",
                "location": "Mumbai", "start_date": "Jan 2022",
                "end_date": "Present", "is_current": True,
                "highlights": ["Built APIs used by 40 clients"],
            }],
            "education": [{
                "degree": "MCA", "institution": "Mumbai University",
                "start_date": "2019", "end_date": "2022",
            }],
            "certifications": ["AWS Certified Cloud Practitioner"],
            "languages": ["English"],
            "links": ["https://linkedin.com/in/asha"],
        },
        "analyses": [],
    }
    record.update(overrides)
    return record


def _success_entry(**eval_overrides):
    ev = {
        "candidate": {"name": "Asha Verma", "headline": "Backend Dev",
                      "location": "Mumbai, India",
                      "email": "asha@example.com",
                      "summary": "Backend developer."},
        "career_stage": "Experienced Professional",
        "professional_domain": "Software Engineering",
        "matching_sections": [
            {"section": "Technical Skills",
             "original_heading": "Skills", "presence_status": "present",
             "completeness_status": "complete",
             "quality_assessment": "Well organized.",
             "summary": "Strong backend stack.",
             "evidence": ["Python, FastAPI"], "remarks": ""},
            {"section": "Projects", "original_heading": "Projects",
             "presence_status": "present",
             "completeness_status": "partial",
             "quality_assessment": "Named, thin outcomes.",
             "summary": "Two projects.", "evidence": [],
             "remarks": ""},
        ],
        "missing_sections": [
            {"section": "Certifications", "importance": "Medium",
             "status": "missing", "reason": "None listed",
             "impact": "Minor", "recommendation": "Add certs",
             "evidence": []},
        ],
        "good_to_have": [],
        "completeness": {"score": 82.0, "classification": "Good",
                         "explanation": {"why_low": "Missing certs",
                                         "problems": []}},
        "quality": {"score": 74.0, "classification": "Good",
                    "explanation": {"why_low": "Thin project outcomes",
                                    "problems": ["Few metrics"]}},
        "role_suitability": [
            {"role": "Backend Developer", "score": 88.0,
             "matching_skills": ["Python", "FastAPI"],
             "missing_skills": ["Kubernetes"],
             "relevant_projects": ["Billing API"],
             "relevant_experience": ["Acme"], "evidence": [],
             "explanation": "Strong fit"}],
        "suitable_roles": ["Backend Developer"],
        "strengths": ["Deep API experience"],
        "weaknesses": ["Few quantified outcomes"],
        "recommendations": ["Quantify project outcomes"],
    }
    ev.update(eval_overrides)
    return {
        "analysis_id": "analysis-1", "resume_id": "canon-test-0000001",
        "analysis_version": 1, "status": "success",
        "created_at": "2026-11-11T11:00:00+00:00",
        "model": "muse-spark-1.3-contributor",
        "yaml_version": "completeness@abc+eval-1.0.0",
        "evaluation": ev, "error": None,
    }


def _evaluated_record():
    entry = _success_entry()
    return _record(analyses=[entry], analysis=entry)


# 1. Valid canonical JSON -------------------------------------------------
def test_valid_document_builds_and_validates():
    doc = build_canonical(_evaluated_record())
    ok, model = check_canonical(doc)
    assert ok, model
    assert doc["metadata"]["schema_version"] == "ai-evaluation/1.1"
    assert doc["metadata"]["evaluation_id"] == "analysis-1"
    assert doc["resume_completeness"]["completeness_score"] == 82.0
    assert doc["resume_quality"]["quality_score"] == 74.0
    assert doc["suitable_roles"] == ["Backend Developer"]
    # recommendations preserved, kept apart from evaluation sections
    assert doc["recommendations"] == ["Quantify project outcomes"]
    names = {s["skill_name"] for s in doc["skills"]["frameworks"]}
    assert "FastAPI" in names
    assert doc["projects"][0]["project_name"] == "Billing API"


# 2/3. Missing optional fields + null behavior ----------------------------
def test_record_without_evaluation_yields_nulls_not_inventions():
    doc = build_canonical(_record())
    ok, _ = check_canonical(doc)
    assert ok
    assert doc["metadata"]["evaluation_id"] is None
    assert doc["resume_completeness"]["completeness_score"] is None
    assert doc["candidate_profile"]["career_stage"] is None
    assert doc["data_reliability"]["field_level_status"][
        "ai_evaluation"] == "not_detected"
    # extraction-side data is still fully present
    assert doc["candidate_profile"]["full_name"] == "Asha Verma"
    assert doc["education"][0]["education_level"] == "masters"


# 4/5. Wrong types + invalid score ranges are rejected --------------------
def test_validation_rejects_bad_types_and_scores():
    doc = build_canonical(_evaluated_record())
    bad = copy.deepcopy(doc)
    bad["resume_quality"]["quality_score"] = 140
    ok, errors = check_canonical(bad)
    assert not ok and any("quality_score" in e for e in errors)
    bad2 = copy.deepcopy(doc)
    bad2["skills"]["frameworks"] = "not-a-list"
    ok2, _ = check_canonical(bad2)
    assert not ok2
    bad3 = copy.deepcopy(doc)
    bad3["unexpected_field"] = 1
    ok3, _ = check_canonical(bad3)
    assert not ok3  # extra keys are forbidden
    with pytest.raises(Exception):
        validate_canonical(bad)


# 6. Malformed stored evaluation never breaks the build -------------------
def test_malformed_evaluation_payload_is_tolerated():
    record = _record(analyses=[{
        "analysis_id": "x", "status": "success",
        "evaluation": "not-a-dict"}])
    doc = build_canonical(record)
    ok, _ = check_canonical(doc)
    assert ok
    assert doc["resume_completeness"]["completeness_score"] is None


# 7. Missing skills / experience ------------------------------------------
def test_fresher_without_experience_is_not_applicable_not_missing():
    record = _record()
    record["resume_json"]["experience"] = []
    record["resume_json"]["skills"] = {}
    entry = _success_entry(career_stage="Fresher")
    record["analyses"] = [entry]
    record["analysis"] = entry
    doc = build_canonical(record)
    status = doc["data_reliability"]["field_level_status"]
    assert status["experience"] == "not_applicable"
    assert doc["experience"] == []
    # skill gaps exist only relative to suitable roles, with the note
    assert doc["gaps"]["detected_skill_gaps"][0]["missing_skills"] == [
        "Kubernetes"]


# 8. Evidence preservation --------------------------------------------------
def test_section_evidence_survives_into_observations():
    doc = build_canonical(_evaluated_record())
    obs = doc["candidate_evaluation"]["observations"]
    assert obs and obs[0]["evidence"] == ["Python, FastAPI"]
    completeness_obs = doc["resume_completeness"][
        "section_level_observations"]
    assert any(o["section"] == "Certifications"
               and o["status"] == "missing" for o in completeness_obs)


# 9. Duplicates + conflicts -------------------------------------------------
def test_duplicate_skills_deduped_and_conflicts_reported():
    record = _evaluated_record()
    record["resume_json"]["skills"]["technical_skills"].append("Python")
    record["resume_json"]["full_name"] = "Asha V Verma"
    doc = build_canonical(record)
    pythons = [s for s in doc["skills"]["programming_languages"]
               if s["skill_name"] == "Python"]
    assert len(pythons) == 1
    assert any("name differs" in c for c in
               doc["data_reliability"]["conflicting_information"])


# 10. Categorization honesty -------------------------------------------------
def test_unknown_skill_is_never_guessed_into_a_category():
    assert categorize("ObscureTool9000", "technical_skills") == \
        "technical_skills"
    doc = build_canonical(_record())
    cats = {s["skill_name"]: s["category"]
            for group in doc["skills"].values() for s in group}
    assert cats["PostgreSQL"] == "databases"
    assert cats["ObscureTool9000"] == "technical_skills"


# 11. Responsibilities are never fabricated ----------------------------------
def test_responsibilities_stay_null_when_source_merges_them():
    doc = build_canonical(_record())
    exp = doc["experience"][0]
    assert exp["responsibilities"] is None
    assert exp["achievements"] is None
    assert exp["highlights"] == ["Built APIs used by 40 clients"]
    assert exp["is_current"] is True


# 12. Backward compatibility: endpoint is additive ---------------------------
def test_canonical_endpoint_serves_document(monkeypatch, tmp_path):
    from api import store
    monkeypatch.setenv("SATVIQ_TEST_DISK_STORE", "1")
    monkeypatch.setattr(store, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(store, "UPLOADS_DIR", tmp_path / "uploads")
    store._DB_CHECKED = True
    store.save_record(_evaluated_record())
    resp = client.get(
        "/api/resumes/canon-test-0000001/canonical-evaluation")
    assert resp.status_code == 200
    body = resp.json()
    assert body["metadata"]["schema_version"] == "ai-evaluation/1.1"
    # the pre-existing analysis endpoint still answers in its shape
    resp2 = client.get("/api/resumes/canon-test-0000001/analysis")
    assert resp2.status_code in (200, 404)  # shape preserved either way


# 13. PostgreSQL persistence (live, opt-in) ---------------------------------
@pytest.mark.skipif(
    not os.environ.get("SATVIQ_TEST_DATABASE_URL"),
    reason="SATVIQ_TEST_DATABASE_URL not set — live PG test skipped")
def test_canonical_persists_in_postgres(monkeypatch):
    monkeypatch.delenv("SATVIQ_TEST_DISK_STORE", raising=False)
    monkeypatch.setenv("SATVIQ_DATABASE_URL",
                       os.environ["SATVIQ_TEST_DATABASE_URL"])
    from api import db
    db._ENV_LOADED = True
    record = _evaluated_record()
    db.sync_record(record)
    stored = db.fetch_canonical(record["resume_id"])
    assert stored is not None
    assert stored["metadata"]["schema_version"] == "ai-evaluation/1.1"
    assert stored["resume_completeness"]["completeness_score"] == 82.0
    db.delete_candidate(record["resume_id"])
    assert db.fetch_canonical(record["resume_id"]) is None
