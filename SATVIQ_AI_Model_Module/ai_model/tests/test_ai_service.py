"""Unit tests for the public AIService interface and configuration."""

import json

import pytest
from pydantic import ValidationError

from ai_model import AIService
from ai_model.config import AIModelConfig
from ai_model.exceptions import (
    ConfigurationError,
    ModelAPIError,
    ProviderNotAvailableError,
    UnsupportedCapabilityError,
)
from ai_model.model_registry import ModelRegistry
from ai_model.providers.local_provider import LocalProvider
from ai_model.schemas import InputType, ModelDefinition


def _service(client, config=None, **kwargs) -> AIService:
    return AIService(
        config or AIModelConfig(openai_api_key="sk-test", max_retries=0),
        openai_client=client,
        **kwargs,
    )


# -- end-to-end through the public interface -------------------------------------


def test_recover_resume_end_to_end(fake_client_factory, png_bytes, valid_payload_json):
    client = fake_client_factory(content=valid_payload_json)
    service = _service(client)
    result = service.recover_resume(
        resume_id="resume_001",
        page_images=[png_bytes],
        extraction_results={"tika": "Jane Doe", "fallback_1": "Jane D..."},
        quality_report={"status": "FAIL"},
    )
    assert result.success is True
    assert result.model_name == "gpt-4o-mini"
    assert result.provider == "openai"
    # routing was recorded
    assert service.router.last_selection is not None
    assert service.router.last_selection.task == "resume_recovery"


def test_recover_text_end_to_end(fake_client_factory):
    payload = {
        "recovered_text": "Jane Doe — Python Developer",
        "recovery_confidence": 0.88,
        "issues": ["fixed garbled characters"],
    }
    client = fake_client_factory(content=json.dumps(payload))
    service = _service(client)
    result = service.recover_text(
        text_id="resume_001",
        corrupted_text="Jane D0e — Pyth0n Developer",
        reference_texts={"tika": "Jane Doe — Python Developer"},
    )
    assert result.success is True
    assert result.recovered_text == "Jane Doe — Python Developer"
    assert result.recovery_confidence == 0.88


def test_list_models_shows_registered_models(fake_client_factory):
    service = _service(fake_client_factory())
    models = service.list_models()
    assert [m.name for m in models] == ["gpt-4o-mini"]


def test_select_model_matches_spec_example(fake_client_factory):
    service = _service(fake_client_factory())
    model = service.select_model(
        task="resume_recovery",
        required_capabilities=["vision", "structured_output"],
    )
    assert model.name == "gpt-4o-mini"


# -- configuration -----------------------------------------------------------------


def test_config_from_env_reads_all_variables():
    config = AIModelConfig.from_env(
        {
            "OPENAI_API_KEY": "sk-env-key",
            "OPENAI_MODEL": "gpt-4o-mini",
            "AI_PROVIDER": "openai",
            "AI_MODEL_TIMEOUT": "30",
            "AI_MODEL_MAX_RETRIES": "3",
            "AI_MODEL_ENABLED": "false",
        }
    )
    assert config.openai_api_key == "sk-env-key"
    assert config.timeout_seconds == 30
    assert config.max_retries == 3
    assert config.enabled is False
    assert "sk-env-key" not in repr(config)


def test_config_rejects_invalid_values():
    with pytest.raises(ValidationError):
        AIModelConfig(timeout_seconds=-5)
    with pytest.raises(ValidationError):
        AIModelConfig(max_retries=99)
    with pytest.raises(ValueError):
        AIModelConfig.from_env({"AI_MODEL_ENABLED": "maybe"})


def test_missing_api_key_fails_on_use_not_on_construction(fake_client_factory):
    # Service construction is fine; the failure surfaces when the
    # OpenAI provider is actually needed.
    service = AIService(AIModelConfig(openai_api_key=None))
    with pytest.raises(ConfigurationError):
        service.recover_resume(
            resume_id="r1",
            page_images=[],
            extraction_results={"tika": "x"},
            quality_report={},
        )


def test_disabled_module_refuses_recovery(fake_client_factory, png_bytes, valid_payload_json):
    service = _service(
        fake_client_factory(content=valid_payload_json),
        config=AIModelConfig(openai_api_key="sk-test", enabled=False),
    )
    with pytest.raises(ConfigurationError):
        service.recover_resume(
            resume_id="r1",
            page_images=[png_bytes],
            extraction_results={},
            quality_report={},
        )


def test_api_failure_is_not_masked_by_a_fabricated_result(
    fake_client_factory, png_bytes
):
    client = fake_client_factory(error=RuntimeError("provider down"))
    service = _service(client)
    with pytest.raises(ModelAPIError):
        service.recover_resume(
            resume_id="r1",
            page_images=[png_bytes],
            extraction_results={"tika": "x"},
            quality_report={},
        )


# -- routing / providers --------------------------------------------------------------


def test_service_raises_when_no_model_can_do_the_task(fake_client_factory):
    text_only = ModelDefinition(
        name="text-only",
        provider="openai",
        model_id="text-only",
        input_types=[InputType.TEXT],
        supports_vision=False,
        supports_structured_output=False,
        max_context_window=8_000,
        enabled=True,
    )
    service = _service(
        fake_client_factory(), registry=ModelRegistry([text_only])
    )
    with pytest.raises(UnsupportedCapabilityError):
        service.recover_resume(
            resume_id="r1", page_images=[], extraction_results={}, quality_report={}
        )


def test_local_provider_is_a_clear_placeholder():
    provider = LocalProvider(AIModelConfig())
    assert provider.health_check().healthy is False
    with pytest.raises(ProviderNotAvailableError):
        provider.generate_text(model_id="x", model_name="x", prompt="hi")


def test_health_check_reports_openai(fake_client_factory):
    service = _service(fake_client_factory())
    results = service.health_check()
    assert results["openai"].healthy is True


def test_custom_provider_can_be_injected(fake_client_factory, png_bytes, valid_payload_json):
    # A caller-supplied provider dict bypasses lazy construction.
    from ai_model.providers.openai_provider import OpenAIProvider

    provider = OpenAIProvider(
        AIModelConfig(openai_api_key="sk-test", max_retries=0),
        client=fake_client_factory(content=valid_payload_json),
        retry_backoff_seconds=0,
    )
    service = AIService(
        AIModelConfig(openai_api_key="sk-test"),
        providers={"openai": provider},
    )
    result = service.recover_resume(
        resume_id="r9",
        page_images=[png_bytes],
        extraction_results={},
        quality_report={},
    )
    assert result.success is True
