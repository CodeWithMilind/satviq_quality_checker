"""YAML Editor AI — independent tests (Muse API always mocked).

Covers the spec's 14 scenarios: low-quality triggers / high-quality
does not / duplicate analysis skipped / useful keywords identified /
no duplication of existing keywords / invalid JSON changes nothing /
unknown categories rejected / updates create versions / no-change
creates none / previous versions immutable / failed updates preserve
the active YAML / token usage recorded from the API reply / concurrent
updates do not corrupt the YAML / analytics accuracy. Plus the API
endpoints and the pipeline hook's trigger behaviour.
"""
from __future__ import annotations

import json
import threading

import pytest
import yaml

from yaml_editor_ai import version_manager
from yaml_editor_ai.agent import EditorConfig, YamlEditorAgent, \
    maybe_improve_from_record
from yaml_editor_ai.token_tracker import TokenTracker
from yaml_editor_ai.yaml_manager import YamlStore

# A low-completeness style resume: real sections under headings the
# current YAML does NOT know ("competency profile", "career
# highlights"), padded past the minimum usable-text length.
LOW_TEXT = """Asha Verma
asha.verma@example.com | +91 90000 11111 | Mumbai, India

Competency Profile
Python developer with strong backend and data skills across projects.
Built FastAPI services, data pipelines and dashboards for real users.

Career Highlights
Data Analyst at Example Corp from June 2024 to March 2025.
Cleaned data, built reports, automated weekly analysis workflows.

Technical Skills
Python, SQL, FastAPI, Pandas, NumPy, Git, Docker, Linux, REST APIs.

Learning Journey
Master of Computer Applications, Example University, 2025 to 2027.
Bachelor of Computer Applications completed in 2025 with distinction.
"""

USAGE = {"prompt_tokens": 812, "completion_tokens": 41, "total_tokens": 853}


def make_agent(tmp_path, reply_text=None, client=None, env=None):
    # NOTE: `env if env is not None` — an explicit {} means "no keys
    # anywhere" (the unconfigured-agent test depends on it).
    cfg = EditorConfig(
        env=env if env is not None else {"YAML_EDITOR_API_KEY": "test-key"},
        file_config={"_store_root": str(tmp_path)})
    if client is None:
        def client(instruction, user_text):
            return {"text": reply_text or "{}", "usage": dict(USAGE),
                    "model": "muse-spark-1.3-contributor",
                    "duration_ms": 12.0}
    agent = YamlEditorAgent(cfg, client=client)
    agent.store.ensure_seeded()
    return agent


def analyze(agent, **kw):
    args = {"resume_id": "r-1", "filename": "asha.pdf", "text": LOW_TEXT,
            "completeness_score": 55.0,
            "missing_sections": ["Skills", "Projects"]}
    args.update(kw)
    return agent.analyze(**args)


# 1 — low-quality resume triggers the agent ------------------------------
def test_low_quality_triggers_and_updates(tmp_path):
    agent = make_agent(tmp_path, reply_text=json.dumps(
        {"skills": ["competency profile"]}))
    result = analyze(agent)
    assert result["status"] == "success"
    assert result["keywords_added"] == {"skills": ["competency profile"]}


# 2 — high-quality resume does NOT trigger -------------------------------
def test_high_quality_does_not_trigger(tmp_path):
    calls = []

    def client(instruction, user_text):
        calls.append(1)
        return {"text": "{}", "usage": dict(USAGE), "model": "m",
                "duration_ms": 1.0}

    agent = make_agent(tmp_path, client=client)
    result = analyze(agent, completeness_score=97.0)
    assert result["status"] == "skipped"
    assert result["reason"] == "completeness_not_low"
    assert calls == []
    assert agent.tracker.all() == []


# 3 — duplicate analysis of the same resume + YAML version is skipped ----
def test_duplicate_analysis_skipped(tmp_path):
    agent = make_agent(tmp_path, reply_text=json.dumps(
        {"skills": ["competency profile"]}))
    assert analyze(agent)["status"] == "success"
    second = analyze(agent)
    assert second["status"] == "skipped"
    assert second["reason"] == "already_analyzed"
    assert len(agent.tracker.all()) == 1


# 4+5 — useful keywords accepted, existing ones never duplicated ---------
def test_useful_keywords_and_no_duplicates(tmp_path):
    reply = {"skills": ["technical skills", "competency profile"],
             "work_experience": ["career highlights"]}
    agent = make_agent(tmp_path, reply_text=json.dumps(reply))
    result = analyze(agent)
    assert result["status"] == "success"
    # "technical skills" already exists in the YAML -> rejected.
    assert result["keywords_added"] == {
        "skills": ["competency profile"],
        "work_experience": ["career highlights"]}
    rejected = {(r["keyword"], r["reason"]) for r in result["rejected"]}
    assert ("technical skills", "duplicate") in rejected
    config = agent.store.read_active()
    assert config["sections"]["skills"]["aliases"].count(
        "technical skills") == 1
    assert "competency profile" in config["sections"]["skills"]["aliases"]


