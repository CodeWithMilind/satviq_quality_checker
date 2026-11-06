"""Unit tests for the OpenAI provider — all API responses are mocked
via an injected fake client. No real API key, no network."""

import json

import httpx
import openai
import pytest
from pydantic import BaseModel, Field

from ai_model.config import AIModelConfig
from ai_model.exceptions import (
    ConfigurationError,
    InvalidModelOutputError,
    ModelAPIError,
    ModelTimeoutError,
)
from ai_model.providers.openai_provider import OpenAIProvider
from ai_model.utils.image_utils import prepare_image


class _Answer(BaseModel):
    answer: str
    confidence: float = Field(ge=0.0, le=1.0)


def _provider(client, config=None, **kwargs) -> OpenAIProvider:
    return OpenAIProvider(
        config or AIModelConfig(openai_api_key="sk-test", max_retries=2),
        client=client,
        retry_backoff_seconds=0,
        **kwargs,
    )


def _timeout_error() -> openai.APITimeoutError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.APITimeoutError(request)


def _status_error(status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status, request=request)
    if status == 429:
        return openai.RateLimitError("rate limited", response=response, body=None)
    if status == 401:
        return openai.AuthenticationError("bad key", response=response, body=None)
    return openai.InternalServerError("server error", response=response, body=None)


# -- construction -----------------------------------------------------------


def test_missing_api_key_raises_configuration_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ConfigurationError):
        OpenAIProvider(AIModelConfig(openai_api_key=None))


def test_api_key_never_appears_in_config_repr_or_errors():
    config = AIModelConfig(openai_api_key="sk-super-secret-value")
    assert "sk-super-secret-value" not in repr(config)


# -- generate_text ------------------------------------------------------------


def test_generate_text_returns_content_and_usage(fake_client_factory):
    client = fake_client_factory(content="Hello world")
    provider = _provider(client)
    response = provider.generate_text(
        model_id="gpt-4o-mini", model_name="gpt-4o-mini", prompt="Say hi"
    )
    assert response.text == "Hello world"
    assert response.provider == "openai"
    assert response.model_name == "gpt-4o-mini"
    assert response.token_usage == {
        "prompt_tokens": 10,
        "completion_tokens": 32,
        "total_tokens": 42,
    }
    sent = client.completions.calls[0]
    assert sent["model"] == "gpt-4o-mini"
    assert sent["messages"][-1] == {"role": "user", "content": "Say hi"}


# -- structured output ----------------------------------------------------------


def test_generate_structured_output_parses_valid_json(fake_client_factory):
    payload = {"answer": "Paris", "confidence": 0.9}
    client = fake_client_factory(content=json.dumps(payload))
    provider = _provider(client)
    result = provider.generate_structured_output(
        model_id="gpt-4o-mini",
        model_name="gpt-4o-mini",
        prompt="Capital of France?",
        response_schema=_Answer,
    )
    assert isinstance(result.parsed, _Answer)
    assert result.parsed.answer == "Paris"
    assert client.completions.calls[0]["response_format"] == {"type": "json_object"}


