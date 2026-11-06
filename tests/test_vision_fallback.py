"""Muse AI pipeline stages — direct extraction fallback + JSON gen.

A fake Muse service proves the pipeline wiring without provider
calls: the direct-extraction stage runs only when the extractor
chain does not PASS, receives the ORIGINAL file, and its attempt is
preserved with model provenance; when Muse is not configured, a
failed Muse attempt carrying the configuration error is recorded
instead of silently skipping; Resume JSON generation runs only
when Muse is configured. Nothing is fabricated anywhere.
"""

from pathlib import Path

from api.pipeline import _apply_muse_stages, process_resume_file

VALID_JSON = {"full_name": "Priya Nair", "email": "priya@example.com",
              "skills": ["Python"]}


class FakeMuseService:
    """Duck-typed MuseService with scripted outcomes."""

    provider_name = "Muse AI"
    model = "muse-spark-1.3"

    def __init__(self, *, configured=True, vision_enabled=True,
                 vision_outcome=None, json_outcome=None):
        self.configured = configured
        self.vision_enabled = vision_enabled
        self.configuration_error = (
            None if configured
            else "MUSE_API_KEY is not set — add it to the backend "
                 ".env file (see .env.example).")
        self.config = type("Cfg", (), {"timeout_seconds": 20.0})()
        self._vision_outcome = vision_outcome
        self._json_outcome = json_outcome
        self.vision_calls = []
        self.json_calls = []

    def vision_extract(self, path, **kwargs):
        self.vision_calls.append(path)
        self.vision_kwargs = kwargs
        return self._vision_outcome

    def generate_resume_json(self, text, **kwargs):
        self.json_calls.append(text)
        return self._json_outcome


def _outcome(ok=True, **extra):
    base = {"ok": ok,
            "error": None if ok else "model returned invalid JSON",
            "error_type": None if ok else "ValidationError",
            "resume_json": VALID_JSON if ok else None,
            "validation": "valid" if ok else "invalid",
            "provider": "Muse AI", "model": "muse-spark-1.3",
            "raw_text": '{"full_name": "Priya Nair"}' if ok else "nope",
            "usage": {"total_tokens": 42}, "duration_ms": 12.5,
            "timeout_seconds": 20.0, "input_type": "image",
            "page_count": 1, "pages_processed": 1, "pages_skipped": 0}
    base.update(extra)
    return base


def _failed_chain_png(tmp_path, name="resume.png"):
    png = tmp_path / name
    png.write_bytes(b"\x89PNG-fake")
    return png


def test_unconfigured_muse_records_a_configuration_attempt(tmp_path):
    png = _failed_chain_png(tmp_path)
    record = process_resume_file(
        png, resume_id="muse-test-1", original_filename="resume.png",
        muse_service=FakeMuseService(configured=False))
    muse_attempts = [a for a in record["attempts"]
                     if a["extractor_name"] == "muse_ai"]
    assert len(muse_attempts) == 1
    attempt = muse_attempts[0]
    assert attempt["success"] is False
    assert attempt["error_type"] == "ConfigurationError"
    assert "MUSE_API_KEY" in attempt["error"]
    assert attempt["provider"] == "Muse AI"
    assert record["resume_json"] is None
    assert record["extractor_used"] is None


def test_muse_vision_rescues_failed_chain(tmp_path):
    png = _failed_chain_png(tmp_path)
    service = FakeMuseService(vision_outcome=_outcome())
    record = process_resume_file(
        png, resume_id="muse-test-2", original_filename="resume.png",
        muse_service=service)
    assert service.vision_calls == [png]  # the ORIGINAL file
    assert record["success"] is True
    assert record["extractor_used"] == "muse_ai"
    assert record["resume_json"] == VALID_JSON
    assert record["resume_json_source"] == "muse_ai"
    attempt = record["attempts"][-1]
    assert attempt["extractor_name"] == "muse_ai"
    assert attempt["provider"] == "Muse AI"
    assert attempt["model"] == "muse-spark-1.3"
    assert attempt["input_type"] == "image"
    assert attempt["json_validation"] == "valid"
    assert attempt["is_final_selected"] is True
    assert all(not a["is_final_selected"]
               for a in record["attempts"][:-1])
    stage_names = [s["stage"] for s in record["pipeline_trace"]]
    assert any(name.startswith("Muse AI (") for name in stage_names)


def test_muse_invalid_json_is_recorded_not_fabricated(tmp_path):
    png = _failed_chain_png(tmp_path)
    service = FakeMuseService(vision_outcome=_outcome(ok=False))
    record = process_resume_file(
        png, resume_id="muse-test-3", original_filename="resume.png",
        muse_service=service)
    assert record["success"] is False
    assert record["resume_json"] is None
    attempt = record["attempts"][-1]
    assert attempt["extractor_name"] == "muse_ai"
    assert attempt["json_validation"] == "invalid"
    assert attempt["extracted_text"] == "nope"


def test_vision_disabled_records_configuration_attempt(tmp_path):
    png = _failed_chain_png(tmp_path)
    service = FakeMuseService(vision_enabled=False)
    record = process_resume_file(
        png, resume_id="muse-test-4", original_filename="resume.png",
        muse_service=service)
    attempt = record["attempts"][-1]
    assert attempt["extractor_name"] == "muse_ai"
    assert attempt["error_type"] == "ConfigurationError"
    assert "MUSE_VISION_ENABLED" in attempt["error"]
    assert service.vision_calls == []


def _passing_record():
    return {"resume_id": "muse-test-5", "attempts": [],
            "attempt_count": 0, "pipeline_trace": [],
            "final_quality_status": "PASS",
            "extracted_text": "Priya Nair — Python developer",
            "resume_json": None, "resume_json_source": None,
            "resume_json_meta": None, "extractor_used": "tika",
            "selection_reason": "accepted"}


def test_resume_json_generation_from_accepted_text():
    record = _passing_record()
    service = FakeMuseService(json_outcome=_outcome(input_type="text"))
    _apply_muse_stages(record, Path("x.pdf"), service, None)
    assert service.json_calls == ["Priya Nair — Python developer"]
    assert service.vision_calls == []  # chain PASSED: no vision stage
    assert record["resume_json"] == VALID_JSON
    assert record["resume_json_source"] == "muse_ai"
    assert record["resume_json_meta"]["provider"] == "Muse AI"
    stage_names = [s["stage"] for s in record["pipeline_trace"]]
    assert any("Resume JSON Generation" in n for n in stage_names)
    assert "JSON Validation" in stage_names


def test_no_json_generation_when_unconfigured():
    record = _passing_record()
    service = FakeMuseService(configured=False)
    _apply_muse_stages(record, Path("x.pdf"), service, None)
    assert service.json_calls == []
    assert record["resume_json"] is None
    assert record["attempts"] == []
