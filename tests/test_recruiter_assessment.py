"""Recruiter Assessment (eval-prompt/2.0) + canonical 1.1 tests.

Covers: RA schema validation (valid / missing / null / bad types),
the deterministic evidence rules (evidence-less points dropped;
unevidenced 'verified' gaps demoted to unmentioned areas),
historical-payload compatibility, prompt content, prompt-version
recording, and the canonical ai-evaluation/1.1 mapping.
"""

from __future__ import annotations

import copy
import json

import pytest

from candidate_intelligence import build_canonical, check_canonical
from evaluation import prompt as eval_prompt
from evaluation.schema import validate_evaluation


def _payload(**overrides):
    payload = {
        "candidate": {"name": "Asha Verma", "headline": "Backend Dev",
                      "location": "Mumbai", "email": "a@x.com",
                      "summary": "Backend developer."},
        "career_stage": "Experienced Professional",
        "professional_domain": "Software Engineering",
        "matching_sections": [],
        "missing_sections": [],
        "good_to_have": [],
        "completeness": {"score": 80, "classification": "Good",
                         "explanation": {"why_low": "x"}},
        "quality": {"score": 70, "classification": "Good",
                    "explanation": {"why_low": "y"}},
        "role_suitability": [
            {"role": "Backend Developer", "score": 88,
             "matching_skills": ["Python"], "missing_skills": ["Docker"],
             "relevant_projects": [], "relevant_experience": [],
             "evidence": [], "explanation": ""}],
        "suitable_roles": ["Backend Developer"],
        "strengths": [], "weaknesses": [], "recommendations": [],
        "recruiter_assessment": {
            "summary": "Backend developer with evidenced API work.",
            "strengths": [{"point": "Deep API experience",
                           "evidence": ["Built APIs for 40 clients"]}],
            "limitations": [{"point": "No quantified outcomes",
                             "evidence": ["No metrics in any bullet"]}],
            "assessment_insights": [
                {"point": "Skills corroborated by project work",
                 "evidence": ["FastAPI in skills and projects"]}],
            "verified_skill_gaps": [
                {"skill_or_area": "API testing",
                 "basis": "Claims API expertise; no testing practice "
                          "evidenced anywhere in described work",
                 "evidence": ["Experience bullets list no tests"]}],
            "unmentioned_areas": [
                {"area": "Docker",
                 "relevance_note": "Preferred for Backend Developer; "
                                   "simply not mentioned."}],
            "verification_points": ["Confirm personal contribution "
                                    "to the billing API"],
            "information_completeness_note": "Assessor has core "
                                             "history; outcomes are "
                                             "unverifiable from the "
                                             "document.",
            "data_reliability_note": "Claims are consistent but "
                                     "lightly evidenced; verify "
                                     "scale claims.",
        },
    }
    payload.update(overrides)
    return payload


def _validate(payload):
    return validate_evaluation(json.dumps(payload))


# --- schema validation ----------------------------------------------------
def test_valid_recruiter_assessment_passes():
    result, error = _validate(_payload())
    assert error is None, error
    ra = result["recruiter_assessment"]
    assert ra["summary"].startswith("Backend developer")
    assert ra["verified_skill_gaps"][0]["skill_or_area"] == "API testing"
    assert ra["unmentioned_areas"][0]["area"] == "Docker"


def test_historical_payload_without_ra_still_validates():
    payload = _payload()
    del payload["recruiter_assessment"]
    payload["recommendations"] = ["Add Docker experience"]
    result, error = _validate(payload)
    assert error is None
    assert result["recruiter_assessment"] is None
    assert result["recommendations"] == ["Add Docker experience"]


def test_ra_null_and_bad_types_are_repaired_or_rejected_safely():
    payload = _payload()
    payload["recruiter_assessment"]["strengths"] = [
        {"point": "X", "evidence": "not-a-list"}]
    result, error = _validate(payload)
    # lenient coercion turns the bare evidence string into a list
    assert error is None
    assert result["recruiter_assessment"]["strengths"][0][
        "evidence"] == ["not-a-list"]


# --- evidence rules ---------------------------------------------------------
def test_evidenceless_points_are_dropped():
    payload = _payload()
    payload["recruiter_assessment"]["strengths"] = [
        {"point": "Great communicator", "evidence": []},
        {"point": "Deep API experience",
         "evidence": ["Built APIs for 40 clients"]}]
    payload["recruiter_assessment"]["limitations"] = [
        {"point": "Vague concern", "evidence": []}]
    result, error = _validate(payload)
    assert error is None
    ra = result["recruiter_assessment"]
    assert [s["point"] for s in ra["strengths"]] == [
        "Deep API experience"]
    assert ra["limitations"] == []


