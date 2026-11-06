"""Unit tests for the model registry."""

import pytest

from ai_model.exceptions import ModelNotFoundError
from ai_model.model_registry import ModelRegistry, create_default_registry
from ai_model.models.model_definitions import GPT_4O
from ai_model.schemas import InputType, ModelDefinition


def _definition(name: str, **overrides) -> ModelDefinition:
    base = dict(
        name=name,
        provider="openai",
        model_id=name,
        input_types=[InputType.TEXT],
        supports_vision=False,
        supports_structured_output=True,
        max_context_window=16_000,
        enabled=True,
    )
    base.update(overrides)
    return ModelDefinition(**base)


def test_default_registry_contains_gpt_4o_mini():
    registry = create_default_registry()
    model = registry.get("gpt-4o-mini")
    assert model.provider == "openai"
    assert model.model_id == "gpt-4o-mini"
    assert model.supports_vision is True
    assert model.supports_structured_output is True
    assert model.enabled is True
    assert model.max_context_window == 128_000


def test_register_and_get_round_trip():
    registry = ModelRegistry()
    registry.register(_definition("my-model"))
    assert "my-model" in registry
    assert len(registry) == 1
    assert registry.get("my-model").name == "my-model"


def test_register_duplicate_name_rejected_unless_overwrite():
    registry = ModelRegistry([_definition("dup")])
    with pytest.raises(ValueError):
        registry.register(_definition("dup"))
    registry.register(_definition("dup", max_context_window=32_000), overwrite=True)
    assert registry.get("dup").max_context_window == 32_000


def test_get_unknown_model_raises_model_not_found():
    registry = create_default_registry()
    with pytest.raises(ModelNotFoundError):
        registry.get("does-not-exist")


def test_unregister_unknown_model_raises():
    registry = ModelRegistry()
    with pytest.raises(ModelNotFoundError):
        registry.unregister("ghost")


def test_capabilities_are_derived_from_definition():
    model = _definition(
        "vision-model",
        input_types=[InputType.TEXT, InputType.IMAGE],
        supports_vision=True,
    )
    assert model.capabilities == {"text", "image", "vision", "structured_output"}
    assert model.supports(["vision", "structured_output"])
    assert not model.supports(["vision", "nonexistent-cap"])


def test_find_by_capabilities_filters_enabled_and_capable():
    registry = ModelRegistry(
        [
            _definition("text-only"),
            _definition(
                "vision",
                input_types=[InputType.TEXT, InputType.IMAGE],
                supports_vision=True,
            ),
            _definition(
                "vision-disabled",
                input_types=[InputType.TEXT, InputType.IMAGE],
                supports_vision=True,
                enabled=False,
            ),
        ]
    )
    found = registry.find_by_capabilities(["vision", "structured_output"])
    assert [m.name for m in found] == ["vision"]


def test_find_by_capabilities_rejects_unknown_capability():
    registry = create_default_registry()
    with pytest.raises(ValueError):
        registry.find_by_capabilities(["telepathy"])


def test_set_enabled_toggles_model():
    registry = ModelRegistry([_definition("toggle")])
    registry.set_enabled("toggle", False)
    assert registry.get("toggle").enabled is False
    assert registry.list_models(enabled_only=True) == []
    registry.set_enabled("toggle", True)
    assert len(registry.list_models(enabled_only=True)) == 1


def test_list_models_filters_by_provider():
    registry = ModelRegistry(
        [
            _definition("a", provider="openai"),
            _definition("b", provider="local"),
        ]
    )
    assert [m.name for m in registry.list_models(provider="local")] == ["b"]


def test_future_model_can_register_without_core_changes():
    registry = create_default_registry()
    registry.register(GPT_4O.model_copy(update={"enabled": True}))
    assert {m.name for m in registry.list_models()} == {"gpt-4o-mini", "gpt-4o"}
