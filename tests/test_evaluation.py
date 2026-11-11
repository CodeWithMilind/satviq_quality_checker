"""Tests for the Muse AI Resume Evaluation platform (evaluation/).

Everything runs against a MOCK Muse client — no real API calls, no
API credits. Coverage: end-to-end analysis persistence + versioning,
failure preservation, the unconfigured path, the already-running
guard, deterministic post-validation (score clamping, classification
recompute, suitable_roles recompute), the fresher evaluation shape,
evaluation-config validation, the chat_json HTTP-400 fallback, the
API endpoints, and the Analytics evaluation block.

The record store is redirected to a tmp directory, so these tests
are safe to run in the real project tree.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import store
from api.analytics import compute_analytics
from api.main import app
from evaluation import config as eval_config
from evaluation import prompt as eval_prompt
from evaluation import service as eval_service
from evaluation.schema import validate_evaluation
from muse_ai.client import MuseClient, MuseError
from muse_ai.config import MuseConfig

client = TestClient(app)

RESUME_ID = "eval-test-00000001"
FRESHER_TEXT = (
    "Test Fresher\ntest.fresher@example.com\nPune, India\n"
    "Candidate Snapshot\nMCA student building AI projects.\n"
    "Skill Inventory\nPython, FastAPI, SQL, Git\n"
    "Build Showcase\nData4Viz — data analysis workbench.\n"
    "Learning Journey\nMCA, DPGU, 2027.\n"
    "Credential Vault\nNASSCOM Certified Data Scientist.\n")


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(store, "UPLOADS_DIR", tmp_path / "uploads")
    return tmp_path


def _record(resume_id=RESUME_ID, text=FRESHER_TEXT):
    return {
        "resume_id": resume_id,
        "filename": "fresher.pdf",
        "uploaded_at": "2026-11-11T10:00:00+00:00",
        "extractor_used": "tika",
        "success": True,
        "extracted_text": text,
        "final_extracted_text": text,
        "text_normalization": {
            "status": "applied", "normalized_text": text,
            "normalization_applied": True},
        "quality": {
            "status": "WARNING", "source_tool": "tika",
            "text_health": {"score": 100.0},
            "completeness": {
                "score": 67.0,
                "metrics": {
                    "detected_sections": ["Contact Information",
                                          "Education"],
                    "missing_sections": ["Skills", "Summary"]}},
            "reading_order": {"score": 99.0}},
        "resume_json": {
            "full_name": "Test Fresher",
            "email": "test.fresher@example.com", "location": "Pune, India",
            "skills": {"technical_skills": ["Python", "FastAPI"]},
            "experience": [], "education": [], "certifications": []},
    }


def _eval_payload(**overrides):
    payload = {
        "candidate": {
            "name": "Test Fresher", "headline": "",
            "location": "Pune, India",
            "email": "test.fresher@example.com",
            "summary": "MCA student with Python and AI project work."},
        "career_stage": "Fresher",
        "professional_domain": "Software / Artificial Intelligence",
        "matching_sections": [
            {"section": "Technical Skills",
             "original_heading": "Skill Inventory",
             "presence_status": "present",
             "completeness_status": "complete",
             "quality_assessment": "Clear, relevant list.",
             "summary": "Python, FastAPI, SQL, Git.",
             "evidence": ["Skill Inventory"],
             "remarks": "Recognised under a nonstandard heading."},
            {"section": "Projects",
             "original_heading": "Build Showcase",
             "presence_status": "present",
             "completeness_status": "partial",
             "quality_assessment": "Named but thinly described.",
             "summary": "Data4Viz workbench.",
             "evidence": ["Build Showcase"], "remarks": ""}],
        "missing_sections": [
            {"section": "Internship Experience", "importance": "High",
             "status": "missing",
             "reason": "No internship is mentioned anywhere.",
             "impact": "Less evidence of workplace readiness.",
             "recommendation": "Add internships with outcomes.",
             "evidence": []}],
        "good_to_have": [
            {"section": "GitHub", "present": False, "relevance": "High",
             "potential_value": "Shows the project code.",
             "recommendation": "Link the GitHub profile."}],
        "completeness": {
            "score": 78, "classification": "Good",
            "explanation": {
                "why_low": "Internship and summary depth are missing.",
                "contributing_sections": ["Technical Skills"],
                "missing_information": ["Internship details"],
                "problems": [], "improvements": ["Add an internship."],
                "priorities": ["Add an internship."]}},
        "quality": {
            "score": 66, "classification": "Average",
            "explanation": {
                "why_low": "Project descriptions lack outcomes.",
                "contributing_sections": ["Projects"],
                "missing_information": ["Measurable results"],
                "problems": ["Thin project descriptions"],
                "improvements": ["Quantify project outcomes."],
                "priorities": ["Quantify project outcomes."]}},
        "role_suitability": [
            {"role": "Python Developer", "score": 82,
             "matching_skills": ["Python", "FastAPI"],
             "missing_skills": ["Docker"],
             "relevant_projects": ["Data4Viz"],
             "relevant_experience": [],
             "evidence": ["Skill Inventory lists Python"],
             "explanation": "Strong project-backed Python evidence."},
            {"role": "AI Engineer", "score": 71,
             "matching_skills": ["Python"],
             "missing_skills": ["RAG", "Vector databases"],
             "relevant_projects": ["Data4Viz"],
             "relevant_experience": [],
             "evidence": ["AI project work"],
             "explanation": "Good foundation, LLM depth missing."},
            {"role": "Data Analyst", "score": 45,
             "matching_skills": ["SQL"],
             "missing_skills": ["Excel", "Tableau"],
             "relevant_projects": [], "relevant_experience": [],
             "evidence": ["SQL listed"],
             "explanation": "Only a keyword-level match."}],
        "suitable_roles": ["Python Developer"],
        "strengths": ["Real project work with Python."],
        "weaknesses": ["No internship evidence."],
        "recommendations": ["Add internship experience."],
    }
    payload.update(overrides)
    return payload


class FakeEvalClient:
    """MuseClient stand-in: returns a canned chat_json result."""

    def __init__(self, payload=None, *, text=None, error=None,
                 usage=None):
        self.payload = payload
        self.text = text
        self.error = error
        self.usage = usage if usage is not None else {
            "prompt_tokens": 1200, "completion_tokens": 800,
            "total_tokens": 2000,
            "completion_tokens_details": {"reasoning_tokens": 120}}
        self.calls = []

    def chat_json(self, instructions, user_text, extra=None):
        self.calls.append({"instructions": instructions,
                           "user_text": user_text, "extra": extra})
        if self.error is not None:
            raise self.error
        text = self.text
        if text is None:
            text = json.dumps(self.payload or _eval_payload())
        return {"text": text, "usage": self.usage,
                "model": "muse-spark-1.3-contributor",
                "request_id": "req-test", "duration_ms": 1234.5}


def _configured():
    return MuseConfig(api_key="test-key")


# --------------------------------------------------------------------- #
# Service: happy path, persistence, versioning
# --------------------------------------------------------------------- #

def test_valid_evaluation_end_to_end(store_dir):
    record = _record()
    store.save_record(record)
    fake = FakeEvalClient()
    entry, error = eval_service.analyze_record(
        record, client=fake, config=_configured())
    assert error is None
    assert entry["status"] == "success"
    assert entry["analysis_version"] == 1
    assert entry["model"] == "muse-spark-1.3-contributor"
    assert entry["yaml_version"].startswith("completeness@")
    assert entry["yaml_version"].endswith("+eval-1.0.0")
    assert entry["tokens"] == {"input": 1200, "output": 800,
                                "total": 2000}
    assert entry["reasoning_tokens"] == 120
    assert entry["usage_type"] == "evaluation"
    # The call carried the reasoning/JSON safeguards.
    extra = fake.calls[0]["extra"]
    assert extra["response_format"] == {"type": "json_object"}
    assert extra["reasoning_effort"] == "low"
    assert extra["max_completion_tokens"] == 9000  # default raised 6000->9000 in the RA truncation fix (env override unchanged)
    # The prompt sent the selected text + QC context, nothing else.
    assert "Skill Inventory" in fake.calls[0]["user_text"]
    assert "DETERMINISTIC QUALITY CHECKER CONTEXT" in \
        fake.calls[0]["user_text"]
    # Persisted on the record.
    stored = store.load_raw_record(RESUME_ID)
    assert stored["analysis"]["analysis_id"] == entry["analysis_id"]
    assert len(stored["analyses"]) == 1

    # A second run increments the version and becomes the latest.
    entry2, error2 = eval_service.analyze_record(
        stored, client=FakeEvalClient(), config=_configured())
    assert error2 is None
    assert entry2["analysis_version"] == 2
    stored = store.load_raw_record(RESUME_ID)
    assert len(stored["analyses"]) == 2
    assert stored["analysis"]["analysis_version"] == 2


def test_invalid_json_failure_preserves_previous_success(store_dir):
    record = _record()
    store.save_record(record)
    entry, _ = eval_service.analyze_record(
        record, client=FakeEvalClient(), config=_configured())
    assert entry["status"] == "success"

    stored = store.load_raw_record(RESUME_ID)
    bad = FakeEvalClient(text="this is not json at all")
    entry2, error2 = eval_service.analyze_record(
        stored, client=bad, config=_configured())
    assert entry2["status"] == "failed"
    assert "validation" in entry2["error"]
    assert error2 == entry2["error"]
    # The failed first response triggers ONE JSON-correction call,
    # which also returns non-JSON here, so the attempt still fails —
    # and the tokens of BOTH calls are recorded (actual usage).
    assert entry2["recovery"]["attempted"] is True
    assert entry2["recovery"]["success"] is False
    assert entry2["tokens"]["total"] == 4000
    stored = store.load_raw_record(RESUME_ID)
    assert len(stored["analyses"]) == 2
    assert stored["analysis"]["analysis_id"] == entry["analysis_id"]
    assert stored["analysis"]["status"] == "success"


def test_unconfigured_muse_records_failed_entry(store_dir):
    record = _record()
    store.save_record(record)
    entry, error = eval_service.analyze_record(
        record, config=MuseConfig())  # no API key
    assert entry is not None
    assert entry["status"] == "failed"
    assert "not configured" in entry["error"]
    assert entry["tokens"] == {"input": None, "output": None,
                               "total": None}
    stored = store.load_raw_record(RESUME_ID)
    assert len(stored["analyses"]) == 1
    assert "analysis" not in stored or stored.get("analysis") is None


def test_transient_error_is_retried(store_dir):
    record = _record()
    store.save_record(record)

    class FlakyClient(FakeEvalClient):
        def chat_json(self, instructions, user_text, extra=None):
            if not self.calls:
                self.calls.append({})
                raise MuseError("HTTP 503", status=503)
            return super().chat_json(instructions, user_text, extra)

    entry, error = eval_service.analyze_record(
        record, client=FlakyClient(), config=_configured())
    assert error is None
    assert entry["status"] == "success"


def test_already_running_guard(store_dir):
    record = _record()
    store.save_record(record)
    assert eval_service._try_acquire(RESUME_ID)
    try:
        entry, error = eval_service.analyze_record(
            record, client=FakeEvalClient(), config=_configured())
        assert entry is None
        assert error == "already_running"
        assert eval_service.analysis_in_progress(RESUME_ID)
    finally:
        eval_service._release(RESUME_ID)
    assert not eval_service.analysis_in_progress(RESUME_ID)
    # Nothing was persisted for the refused attempt.
    assert store.load_raw_record(RESUME_ID).get("analyses") in (None, [])


# --------------------------------------------------------------------- #
# Schema post-validation
# --------------------------------------------------------------------- #

def test_scores_clamped_and_suitable_roles_recomputed():
    payload = _eval_payload()
    payload["completeness"]["score"] = 140
    payload["completeness"]["classification"] = "Wrong Label"
    payload["quality"]["score"] = -5
    payload["role_suitability"][0]["score"] = 120
    payload["suitable_roles"] = ["Data Analyst"]  # disagrees: 45 < 50
    parsed, error = validate_evaluation(json.dumps(payload))
    assert error is None
    assert parsed["completeness"]["score"] == 100
    assert parsed["completeness"]["classification"] == "Excellent"
    assert parsed["quality"]["score"] == 0
    assert parsed["quality"]["classification"] == "Poor"
    assert parsed["role_suitability"][0]["score"] == 100
    # Recomputed: >= 50, best first — Data Analyst (45) excluded.
    assert parsed["suitable_roles"] == ["Python Developer", "AI Engineer"]


def test_fresher_evaluation_shape():
    parsed, error = validate_evaluation(json.dumps(_eval_payload()))
    assert error is None
    assert parsed["career_stage"] == "Fresher"
    assert parsed["completeness"]["score"] == 78
    assert parsed["completeness"]["classification"] == "Good"
    assert parsed["quality"]["classification"] == "Average"
    # The nonstandard headings were recognised by meaning.
    headings = {s["section"]: s["original_heading"]
                for s in parsed["matching_sections"]}
    assert headings["Technical Skills"] == "Skill Inventory"
    assert headings["Projects"] == "Build Showcase"
    # A fresher's missing list does not include Work Experience as a
    # penalty item in this fixture — the schema stores it as given.
    assert parsed["missing_sections"][0]["importance"] == "High"


def test_validate_evaluation_rejects_garbage():
    parsed, error = validate_evaluation("no json here")
    assert parsed is None
    assert error
    parsed, error = validate_evaluation("")
    assert parsed is None
    assert error


# --------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------- #

def test_shipped_config_is_valid():
    config = eval_config.load_config()
    assert config["config_version"] == "eval-1.0.0"
    assert len(config["role_taxonomy"]) == 34
    assert len(config["standard_sections"]) == 15
    assert len(config["good_to_have_sections"]) == 12
    assert eval_config.classification_for(85) == "Excellent"
    assert eval_config.classification_for(84.9) == "Good"
    assert eval_config.classification_for(0) == "Poor"


def test_config_validation_failures():
    config = copy.deepcopy(eval_config.load_config())

    bad_bands = copy.deepcopy(config)
    bad_bands["classification_bands"][1]["min_score"] = 90
    with pytest.raises(ValueError, match="strictly decreasing"):
        eval_config.validate_config(bad_bands)

    dup_roles = copy.deepcopy(config)
    dup_roles["role_taxonomy"].append(
        copy.deepcopy(dup_roles["role_taxonomy"][0]))
    with pytest.raises(ValueError, match="Duplicate role"):
        eval_config.validate_config(dup_roles)

    dup_sections = copy.deepcopy(config)
    dup_sections["standard_sections"].append(
        copy.deepcopy(dup_sections["standard_sections"][0]))
    with pytest.raises(ValueError, match="Duplicate standard section"):
        eval_config.validate_config(dup_sections)

    no_skills = copy.deepcopy(config)
    no_skills["role_taxonomy"][0]["required_skills"] = []
    with pytest.raises(ValueError, match="required skill"):
        eval_config.validate_config(no_skills)


def test_public_metadata_has_no_secrets():
    metadata = eval_config.public_metadata()
    assert metadata["config_version"] == "eval-1.0.0"
    assert len(metadata["roles"]) == 34
    assert "api_key" not in json.dumps(metadata).lower()


def test_yaml_version_label_has_single_eval_prefix():
    # Regression: the label was "completeness@<hash>+eval-eval-1.0.0"
    # because config_version already carries the "eval-" prefix.
    version = eval_config.yaml_version()
    assert version.startswith("completeness@")
    assert version.endswith("+eval-1.0.0")
    assert "eval-eval" not in version


# --------------------------------------------------------------------- #
# Prompt assembly
# --------------------------------------------------------------------- #

def test_prompt_uses_selected_text_and_qc_context():
    record = _record()
    instructions, user_text = eval_prompt.build_prompt(record)
    assert "Candidate Snapshot" in user_text  # the selected text
    assert "DETERMINISTIC QUALITY CHECKER CONTEXT" in user_text
    assert "Completeness: 67.0/100" in user_text
    assert "ALREADY-EXTRACTED RESUME JSON" in user_text
    # Instructions encode the hard rules.
    assert "NEVER classify a section as missing" in instructions
    assert "no combined" in instructions.lower() or \
        "never compute or imply a combined" in instructions.lower()
    assert "Candidate Snapshot" in instructions


# --------------------------------------------------------------------- #
# MuseClient.chat_json payload behaviour
# --------------------------------------------------------------------- #

def _fake_http(responses):
    calls = []

    def fake(url, payload, headers, timeout=None):
        calls.append(copy.deepcopy(payload))
        outcome = responses[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return calls, fake


def _ok_reply():
    return {"choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2,
                      "total_tokens": 3},
            "model": "muse-spark-1.3-contributor", "id": "r1"}


def test_chat_json_sends_extras(monkeypatch):
    calls, fake = _fake_http([_ok_reply()])
    monkeypatch.setattr("muse_ai.client.http_post_json", fake)
    muse = MuseClient(MuseConfig(api_key="k"))
    result = muse.chat_json("inst", "user", extra={
        "response_format": {"type": "json_object"},
        "reasoning_effort": "low", "max_completion_tokens": 4000})
    assert result["text"] == "{}"
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["reasoning_effort"] == "low"
    assert calls[0]["max_completion_tokens"] == 4000


def test_chat_json_400_fallback_drops_params_in_order(monkeypatch):
    bad = MuseError("HTTP 400: unsupported parameter", status=400)
    calls, fake = _fake_http([bad, bad, _ok_reply()])
    monkeypatch.setattr("muse_ai.client.http_post_json", fake)
    muse = MuseClient(MuseConfig(api_key="k"))
    result = muse.chat_json("inst", "user", extra={
        "response_format": {"type": "json_object"},
        "reasoning_effort": "low"})
    assert result["text"] == "{}"
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]
    assert calls[1]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in calls[2]


def test_chat_json_non_400_not_retried(monkeypatch):
    calls, fake = _fake_http([MuseError("HTTP 500", status=500)])
    monkeypatch.setattr("muse_ai.client.http_post_json", fake)
    muse = MuseClient(MuseConfig(api_key="k"))
    with pytest.raises(MuseError):
        muse.chat_json("inst", "user", extra={
            "response_format": {"type": "json_object"}})
    assert len(calls) == 1


# --------------------------------------------------------------------- #
# API endpoints
# --------------------------------------------------------------------- #

def _patch_service(monkeypatch, fake):
    monkeypatch.setattr(eval_service, "load_muse_config",
                        lambda: MuseConfig(api_key="test-key"))
    monkeypatch.setattr(eval_service, "MuseClient",
                        lambda config: fake)


def test_analysis_endpoints(store_dir, monkeypatch):
    store.save_record(_record())
    _patch_service(monkeypatch, FakeEvalClient())

    response = client.get(f"/api/resumes/{RESUME_ID}/analysis")
    assert response.status_code == 200
    assert response.json() == {"analysis": None}

    response = client.post(f"/api/resumes/{RESUME_ID}/analyze")
    assert response.status_code == 200
    body = response.json()
    assert body["analysis"]["status"] == "success"
    assert body["analysis"]["evaluation"]["career_stage"] == "Fresher"
    assert body["record"]["analysis"]["status"] == "success"

    response = client.get(f"/api/resumes/{RESUME_ID}/analysis")
    assert response.json()["analysis"]["analysis_version"] == 1

    response = client.post(f"/api/resumes/{RESUME_ID}/analyze")
    assert response.status_code == 200
    response = client.get(f"/api/resumes/{RESUME_ID}/analysis/history")
    history = response.json()["history"]
    assert len(history) == 2
    assert [h["analysis_version"] for h in history] == [1, 2]


def test_analysis_endpoints_unknown_resume(store_dir):
    assert client.post("/api/resumes/nope-nope-0000/analyze").status_code == 404
    assert client.get("/api/resumes/nope-nope-0000/analysis").status_code == 404
    assert client.get(
        "/api/resumes/nope-nope-0000/analysis/history").status_code == 404


def test_analyze_endpoint_conflict_when_running(store_dir, monkeypatch):
    store.save_record(_record())
    _patch_service(monkeypatch, FakeEvalClient())
    assert eval_service._try_acquire(RESUME_ID)
    try:
        response = client.post(f"/api/resumes/{RESUME_ID}/analyze")
        assert response.status_code == 409
    finally:
        eval_service._release(RESUME_ID)


def test_evaluation_config_endpoint():
    response = client.get("/api/evaluation/config")
    assert response.status_code == 200
    body = response.json()
    assert body["config_version"] == "eval-1.0.0"
    assert body["yaml_version"].startswith("completeness@")
    assert len(body["roles"]) == 34
    assert len(body["standard_sections"]) == 15
    assert "api_key" not in json.dumps(body).lower()


def test_frontend_evaluation_wiring():
    root = Path(__file__).resolve().parents[1]
    app_js = (root / "frontend" / "js" / "app.js").read_text("utf-8")
    api_js = (root / "frontend" / "js" / "api.js").read_text("utf-8")
    analytics = (root / "frontend" / "js" / "app.js").read_text("utf-8")
    assert "analyzeResume" in api_js
    assert "evaluationSection" in app_js
    assert "evaluationBodyHtml" in app_js
    assert "AI Evaluation" in analytics


# --------------------------------------------------------------------- #
# Analytics evaluation block
# --------------------------------------------------------------------- #

def _analysis_entry(*, status="success", version=1, completeness=80,
                    quality=70, roles=("Python Developer",),
                    missing=("Projects",), tokens=(100, 50, 150),
                    duration=1000, error=None):
    entry = {
        "analysis_id": f"a-{version}", "resume_id": "r",
        "analysis_version": version, "status": status,
        "created_at": "2026-11-11T11:00:00+00:00",
        "model": "muse-spark-1.3-contributor",
        "yaml_version": "completeness@abc123+eval-eval-1.0.0",
        "usage_type": "evaluation",
        "tokens": {"input": tokens[0], "output": tokens[1],
                   "total": tokens[2]} if tokens else
                  {"input": None, "output": None, "total": None},
        "reasoning_tokens": None, "duration_ms": duration,
        "evaluation": None, "error": error,
    }
    if status == "success":
        entry["evaluation"] = {
            "completeness": {"score": completeness},
            "quality": {"score": quality},
            "suitable_roles": list(roles),
            "missing_sections": [{"section": s} for s in missing],
        }
    return entry


def test_analytics_evaluation_block_math():
    rec1 = _record("analytics-rec-0001")
    rec1["analyses"] = [
        _analysis_entry(version=1, completeness=80, quality=70,
                        roles=("Python Developer", "AI Engineer"),
                        missing=("Projects",), duration=1000),
        _analysis_entry(status="failed", version=2, tokens=(10, 5, 15),
                        duration=None,
                        error="Evaluation response failed validation: x"),
    ]
    rec1["token_usage"] = {"usage_available": False}
    rec2 = _record("analytics-rec-0002")
    rec2["analyses"] = [
        _analysis_entry(version=1, completeness=60, quality=50,
                        roles=("Python Developer",),
                        missing=("Projects", "Certifications"),
                        tokens=None, duration=2000),
    ]
    result = compute_analytics([rec1, rec2])
    ev = result["evaluation"]
    assert ev["total_evaluations"] == 3
    assert ev["successful_evaluations"] == 2
    assert ev["failed_evaluations"] == 1
    assert ev["invalid_json_failures"] == 1
    assert ev["avg_completeness_score"] == 70.0
    assert ev["avg_quality_score"] == 60.0
    assert ev["most_recommended_roles"][0] == {
        "role": "Python Developer", "count": 2}
    assert ev["most_missing_sections"][0] == {
        "section": "Projects", "count": 2}
    assert ev["tokens"] == {"input": 110, "output": 55, "total": 165,
                            "evaluations_with_usage": 2}
    assert ev["avg_duration_ms"] == 1500
    assert ev["yaml_versions_used"] == [
        "completeness@abc123+eval-eval-1.0.0"]
    # Extraction token totals are untouched by evaluation usage.
    assert result["tokens"]["total"] == 0


# --------------------------------------------------------------------- #
# Reliability round: JSON parsing/recovery, content evidence, roles
# --------------------------------------------------------------------- #

def _valid_json_text(**overrides):
    return json.dumps(_eval_payload(**overrides))


def test_fenced_json_response_accepted():
    parsed, error = validate_evaluation(
        "```json\n" + _valid_json_text() + "\n```")
    assert error is None and parsed is not None


def test_prose_wrapped_json_response_accepted():
    parsed, error = validate_evaluation(
        "Here is the evaluation you requested:\n" + _valid_json_text()
        + "\nLet me know if you need anything else.")
    assert error is None and parsed is not None


def test_truncated_json_classified_token_limit():
    text = _valid_json_text()
    parsed, error = validate_evaluation(text[: len(text) // 2])
    assert parsed is None
    assert error.startswith("token_limit:")


def test_empty_response_classified():
    parsed, error = validate_evaluation("   ")
    assert parsed is None
    assert error.startswith("empty_response:")


def test_literal_control_characters_in_strings_tolerated():
    text = _valid_json_text()
    broken = text.replace("MCA student", "MCA\nstudent", 1)
    assert broken != text  # raw newline INSIDE a JSON string
    parsed, error = validate_evaluation(broken)
    assert error is None and parsed is not None


def test_null_and_mistyped_leaves_are_repaired_not_rejected():
    payload = _eval_payload()
    payload["matching_sections"][0]["evidence"] = None
    payload["candidate"]["headline"] = None
    payload["completeness"]["score"] = "78"  # numeric string
    payload["missing_sections"][0]["evidence"] = "none found"
    parsed, error = validate_evaluation(json.dumps(payload))
    assert error is None
    assert parsed["matching_sections"][0]["evidence"] == []
    assert parsed["candidate"]["headline"] == ""
    assert parsed["completeness"]["score"] == 78.0
    assert parsed["missing_sections"][0]["evidence"] == ["none found"]


def test_schema_invalid_is_classified():
    parsed, error = validate_evaluation('{"completeness": "high"}')
    assert parsed is None
    assert error.startswith("schema_invalid:")


def test_correction_call_recovers_from_invalid_json(store_dir):
    record = _record()
    store.save_record(record)
    good = _valid_json_text()
    client = FakeEvalClient(text="not json at all {")
    calls = {"n": 0}

    def chat_json(instructions, user_text, extra=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"text": "not json at all {", "usage": client.usage,
                    "model": "muse-spark-1.3-contributor",
                    "request_id": "r1", "duration_ms": 10.0,
                    "finish_reason": "stop"}
        # The correction request must carry the broken response.
        assert "not json at all" in user_text
        return {"text": good, "usage": client.usage,
                "model": "muse-spark-1.3-contributor",
                "request_id": "r2", "duration_ms": 10.0,
                "finish_reason": "stop"}

    client.chat_json = chat_json
    entry, error = eval_service.analyze_record(
        record, client=client, config=_configured())
    assert error is None
    assert entry["status"] == "success"
    assert entry["recovery"]["attempted"] is True
    assert entry["recovery"]["success"] is True
    # Tokens are summed across BOTH calls — never hidden.
    assert entry["tokens"]["total"] == 4000
    assert entry["diagnostics"]["recovered_via_correction"] is True


def test_failed_correction_preserves_previous_success(store_dir):
    record = _record()
    store.save_record(record)
    entry, error = eval_service.analyze_record(
        record, client=FakeEvalClient(), config=_configured())
    assert error is None and entry["status"] == "success"
    bad = FakeEvalClient(text="{broken")
    entry2, error2 = eval_service.analyze_record(
        record, client=bad, config=_configured())
    assert error2 is not None
    assert entry2["status"] == "failed"
    assert entry2["recovery"]["attempted"] is True
    assert entry2["recovery"]["success"] is False
    assert entry2["diagnostics"]["failure_category"] in (
        "invalid_json", "token_limit")
    saved = store.get_record(RESUME_ID)
    assert saved["analysis"]["status"] == "success"  # untouched
    assert len(saved["analyses"]) == 2


CONTENT_TEXT = """A. Writer
Content Strategist and Editorial Journalist with six years of experience.
Professional Summary
Content strategist who grew organic reach to 2M+ readers and improved SEO performance by 50-75% across flagship properties.
Work Experience
Content Strategist, MediaHouse, 2020-Present
- Led content strategy and campaign management for three brands.
- Managed the editorial calendar and CMS publishing workflows.
- Produced video series and coordinated with design teams.
- Executed paid campaigns that increased Instagram audience growth by 20-40%.
- Achieved 90% audience penetration in the target segment.
Education
BA Journalism, City University, 2014-2017
"""


def _content_payload():
    payload = _eval_payload()
    payload["missing_sections"] = [
        {"section": "Responsibilities", "importance": "High",
         "status": "missing", "reason": "No heading found.",
         "impact": "Duties unclear.", "recommendation": "Add one.",
         "evidence": []},
        {"section": "Achievements", "importance": "Medium",
         "status": "missing", "reason": "No heading found.",
         "impact": "Outcomes unclear.", "recommendation": "Add one.",
         "evidence": []}]
    payload["role_suitability"] = [
        {"role": "Content Strategist", "score": 88,
         "matching_skills": ["Content strategy", "Editorial planning"],
         "missing_skills": ["Ahrefs"], "relevant_projects": [],
         "relevant_experience": ["Content Strategist, MediaHouse"],
         "evidence": ["Led content strategy"], "explanation": "Direct fit."},
        {"role": "Journalist", "score": 74,
         "matching_skills": ["Reporting", "News writing"],
         "missing_skills": [], "relevant_projects": [],
         "relevant_experience": ["Editorial Journalist"],
         "evidence": ["Six years editorial"], "explanation": "Strong fit."},
        {"role": "UI/UX Designer", "score": 30,
         "matching_skills": [], "missing_skills": ["Figma"],
         "relevant_projects": [], "relevant_experience": [],
         "evidence": [], "explanation": "No design evidence."}]
    return payload


def test_responsibilities_and_achievements_detected_from_content(store_dir):
    record = _record(text=CONTENT_TEXT)
    store.save_record(record)
    client = FakeEvalClient(payload=_content_payload())
    entry, error = eval_service.analyze_record(
        record, client=client, config=_configured())
    assert error is None
    ev = entry["evaluation"]
    missing_names = [m["section"] for m in ev["missing_sections"]]
    matching_names = [m["section"] for m in ev["matching_sections"]]
    assert "Responsibilities" not in missing_names
    assert "Achievements" not in missing_names
    assert "Responsibilities" in matching_names
    assert "Achievements" in matching_names
    resp = next(m for m in ev["matching_sections"]
                if m["section"] == "Responsibilities")
    assert resp["evidence"]  # literal lines from the text
    assert entry["content_evidence"]["responsibilities"]["action"] == \
        "moved_to_matching"
    assert entry["content_evidence"]["achievements"]["action"] == \
        "moved_to_matching"


def test_content_roles_flow_into_suitable_roles(store_dir):
    record = _record(text=CONTENT_TEXT)
    store.save_record(record)
    client = FakeEvalClient(payload=_content_payload())
    entry, error = eval_service.analyze_record(
        record, client=client, config=_configured())
    assert error is None
    # Suitable roles recomputed from the details: the content roles
    # qualify, the evidence-less technical role does not.
    assert entry["evaluation"]["suitable_roles"] == [
        "Content Strategist", "Journalist"]


def test_content_taxonomy_includes_editorial_roles():
    names = [r["name"] for r in eval_config.load_config()["role_taxonomy"]]
    for role in ("Content Strategist", "Content Writer",
                 "Editorial Manager", "Journalist",
                 "Social Media Manager", "Digital Marketing Specialist",
                 "Video Content Creator", "Content Marketing Manager",
                 "SEO Content Specialist", "Brand Content Manager"):
        assert role in names
    # Technical roles retained.
    assert "Python Developer" in names and "AI Engineer" in names


def test_no_content_evidence_keeps_missing_verdict():
    from evaluation.content_evidence import reconcile_content_sections

    evaluation = {"matching_sections": [],
                  "missing_sections": [
                      {"section": "Responsibilities"},
                      {"section": "Achievements"}]}
    result = reconcile_content_sections(
        evaluation, "Name\nEmail\nEducation\nBSc, Some University.")
    assert result["responsibilities"]["action"] == "none"
    assert result["achievements"]["action"] == "none"
    assert len(evaluation["missing_sections"]) == 2