def test_unevidenced_gap_is_demoted_never_stored_as_verified():
    payload = _payload()
    payload["recruiter_assessment"]["verified_skill_gaps"] = [
        {"skill_or_area": "Kubernetes", "basis": "",
         "evidence": []}]
    payload["recruiter_assessment"]["unmentioned_areas"] = []
    result, error = _validate(payload)
    assert error is None
    ra = result["recruiter_assessment"]
    assert ra["verified_skill_gaps"] == []
    assert ra["unmentioned_areas"][0]["area"] == "Kubernetes"
    assert "not evidence of absence" in ra["unmentioned_areas"][0][
        "relevance_note"]


def test_role_missing_skill_is_not_a_verified_gap():
    # Docker sits in role_suitability.missing_skills AND
    # unmentioned_areas in the fixture — and nowhere else.
    result, _ = _validate(_payload())
    gaps = [g["skill_or_area"] for g in
            result["recruiter_assessment"]["verified_skill_gaps"]]
    assert "Docker" not in gaps


# --- prompt -------------------------------------------------------------------
def test_prompt_is_recruiter_facing_and_versioned():
    text = eval_prompt.build_instructions()
    assert eval_prompt.PROMPT_VERSION == "eval-prompt/2.1"
    assert "RECRUITER ASSESSMENT" in text
    assert "CANDIDATE INFORMATION COMPLETENESS" in text
    assert "RESUME DATA RELIABILITY" in text
    assert "Leave EMPTY: top-level recommendations" in text
    assert "absence of evidence" in text


# --- canonical 1.1 --------------------------------------------------------------
def _record_with_ra():
    result, error = _validate(_payload())
    assert error is None
    entry = {"analysis_id": "ra-1", "status": "success",
             "analysis_version": 1, "model": "m",
             "created_at": "2026-11-12T00:00:00+00:00",
             "evaluation": result, "error": None}
    return {"resume_id": "ra-test-00000001", "filename": "a.pdf",
            "resume_json": {"full_name": "Asha Verma",
                            "skills": {"technical_skills": ["Python"]},
                            "experience": [], "education": [],
                            "certifications": []},
            "analyses": [entry], "analysis": entry}


def test_canonical_11_carries_recruiter_assessment():
    doc = build_canonical(_record_with_ra())
    ok, model = check_canonical(doc)
    assert ok, model
    assert doc["metadata"]["schema_version"] == "ai-evaluation/1.1"
    ra = doc["recruiter_assessment"]
    assert ra["summary"].startswith("Backend developer")
    assert ra["verified_skill_gaps"][0]["skill_or_area"] == \
        "API testing"
    # canonical strengths now carry the RA evidence
    assert doc["candidate_evaluation"]["strengths"][0]["evidence"] == [
        "Built APIs for 40 clients"]
    # verified gap also surfaces in gaps with its source labelled
    assert any(g.get("source") == "recruiter_assessment"
               for g in doc["gaps"]["detected_skill_gaps"])
    # unmentioned Docker is unverified info, not a gap
    assert "Docker" in doc["gaps"]["unverified_information"]


def test_canonical_historical_evaluation_has_null_ra():
    record = _record_with_ra()
    record["analysis"]["evaluation"] = dict(
        record["analysis"]["evaluation"])
    record["analysis"]["evaluation"]["recruiter_assessment"] = None
    record["analyses"] = [record["analysis"]]
    doc = build_canonical(record)
    ok, _ = check_canonical(doc)
    assert ok
    assert doc["recruiter_assessment"] is None


