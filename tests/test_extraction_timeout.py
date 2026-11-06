"""Per-attempt extraction timeout — backend enforcement.

A slow extractor under a small cap must be recorded as a TIMEOUT
attempt (error_type "TimeoutError"), the chain must continue to the
next extractor, and the record must carry the configured timeout.
The frontend only selects the value; enforcement lives here.
"""

import time
from pathlib import Path

from fastapi.testclient import TestClient

from api.main import app
from api.pipeline import QualityGatePolicy, build_record_from_result
from extractor import ExtractionManager
from extractor.base import BaseExtractor
from quality_checker import QualityChecker

from tests.test_attempt_outputs import PASS_TEXT

client = TestClient(app)


class SlowExtractor(BaseExtractor):
    name = "slow"

    def _extract(self, path):
        time.sleep(2.0)
        return self._success(path, PASS_TEXT)


class FastExtractor(BaseExtractor):
    name = "fast"

    def _extract(self, path):
        return self._success(path, PASS_TEXT)


def test_slow_attempt_times_out_and_chain_continues(tmp_path):
    sample = tmp_path / "resume.pdf"
    sample.write_bytes(b"%PDF-fake")
    policy = QualityGatePolicy(QualityChecker(), document_id="timeout-1")
    manager = ExtractionManager(
        config={"manager": {"order": ["slow", "fast"], "max_attempts": 5}},
        extractors={"slow": SlowExtractor({}), "fast": FastExtractor({})},
        acceptance_policy=policy, attempt_timeout_seconds=0.3)
    result = manager.extract(sample)
    assert result.success is True
    assert result.extractor_name == "fast"
    slow_attempt = result.attempts[0]
    assert slow_attempt.extractor_name == "slow"
    assert slow_attempt.success is False
    assert slow_attempt.error_type == "TimeoutError"
    assert "TIMEOUT" in (slow_attempt.error or "")


def test_record_carries_configured_timeout_per_attempt(tmp_path):
    sample = tmp_path / "resume.pdf"
    sample.write_bytes(b"%PDF-fake")
    policy = QualityGatePolicy(QualityChecker(), document_id="timeout-2")
    manager = ExtractionManager(
        config={"manager": {"order": ["fast"], "max_attempts": 5}},
        extractors={"fast": FastExtractor({})},
        acceptance_policy=policy, attempt_timeout_seconds=20)
    result = manager.extract(sample)
    record = build_record_from_result(
        resume_id="timeout-2", original_filename="resume.pdf", path=sample,
        mime_type="application/pdf", file_size=sample.stat().st_size,
        result=result, policy=policy, order=["fast"],
        extraction_timeout_seconds=20)
    assert record["extraction_timeout_seconds"] == 20
    assert record["attempts"][0]["timeout_seconds"] == 20


def test_no_cap_means_no_timeout_behavior(tmp_path):
    sample = tmp_path / "resume.pdf"
    sample.write_bytes(b"%PDF-fake")
    manager = ExtractionManager(
        config={"manager": {"order": ["fast"], "max_attempts": 5}},
        extractors={"fast": FastExtractor({})})
    assert manager.attempt_timeout_seconds is None
    assert manager.extract(sample).success is True


def test_process_endpoint_accepts_and_validates_timeout():
    project_sample = Path(__file__).resolve().parent.parent / "sample_resume.pdf"
    with project_sample.open("rb") as handle:
        ok = client.post(
            "/api/resumes/process",
            files={"files": ("sample_resume.pdf", handle, "application/pdf")},
            data={"extraction_timeout": "15"})
    assert ok.status_code == 200
    record = ok.json()["results"][0]
    assert record["extraction_timeout_seconds"] == 15
    with project_sample.open("rb") as handle:
        bad = client.post(
            "/api/resumes/process",
            files={"files": ("sample_resume.pdf", handle, "application/pdf")},
            data={"extraction_timeout": "0"})
    assert bad.status_code == 400


def test_frontend_offers_15_and_20_and_sends_the_value():
    """Production UI (v14): the timeout select is rendered by app.js
    (the HTML is a shell), and the central API layer sends the chosen
    value as the extraction_timeout form field."""
    root = Path(__file__).resolve().parent.parent
    js = (root / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    api_js = (root / "frontend" / "js" / "api.js").read_text(encoding="utf-8")
    assert 'id="timeoutSelect"' in js
    assert '<option value="15">15 seconds</option>' in js
    assert '<option value="20" selected>20 seconds</option>' in js
    assert 'form.append("extraction_timeout"' in api_js


def test_frontend_has_no_model_configuration_only_muse_status():
    """Provider/model/API key stay backend-.env-only. The production
    UI's Settings page (new spec) edits ONLY the system prompt: it
    shows provider/model read-only from the backend and contains no
    provider, model or key form anywhere."""
    root = Path(__file__).resolve().parent.parent
    html = (root / "frontend" / "index.html").read_text(encoding="utf-8")
    js = (root / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    api_js = (root / "frontend" / "js" / "api.js").read_text(encoding="utf-8")
    combined = html + js + api_js
    assert "/api/ai/configs" not in combined
    assert "aiCfg" not in combined
    assert "MUSE_API_KEY" not in combined
    assert "Bearer" not in combined
    # Settings edits the prompt only, via the dedicated endpoint.
    assert 'id="promptEditor"' in js
    assert "/api/settings/system-prompt" in api_js
    assert "ai.provider" in js and "ai.model" in js  # read-only display