# — invented keywords (not in the resume text) are rejected --------------
def test_invented_keyword_rejected(tmp_path):
    agent = make_agent(tmp_path, reply_text=json.dumps(
        {"skills": ["quantum blockchain wizardry"]}))
    result = analyze(agent)
    assert result["status"] == "no_change"
    assert result["rejected"][0]["reason"] == "not_in_resume_text"


# 6 — invalid JSON does not modify the YAML -------------------------------
def test_invalid_json_changes_nothing(tmp_path):
    agent = make_agent(tmp_path, reply_text="this is not json")
    before = agent.store.read_active_text()
    result = analyze(agent)
    assert result["status"] == "failed"
    assert "invalid_json" in result["error"]
    assert agent.store.read_active_text() == before
    assert agent.store.version_numbers() == [1]


# 7 — unknown categories are rejected, no new category is created --------
def test_unknown_category_rejected(tmp_path):
    agent = make_agent(tmp_path, reply_text=json.dumps(
        {"hobbies_and_fun": ["competency profile"]}))
    result = analyze(agent)
    assert result["status"] == "no_change"
    assert result["rejected"][0]["reason"] == "unknown_category"
    assert "hobbies_and_fun" not in agent.store.read_active()["sections"]


# 8+10 — a successful update creates a version; previous ones unchanged ---
def test_update_creates_version_and_preserves_previous(tmp_path):
    agent = make_agent(tmp_path, reply_text=json.dumps(
        {"skills": ["competency profile"]}))
    v1_text = (agent.store.versions_dir / "completeness_v001.yaml") \
        .read_text(encoding="utf-8")
    result = analyze(agent)
    assert result["yaml_version_after"] == "v002"
    assert agent.store.version_numbers() == [1, 2]
    assert (agent.store.versions_dir / "completeness_v001.yaml") \
        .read_text(encoding="utf-8") == v1_text
    v2_text = (agent.store.versions_dir / "completeness_v002.yaml") \
        .read_text(encoding="utf-8")
    assert agent.store.read_active_text() == v2_text
    assert "competency profile" in v2_text
    assert "yaml-editor-ai" in v2_text  # provenance comment
    changelog = version_manager.read_changelog(agent.store)
    entry = changelog[-1]
    assert entry["new_version"] == "v002"
    assert entry["previous_version"] == "v001"
    assert entry["resume_id"] == "r-1"
    assert entry["keywords_added"] == {"skills": ["competency profile"]}
    assert entry["input_tokens"] == 812
    assert entry["status"] == "success"


# 9 — a no-change response creates NO new version --------------------------
def test_no_change_creates_no_version(tmp_path):
    agent = make_agent(tmp_path, reply_text="{}")
    result = analyze(agent)
    assert result["status"] == "no_change"
    assert agent.store.version_numbers() == [1]


# 11 — a failed update preserves the active YAML ---------------------------
def test_failed_update_preserves_active(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, reply_text=json.dumps(
        {"skills": ["competency profile"]}))
    before = agent.store.read_active_text()

    def boom(*a, **kw):
        raise OSError("disk exploded")

    monkeypatch.setattr(version_manager, "_atomic_write", boom)
    result = analyze(agent)
    assert result["status"] == "failed"
    assert agent.store.read_active_text() == before


# — an API failure is recorded and changes nothing -------------------------
def test_api_failure_recorded(tmp_path):
    def failing_client(instruction, user_text):
        raise TimeoutError("timed out")

    agent = make_agent(tmp_path, client=failing_client)
    before = agent.store.read_active_text()
    result = analyze(agent)
    assert result["status"] == "failed"
    assert "api_error" in result["error"]
    assert agent.store.read_active_text() == before
    assert agent.tracker.summary()["api_failures"] == 1


# 12 — token usage is recorded from the API reply, never estimated --------
def test_token_usage_recorded(tmp_path):
    agent = make_agent(tmp_path, reply_text="{}")
    analyze(agent)
    (entry,) = agent.tracker.all()
    assert entry["input_tokens"] == 812
    assert entry["output_tokens"] == 41
    summary = agent.tracker.summary()
    assert summary["total_tokens"] == 853
    assert summary["total_executions"] == 1