# --- regression: truncated response + JSON correction -------------------
class _TruncationMock:
    """Call 1 returns a response truncated before recruiter_assessment
    (the token-cap failure). Call 2 (correction) follows the CURRENT
    correction instructions: it completes the missing section from
    the resume text and returns the full evaluation."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def chat_json(self, instructions, user_text, extra=None):
        self.calls.append({"instructions": instructions,
                           "user_text": user_text})
        if len(self.calls) == 1:
            raw = json.dumps(self.payload)
            cut = raw.index('"recruiter_assessment"')
            return {"text": raw[:cut] + ', "strengths": ["cut',
                    "model": "muse-spark-1.3-contributor",
                    "usage": {"prompt_tokens": 4500,
                              "completion_tokens": 6000,
                              "total_tokens": 10500},
                    "request_id": "r1", "duration_ms": 9000,
                    "finish_reason": "length"}
        return {"text": json.dumps(self.payload),
                "model": "muse-spark-1.3-contributor",
                "usage": {"prompt_tokens": 4475,
                          "completion_tokens": 3084,
                          "total_tokens": 7559},
                "request_id": "r2", "duration_ms": 6000,
                "finish_reason": "stop"}


def test_correction_can_restore_recruiter_assessment():
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location(
        "te_recovery", "tests/test_evaluation.py")
    te = importlib.util.module_from_spec(spec)
    sys.modules["te_recovery"] = te
    spec.loader.exec_module(te)
    from evaluation.service import analyze_record
    from muse_ai.config import MuseConfig

    mock = _TruncationMock(_payload())
    record = te._record()
    try:
        entry, error = analyze_record(
            record, client=mock,
            config=MuseConfig(api_key="<redacted>"))
    finally:
        # analyze_record persists via the store — remove the test
        # record again so the suite never leaves files behind.
        from api import store as _store
        try:
            _store.delete_record(record["resume_id"])
        except Exception:
            pass
    assert error is None
    assert entry["status"] == "success"
    assert entry["recovery"]["success"] is True
    ev = entry["evaluation"]
    assert ev["recruiter_assessment"] is not None
    assert ev["recruiter_assessment"]["summary"].startswith(
        "Backend developer")
    # The correction request must carry the schema and the resume
    # text — the two things that make completion possible.
    correction = mock.calls[1]
    assert "recruiter_assessment" in correction["instructions"]
    assert "TRUNCATED" in correction["instructions"]
    assert "RESUME TEXT" in correction["user_text"]


# --- refinements: documentation gaps + section classification -----------
def test_documentation_topics_are_limitations_not_verified_gaps():
    payload = _payload()
    payload["recruiter_assessment"]["verified_skill_gaps"] = [
        {"skill_or_area": "Quantified project metrics",
         "basis": "Projects describe work without metrics",
         "evidence": ["No numbers in any project description"]},
        {"skill_or_area": "Deployment evidence",
         "basis": "No production deployment documented",
         "evidence": ["Projects list no deployment"]},
        {"skill_or_area": "API testing",
         "basis": "Claims API expertise; no testing evidenced",
         "evidence": ["Experience bullets list no tests"]}]
    result, error = _validate(payload)
    assert error is None
    ra = result["recruiter_assessment"]
    assert [g["skill_or_area"] for g in ra["verified_skill_gaps"]] == [
        "API testing"]
    limitation_points = " | ".join(
        lim["point"] for lim in ra["limitations"])
    assert "Quantified project metrics" in limitation_points
    assert "Deployment evidence" in limitation_points
    # and they were NOT pushed into unmentioned areas
    assert all("metric" not in (u["area"] or "").lower()
               for u in ra["unmentioned_areas"])


def test_section_classification_rules():
    from evaluation.content_evidence import (
        reconcile_section_classification)
    ev = {
        "career_stage": "Fresher",
        "matching_sections": [{"section": "Projects"}],
        "missing_sections": [
            {"section": "Academic Projects"},
            {"section": "Publications"},
            {"section": "Research Experience"},
            {"section": "Internship Experience"}],
    }
    actions = reconcile_section_classification(ev)
    remaining = [m["section"] for m in ev["missing_sections"]]
    assert remaining == ["Internship Experience"]
    assert len(actions) == 3


def test_section_classification_keeps_research_for_experienced():
    from evaluation.content_evidence import (
        reconcile_section_classification)
    ev = {
        "career_stage": "Experienced Professional",
        "matching_sections": [{"section": "Work Experience"}],
        "missing_sections": [{"section": "Research Experience"},
                             {"section": "Academic Projects"}],
    }
    reconcile_section_classification(ev)
    remaining = [m["section"] for m in ev["missing_sections"]]
    # Research kept (stage expects it); Academic Projects kept too —
    # Projects content was NOT recognised in this evaluation
    assert remaining == ["Research Experience", "Academic Projects"]


def test_prompt_carries_refinement_rules():
    text = eval_prompt.build_instructions()
    assert "NOT verified gaps" in text
    assert "APPLICABILITY" in text
    assert "subtype of" in text