def test_structured_output_with_images_sends_image_parts(fake_client_factory, png_bytes):
    payload = {"answer": "seen", "confidence": 0.5}
    client = fake_client_factory(content=json.dumps(payload))
    provider = _provider(client)
    image = prepare_image(png_bytes, page_index=0)
    provider.generate_structured_output(
        model_id="gpt-4o-mini",
        model_name="gpt-4o-mini",
        prompt="Describe",
        response_schema=_Answer,
        images=[image],
    )
    content = client.completions.calls[0]["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "Describe"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_invalid_json_raises_invalid_model_output(fake_client_factory):
    client = fake_client_factory(content="this is not json at all")
    provider = _provider(client)
    with pytest.raises(InvalidModelOutputError):
        provider.generate_structured_output(
            model_id="gpt-4o-mini",
            model_name="gpt-4o-mini",
            prompt="x",
            response_schema=_Answer,
        )


def test_schema_violation_raises_invalid_model_output(fake_client_factory):
    # confidence = 7.5 violates the 0..1 constraint
    client = fake_client_factory(content=json.dumps({"answer": "x", "confidence": 7.5}))
    provider = _provider(client)
    with pytest.raises(InvalidModelOutputError):
        provider.generate_structured_output(
            model_id="gpt-4o-mini",
            model_name="gpt-4o-mini",
            prompt="x",
            response_schema=_Answer,
        )


def test_empty_content_raises_invalid_model_output(fake_client_factory):
    client = fake_client_factory(content="   ")
    provider = _provider(client)
    with pytest.raises(InvalidModelOutputError):
        provider.generate_text(
            model_id="gpt-4o-mini", model_name="gpt-4o-mini", prompt="x"
        )


# -- analyze_images ----------------------------------------------------------


def test_analyze_images_requires_at_least_one_image(fake_client_factory):
    from ai_model.exceptions import ImageProcessingError

    provider = _provider(fake_client_factory())
    with pytest.raises(ImageProcessingError):
        provider.analyze_images(
            model_id="gpt-4o-mini", model_name="gpt-4o-mini", images=[], prompt="x"
        )


def test_analyze_images_sends_all_pages(fake_client_factory, png_bytes, jpeg_bytes):
    client = fake_client_factory(content="two pages seen")
    provider = _provider(client)
    images = [prepare_image(png_bytes, 0), prepare_image(jpeg_bytes, 1)]
    response = provider.analyze_images(
        model_id="gpt-4o-mini", model_name="gpt-4o-mini", images=images, prompt="Read"
    )
    assert response.text == "two pages seen"
    content = client.completions.calls[0]["messages"][-1]["content"]
    image_parts = [p for p in content if p["type"] == "image_url"]
    assert len(image_parts) == 2


# -- retries and failure mapping --------------------------------------------


def test_timeout_is_retried_then_raises_model_timeout(fake_client_factory):
    client = fake_client_factory(error=_timeout_error())
    provider = _provider(client)  # max_retries=2 -> 3 attempts
    with pytest.raises(ModelTimeoutError) as excinfo:
        provider.generate_text(
            model_id="gpt-4o-mini", model_name="gpt-4o-mini", prompt="x"
        )
    assert excinfo.value.retryable is True
    assert len(client.completions.calls) == 3


def test_transient_error_then_success_recovers(fake_client_factory, valid_payload_json):
    client = fake_client_factory(
        content=valid_payload_json, error=[_status_error(500)]
    )
    provider = _provider(client)
    response = provider.generate_text(
        model_id="gpt-4o-mini", model_name="gpt-4o-mini", prompt="x"
    )
    assert response.text == valid_payload_json
    assert len(client.completions.calls) == 2


def test_rate_limit_is_retryable_api_error(fake_client_factory):
    client = fake_client_factory(error=_status_error(429))
    provider = _provider(client)
    with pytest.raises(ModelAPIError) as excinfo:
        provider.generate_text(
            model_id="gpt-4o-mini", model_name="gpt-4o-mini", prompt="x"
        )
    assert excinfo.value.status_code == 429
    assert excinfo.value.retryable is True
    assert len(client.completions.calls) == 3  # retried, then gave up


def test_authentication_error_is_not_retried(fake_client_factory):
    client = fake_client_factory(error=_status_error(401))
    provider = _provider(client)
    with pytest.raises(ModelAPIError) as excinfo:
        provider.generate_text(
            model_id="gpt-4o-mini", model_name="gpt-4o-mini", prompt="x"
        )
    assert excinfo.value.retryable is False
    assert len(client.completions.calls) == 1  # no pointless retries


def test_unexpected_error_is_wrapped_not_leaked(fake_client_factory):
    client = fake_client_factory(error=RuntimeError("boom"))
    provider = _provider(client)
    with pytest.raises(ModelAPIError):
        provider.generate_text(
            model_id="gpt-4o-mini", model_name="gpt-4o-mini", prompt="x"
        )
    assert len(client.completions.calls) == 1


# -- health check ------------------------------------------------------------


def test_health_check_true_when_models_list_succeeds(fake_client_factory):
    provider = _provider(fake_client_factory())
    result = provider.health_check()
    assert bool(result) is True
    assert result.healthy is True
    assert result.provider == "openai"


def test_health_check_false_instead_of_raising(fake_client_factory):
    provider = _provider(fake_client_factory(models_error=RuntimeError("down")))
    result = provider.health_check()
    assert result.healthy is False