# 13 — concurrent updates never corrupt the YAML ---------------------------
def test_concurrent_updates_do_not_corrupt(tmp_path):
    replies = {
        "r-a": {"skills": ["competency profile"]},
        "r-b": {"projects": ["learning journey"]},
        "r-c": {"skills": ["competency profile"]},
    }

    def client_factory(rid):
        def client(instruction, user_text):
            return {"text": json.dumps(replies[rid]),
                    "usage": dict(USAGE), "model": "m", "duration_ms": 1.0}
        return client

    agents = [make_agent(tmp_path, client=client_factory(rid))
              for rid in replies]
    threads = [threading.Thread(
        target=lambda a=agent, r=rid: a.analyze(
            resume_id=r, filename=f"{r}.pdf", text=LOW_TEXT,
            completeness_score=50.0, missing_sections=["Skills"]))
        for agent, rid in zip(agents, replies)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    store = agents[0].store
    config = store.read_active()  # must still parse
    skills = config["sections"]["skills"]["aliases"]
    assert skills.count("competency profile") == 1  # applied once only
    assert "learning journey" in \
        config["sections"]["projects"]["aliases"]
    versions = store.version_numbers()
    assert versions == sorted(set(versions))
    assert store.read_active_text() == (
        store.versions_dir / f"completeness_v{versions[-1]:03d}.yaml") \
        .read_text(encoding="utf-8")


# 14 — analytics returns accurate records ----------------------------------
def test_analytics_accuracy(tmp_path):
    agent = make_agent(tmp_path, reply_text=json.dumps(
        {"skills": ["competency profile", "technical skills"]}))
    analyze(agent)
    data = agent.analytics()
    s = data["summary"]
    assert s["total_executions"] == 1
    assert s["successful_executions"] == 1
    assert s["total_input_tokens"] == 812
    assert s["total_output_tokens"] == 41
    assert s["total_keywords_accepted"] == 1
    assert s["total_keywords_rejected"] == 1
    assert data["total_yaml_versions"] == 2
    assert data["history"][0]["filename"] == "asha.pdf"
    assert data["history"][0]["keywords_added"] == 1
    assert data["recent_updates"][0]["new_version"] == "v002"


# — seeding: the active YAML starts byte-identical to the bundled one -----
def test_seeding_is_byte_identical(tmp_path):
    agent = make_agent(tmp_path)
    from yaml_editor_ai.yaml_manager import BUNDLED_COMPLETENESS
    assert agent.store.read_active_text() == \
        BUNDLED_COMPLETENESS.read_text(encoding="utf-8")


# — unconfigured agent (no key) never calls the model ----------------------
def test_unconfigured_agent_skips(tmp_path):
    agent = make_agent(tmp_path, env={})
    result = analyze(agent)
    assert result["status"] == "skipped"
    assert result["reason"] == "not_configured"


# — pipeline hook: record shape in, trigger decision out -------------------
def test_hook_uses_record_fields(tmp_path):
    agent = make_agent(tmp_path, reply_text=json.dumps(
        {"skills": ["competency profile"]}))
    record = {
        "resume_id": "hook-1", "filename": "asha.pdf",
        "final_extracted_text": LOW_TEXT,
        "attempts": [{"extractor_name": "tika", "is_final_selected": True,
                      "quality": {"completeness": {
                          "score": 55.0,
                          "missing_sections": ["Skills"]}}}],
    }
    outcome = maybe_improve_from_record(record, agent=agent)
    assert outcome["status"] == "success"
    # The record itself is never modified by the hook.
    assert record["attempts"][0]["quality"]["completeness"]["score"] == 55.0


def test_hook_high_quality_record_skips(tmp_path):
    agent = make_agent(tmp_path)
    record = {
        "resume_id": "hook-2", "filename": "good.pdf",
        "final_extracted_text": LOW_TEXT,
        "quality": {"completeness": {"score": 100.0,
                                     "missing_sections": []}},
    }
    outcome = maybe_improve_from_record(record, agent=agent)
    assert outcome["status"] == "skipped"


# — API endpoints ------------------------------------------------------------
def test_api_endpoints(tmp_path, monkeypatch):
    import api.main as main_module
    import yaml_editor_ai.agent as agent_module
    from fastapi.testclient import TestClient

    agent = make_agent(tmp_path, reply_text=json.dumps(
        {"skills": ["competency profile"]}))
    analyze(agent)
    monkeypatch.setattr(agent_module, "_default_agent", agent)
    client = TestClient(main_module.app)
    analytics = client.get("/api/yaml-editor/analytics")
    assert analytics.status_code == 200
    assert analytics.json()["summary"]["total_executions"] == 1
    versions = client.get("/api/yaml-editor/versions")
    assert versions.status_code == 200
    assert versions.json()["current_version"] == "v002"
    content = client.get("/api/yaml-editor/versions/v001")
    assert content.status_code == 200
    assert "sections:" in content.json()["content"]
    assert client.get("/api/yaml-editor/versions/v099").status_code == 404
    changelog = client.get("/api/yaml-editor/changelog")
    assert changelog.status_code == 200
    assert changelog.json()["changelog"][-1]["new_version"] == "v002"
    history = client.get("/api/yaml-editor/history")
    assert history.status_code == 200
    assert history.json()["history"][0]["status"] == "success"
    # No credentials or resume text leak through the endpoints.
    blob = json.dumps(analytics.json()) + json.dumps(history.json())
    assert "test-key" not in blob
    assert "Competency Profile" not in blob
