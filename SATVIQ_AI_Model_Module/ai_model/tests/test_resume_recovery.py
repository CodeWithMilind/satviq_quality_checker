"""Unit tests for the resume recovery task and image utilities.
The OpenAI provider is mocked throughout — no real API calls."""

import base64
import json

import pytest

from ai_model.config import AIModelConfig
from ai_model.exceptions import (
    ImageProcessingError,
    InvalidModelOutputError,
    ModelAPIError,
)
from ai_model.model_registry import create_default_registry
from ai_model.providers.openai_provider import OpenAIProvider
from ai_model.schemas import ResumeRecoveryInput, ResumeRecoveryResult
from ai_model.tasks.resume_recovery import ResumeRecoveryTask
from ai_model.utils.image_utils import prepare_image, prepare_images


def _task(client, config=None) -> ResumeRecoveryTask:
    model = create_default_registry().get("gpt-4o-mini")
    provider = OpenAIProvider(
        config or AIModelConfig(openai_api_key="sk-test", max_retries=0),
        client=client,
        retry_backoff_seconds=0,
    )
    return ResumeRecoveryTask(provider, model)


def _input(png_bytes: bytes) -> ResumeRecoveryInput:
    return ResumeRecoveryInput(
        resume_id="resume_001",
        page_images=[png_bytes],
        extraction_results={
            "tika": "Jane Doe\nPython Developer",
            "fallback_1": "Jane Doe\nPython Dev...",
        },
        quality_report={"text_health": {"score": 42}},
    )


# -- happy path ---------------------------------------------------------------


def test_recovery_returns_structured_result(fake_client_factory, png_bytes, valid_payload_json):
    client = fake_client_factory(content=valid_payload_json)
    result = _task(client).run(_input(png_bytes))

    assert isinstance(result, ResumeRecoveryResult)
    assert result.success is True
    assert result.resume_id == "resume_001"
    assert result.recovered_text.startswith("Jane Doe")
    assert result.recovery_confidence == 0.92
    assert result.unreadable_sections == ["header logo"]
    assert result.issues == ["tika output dropped the Skills section"]
    # The model actually used is recorded, never hidden.
    assert result.model_name == "gpt-4o-mini"
    assert result.provider == "openai"
    assert result.token_usage == {
        "prompt_tokens": 10,
        "completion_tokens": 32,
        "total_tokens": 42,
    }


def test_recovery_prompt_carries_all_extractions_and_rules(
    fake_client_factory, png_bytes, valid_payload_json
):
    client = fake_client_factory(content=valid_payload_json)
    _task(client).run(_input(png_bytes))
    call = client.completions.calls[0]
    system = call["messages"][0]["content"]
    user = call["messages"][1]["content"]
    assert "SOURCE OF TRUTH" in system
    assert "NEVER invent" in system
    user_text = user[0]["text"]
    assert "tika" in user_text and "fallback_1" in user_text
    assert "text_health" in user_text
    # the page image itself was attached
    assert user[1]["type"] == "image_url"


def test_recovery_accepts_dict_input(fake_client_factory, png_bytes, valid_payload_json):
    client = fake_client_factory(content=valid_payload_json)
    result = _task(client).run(
        {
            "resume_id": "resume_002",
            "page_images": [png_bytes],
            "extraction_results": {"tika": "..."},
            "quality_report": {},
        }
    )
    assert result.resume_id == "resume_002"


def test_unreadable_content_is_marked_not_invented(
    fake_client_factory, png_bytes
):
    payload = {
        "recovered_text": "Jane Doe\n[UNREADABLE]\nSkills: Python",
        "recovery_confidence": 0.6,
        "unreadable_sections": ["experience dates"],
        "issues": [],
    }
    client = fake_client_factory(content=json.dumps(payload))
    result = _task(client).run(_input(png_bytes))
    assert "[UNREADABLE]" in result.recovered_text
    assert result.unreadable_sections == ["experience dates"]


# -- failure contract: nothing fabricated --------------------------------------


def test_api_failure_propagates_and_returns_nothing(fake_client_factory, png_bytes):
    client = fake_client_factory(error=RuntimeError("api down"))
    with pytest.raises(ModelAPIError):
        _task(client).run(_input(png_bytes))


def test_invalid_model_json_propagates(fake_client_factory, png_bytes):
    client = fake_client_factory(content="{not json")
    with pytest.raises(InvalidModelOutputError):
        _task(client).run(_input(png_bytes))


def test_confidence_out_of_range_is_rejected(fake_client_factory, png_bytes):
    payload = {
        "recovered_text": "Jane Doe",
        "recovery_confidence": 1.5,  # invalid: must be 0..1
        "unreadable_sections": [],
        "issues": [],
    }
    client = fake_client_factory(content=json.dumps(payload))
    with pytest.raises(InvalidModelOutputError):
        _task(client).run(_input(png_bytes))


def test_empty_recovered_text_is_rejected(fake_client_factory, png_bytes):
    payload = {
        "recovered_text": "  ",
        "recovery_confidence": 0.5,
        "unreadable_sections": [],
        "issues": [],
    }
    client = fake_client_factory(content=json.dumps(payload))
    with pytest.raises(InvalidModelOutputError):
        _task(client).run(_input(png_bytes))


def test_result_schema_validates_confidence_bounds():
    with pytest.raises(Exception):
        ResumeRecoveryResult(
            resume_id="r",
            recovered_text="x",
            recovery_confidence=2.0,
            model_name="gpt-4o-mini",
            provider="openai",
            success=True,
        )


# -- image utilities ------------------------------------------------------------


def test_prepare_image_accepts_png_and_jpeg(png_bytes, jpeg_bytes):
    png = prepare_image(png_bytes, page_index=0)
    assert png.media_type == "image/png"
    assert png.data_uri.startswith("data:image/png;base64,")
    jpeg = prepare_image(jpeg_bytes, page_index=1)
    assert jpeg.media_type == "image/jpeg"
    assert jpeg.page_index == 1


def test_prepare_image_accepts_base64_and_data_uri(png_bytes):
    encoded = base64.b64encode(png_bytes).decode("ascii")
    assert prepare_image(encoded, 0).media_type == "image/png"
    uri = f"data:image/png;base64,{encoded}"
    assert prepare_image(uri, 0).media_type == "image/png"


def test_prepare_image_rejects_garbage_bytes():
    with pytest.raises(ImageProcessingError):
        prepare_image(b"this is definitely not an image", page_index=0)


def test_prepare_image_rejects_unsupported_format():
    gif = b"GIF89a" + b"\x00" * 32
    with pytest.raises(ImageProcessingError):
        prepare_image(gif, page_index=0)


def test_prepare_image_rejects_empty_input():
    with pytest.raises(ImageProcessingError):
        prepare_image(b"", page_index=0)


def test_prepare_images_reports_failing_page_index(png_bytes):
    with pytest.raises(ImageProcessingError) as excinfo:
        prepare_images([png_bytes, b"junk"])
    assert "Page 1" in str(excinfo.value)


def test_prepare_image_downscales_oversized_pages():
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (3000, 3000), color=(200, 200, 200)).save(buffer, format="PNG")
    prepared = prepare_image(buffer.getvalue(), page_index=0, max_dimension=1000)
    assert max(prepared.width, prepared.height) <= 1000
