"""Unit tests for the model router."""

import pytest

from ai_model.exceptions import ModelNotFoundError, UnsupportedCapabilityError
from ai_model.model_registry import ModelRegistry, create_default_registry
from ai_model.model_router import ModelRouter
from ai_model.schemas import InputType, ModelDefinition


def _definition(name: str, **overrides) -> ModelDefinition:
    base = dict(
        name=name,
        provider="openai",
        model_id=name,
        input_types=[InputType.TEXT, InputType.IMAGE],
        supports_vision=True,
        supports_structured_output=True,
        max_context_window=16_000,
        enabled=True,
    )
    base.update(overrides)
    return ModelDefinition(**base)


def test_router_selects_gpt_4o_mini_for_resume_recovery():
    router = ModelRouter(create_default_registry())
    selected = router.select_model(
        task="resume_recovery",
        required_capabilities=["vision", "structured_output"],
    )
    assert selected.name == "gpt-4o-mini"
    assert selected.provider == "openai"


def test_router_task_requirements_are_applied_automatically():
    router = ModelRouter(create_default_registry())
    selected = router.select_model(task="resume_recovery")
    assert selected.supports_vision and selected.supports_structured_output


def test_router_records_every_selection():
    router = ModelRouter(create_default_registry())
    router.select_model(task="resume_recovery")
    router.select_model(task="text_recovery")
    assert len(router.selection_history) == 2
    record = router.last_selection
    assert record is not None
    assert record.task == "text_recovery"
    assert record.model_name == "gpt-4o-mini"
    assert record.provider == "openai"
    assert "structured_output" in record.required_capabilities


def test_router_raises_when_no_model_has_required_capability():
    registry = ModelRegistry(
        [_definition("text-only", input_types=[InputType.TEXT], supports_vision=False)]
    )
    router = ModelRouter(registry)
    with pytest.raises(UnsupportedCapabilityError):
        router.select_model(task="resume_recovery", required_capabilities=["vision"])


def test_router_skips_disabled_models():
    registry = ModelRegistry(
        [
            _definition("capable-but-disabled", enabled=False),
            _definition("capable", max_context_window=8_000),
        ]
    )
    router = ModelRouter(registry)
    assert router.select_model(task="resume_recovery").name == "capable"


def test_router_raises_when_all_capable_models_disabled():
    registry = ModelRegistry([_definition("off", enabled=False)])
    router = ModelRouter(registry)
    with pytest.raises(UnsupportedCapabilityError):
        router.select_model(task="resume_recovery")


def test_router_prefers_largest_context_window():
    registry = ModelRegistry(
        [
            _definition("small", max_context_window=8_000),
            _definition("large", max_context_window=128_000),
        ]
    )
    router = ModelRouter(registry)
    assert router.select_model(task="text_recovery").name == "large"


def test_router_honours_preferred_model_when_it_qualifies():
    registry = ModelRegistry(
        [
            _definition("small", max_context_window=8_000),
            _definition("large", max_context_window=128_000),
        ]
    )
    router = ModelRouter(registry, preferred_model="small")
    assert router.select_model(task="text_recovery").name == "small"


def test_router_unknown_preferred_model_raises_not_found():
    router = ModelRouter(create_default_registry())
    with pytest.raises(ModelNotFoundError):
        router.select_model(task="text_recovery", preferred_model="nope")


def test_router_filters_by_provider():
    registry = ModelRegistry(
        [
            _definition("oai", provider="openai"),
            _definition("loc", provider="local"),
        ]
    )
    router = ModelRouter(registry)
    selected = router.select_model(task="text_recovery", provider="local")
    assert selected.name == "loc"
