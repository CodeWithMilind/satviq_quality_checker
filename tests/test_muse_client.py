"""Muse AI client + service — protocol shapes with a faked HTTP seam.

Request shapes follow the official Meta Model API documentation:
Chat Completions at {base}/chat/completions, Bearer auth, images as
image_url data-URL parts, PDFs as file parts. No live calls.
"""

import base64
import json
from pathlib import Path

import pytest

import muse_ai.client as client_module
from muse_ai import (MuseAuthError, MuseConfig, MuseConfigurationError,
                     MuseService, MuseTimeoutError)

VALID = {"full_name": "Priya Nair", "email": "priya@example.com",
         "skills": ["Python"]}


def make_config(**overrides):
    values = {"api_key": "test-key-9876", "timeout_seconds": 7.0}
    values.update(overrides)
    return MuseConfig(**values)


@pytest.fixture
def http(monkeypatch):
    calls = []

    def fake(url, payload, headers, timeout):
        calls.append({"url": url, "payload": payload,
                      "headers": headers, "timeout": timeout})
        return {"model": "muse-spark-1.3",
                "choices": [{"message": {
                    "content": json.dumps(VALID)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                          "total_tokens": 15}}

    monkeypatch.setattr(client_module, "http_post_json", fake)
    return calls


def test_text_request_shape_and_auth(http):
    service = MuseService(make_config())
    outcome = service.generate_resume_json("Priya Nair, Python dev")
    assert outcome["ok"] is True
    assert outcome["resume_json"]["full_name"] == "Priya Nair"
    assert outcome["validation"] == "valid"
    assert outcome["provider"] == "Muse AI"
    assert outcome["input_type"] == "text"
    assert outcome["usage"]["total_tokens"] == 15
    call = http[0]
    assert call["url"] == "https://api.meta.ai/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer test-key-9876"
    assert call["timeout"] == 7.0
    payload = call["payload"]
    assert payload["model"] == "muse-spark-1.3"
    assert [m["role"] for m in payload["messages"]] == ["developer", "user"]
    # Optional parameters are sent ONLY when configured.
    assert "max_completion_tokens" not in payload
    assert "temperature" not in payload


def test_optional_parameters_sent_when_configured(http):
    service = MuseService(make_config(max_output_tokens=999,
                                      temperature=0.2))
    service.generate_resume_json("text")
    payload = http[0]["payload"]
    assert payload["max_completion_tokens"] == 999
    assert payload["temperature"] == 0.2


def test_image_input_uses_image_url_data_url_parts(http, tmp_path):
    png = tmp_path / "resume.png"
    png.write_bytes(b"\x89PNG-fake-bytes")
    service = MuseService(make_config())
    outcome = service.vision_extract(png)
    assert outcome["ok"] is True
    assert outcome["input_type"] == "image"
    content = http[0]["payload"]["messages"][1]["content"]
    image_parts = [p for p in content if p["type"] == "image_url"]
    assert len(image_parts) == 1
    expected = "data:image/png;base64," + base64.b64encode(
        b"\x89PNG-fake-bytes").decode("ascii")
    assert image_parts[0]["image_url"]["url"] == expected


def test_pdf_input_sends_original_file_part(http):
    sample = Path(__file__).resolve().parent.parent / "sample_resume.pdf"
    service = MuseService(make_config())
    outcome = service.vision_extract(sample)
    assert outcome["ok"] is True
    assert outcome["input_type"] == "pdf"
    assert outcome["page_count"] == 2
    assert outcome["pages_skipped"] == 0
    content = http[0]["payload"]["messages"][1]["content"]
    file_parts = [p for p in content if p["type"] == "file"]
    assert len(file_parts) == 1
    assert file_parts[0]["file"]["filename"] == "sample_resume.pdf"
    assert file_parts[0]["file"]["file_data"].startswith(
        "data:application/pdf;base64,")


def test_invalid_json_is_recorded_not_fabricated(monkeypatch):
    monkeypatch.setattr(client_module, "http_post_json",
                        lambda *a, **k: {"choices": [{"message": {
                            "content": "definitely not json"}}]})
    outcome = MuseService(make_config()).generate_resume_json("text")
    assert outcome["ok"] is False
    assert outcome["validation"] == "invalid"
    assert outcome["resume_json"] is None
    assert outcome["error_type"] == "ValidationError"


def test_auth_failure_maps_and_scrubs_the_key(monkeypatch):
    def unauthorized(url, payload, headers, timeout):
        raise client_module.MuseError(
            "Muse API returned HTTP 401: bad key test-key-9876",
            status=401)
    monkeypatch.setattr(client_module, "http_post_json", unauthorized)
    service = MuseService(make_config())
    with pytest.raises(MuseAuthError):
        service.client.chat_text("i", "u")
    outcome = service.generate_resume_json("text")
    assert outcome["ok"] is False
    assert outcome["error_type"] == "MuseAuthError"
    assert "test-key-9876" not in (outcome["error"] or "")


def test_timeout_maps_to_timeout_error(monkeypatch):
    def slow(url, payload, headers, timeout):
        raise MuseTimeoutError("Muse API request timed out after 7s.")
    monkeypatch.setattr(client_module, "http_post_json", slow)
    outcome = MuseService(make_config()).generate_resume_json("text")
    assert outcome["ok"] is False
    assert outcome["error_type"] == "TimeoutError"
    assert outcome["timeout_seconds"] == 7.0


def test_missing_key_blocks_calls_with_configuration_error(http):
    service = MuseService(MuseConfig(api_key=None, errors=[
        "MUSE_API_KEY is not set — add it to the backend .env file "
        "(see .env.example)."]))
    outcome = service.generate_resume_json("text")
    assert outcome["ok"] is False
    assert outcome["error_type"] == "ConfigurationError"
    assert "MUSE_API_KEY" in outcome["error"]
    assert http == []  # no request was attempted
    with pytest.raises(MuseConfigurationError):
        service.client.chat_text("i", "u")


def test_vision_disabled_is_a_clear_configuration_error(http, tmp_path):
    png = tmp_path / "resume.png"
    png.write_bytes(b"\x89PNG")
    service = MuseService(make_config(vision_enabled=False))
    outcome = service.vision_extract(png)
    assert outcome["ok"] is False
    assert outcome["error_type"] == "ConfigurationError"
    assert "MUSE_VISION_ENABLED" in outcome["error"]
    assert http == []


def test_unsupported_input_type_is_a_clear_error(http, tmp_path):
    docx = tmp_path / "resume.docx"
    docx.write_bytes(b"PK-fake")
    outcome = MuseService(make_config()).vision_extract(docx)
    assert outcome["ok"] is False
    assert outcome["input_type"] == "unsupported"
    assert "PDF and image" in outcome["error"]
    assert http == []
