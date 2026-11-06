"""Central registry of AI models.

The registry is the single source of truth for which models exist,
what they can do, and whether they are enabled. Nothing else in the
module hardcodes model names.
"""

from __future__ import annotations

from .exceptions import ModelNotFoundError
from .models.model_definitions import default_model_definitions
from .schemas import KNOWN_CAPABILITIES, ModelDefinition


class ModelRegistry:
    """In-memory registry of :class:`ModelDefinition` objects."""

    def __init__(self, definitions: list[ModelDefinition] | None = None) -> None:
        self._models: dict[str, ModelDefinition] = {}
        for definition in definitions or []:
            self.register(definition)

    # -- mutation ---------------------------------------------------------

    def register(self, definition: ModelDefinition, *, overwrite: bool = False) -> ModelDefinition:
        """Register a model. Raises ValueError on a duplicate name
        unless ``overwrite=True``."""
        if definition.name in self._models and not overwrite:
            raise ValueError(f"Model {definition.name!r} is already registered")
        self._models[definition.name] = definition
        return definition

    def unregister(self, name: str) -> None:
        if name not in self._models:
            raise ModelNotFoundError(f"Model {name!r} is not registered")
        del self._models[name]

    def set_enabled(self, name: str, enabled: bool) -> ModelDefinition:
        definition = self.get(name)
        updated = definition.model_copy(update={"enabled": enabled})
        self._models[name] = updated
        return updated

    # -- lookup -----------------------------------------------------------

    def get(self, name: str) -> ModelDefinition:
        """Return a definition by registry name or raise
        :class:`ModelNotFoundError`."""
        try:
            return self._models[name]
        except KeyError:
            raise ModelNotFoundError(
                f"Model {name!r} is not registered. "
                f"Registered models: {sorted(self._models) or 'none'}"
            ) from None

    def list_models(
        self,
        *,
        enabled_only: bool = False,
        provider: str | None = None,
    ) -> list[ModelDefinition]:
        models = list(self._models.values())
        if enabled_only:
            models = [m for m in models if m.enabled]
        if provider is not None:
            models = [m for m in models if m.provider == provider]
        return sorted(models, key=lambda m: m.name)

    def find_by_capabilities(
        self,
        required_capabilities: list[str] | set[str],
        *,
        provider: str | None = None,
    ) -> list[ModelDefinition]:
        """Enabled models that satisfy every required capability.

        Raises ValueError for an unknown capability name so typos fail
        loudly instead of silently matching nothing.
        """
        unknown = set(required_capabilities) - KNOWN_CAPABILITIES
        if unknown:
            raise ValueError(
                f"Unknown capabilities: {sorted(unknown)}. "
                f"Known capabilities: {sorted(KNOWN_CAPABILITIES)}"
            )
        return [
            m
            for m in self.list_models(enabled_only=True, provider=provider)
            if m.supports(required_capabilities)
        ]

    def __contains__(self, name: str) -> bool:
        return name in self._models

    def __len__(self) -> int:
        return len(self._models)


def create_default_registry() -> ModelRegistry:
    """Registry pre-loaded with the built-in definitions
    (initially GPT-4o-mini)."""
    return ModelRegistry(default_model_definitions())
