"""Task-based model routing.

The router selects a model from the registry for a task, checking
capabilities and enabled status. **Every selection is recorded** in
``selection_history`` (task, model, provider, capabilities, time) —
the router never silently switches models: the task layer copies the
selected model's name and provider into the result it returns.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from .exceptions import ModelNotFoundError, UnsupportedCapabilityError
from .model_registry import ModelRegistry
from .schemas import ModelDefinition

# Capabilities each known task needs. Unknown tasks may pass explicit
# ``required_capabilities`` to ``select_model``.
TASK_REQUIREMENTS: dict[str, list[str]] = {
    "resume_recovery": ["vision", "structured_output"],
    "text_recovery": ["structured_output"],
}


class SelectionRecord(BaseModel):
    """Audit record of one routing decision."""

    model_config = ConfigDict(extra="forbid")

    task: str | None
    model_name: str
    provider: str
    required_capabilities: list[str] = Field(default_factory=list)
    selected_at: datetime


class ModelRouter:
    def __init__(
        self,
        registry: ModelRegistry,
        *,
        preferred_model: str | None = None,
    ) -> None:
        self._registry = registry
        self._preferred_model = preferred_model
        self.selection_history: list[SelectionRecord] = []

    @property
    def registry(self) -> ModelRegistry:
        return self._registry

    @property
    def last_selection(self) -> SelectionRecord | None:
        return self.selection_history[-1] if self.selection_history else None

    def select_model(
        self,
        task: str | None = None,
        required_capabilities: list[str] | None = None,
        *,
        preferred_model: str | None = None,
        provider: str | None = None,
    ) -> ModelDefinition:
        """Select the best enabled model for a task.

        Selection order:
        1. An explicitly preferred model (argument, else router-level
           preference, else the task default) that qualifies.
        2. Otherwise the qualifying model with the largest context
           window (ties broken by name, so selection is deterministic).

        Raises:
            ModelNotFoundError: a named preferred model is not registered.
            UnsupportedCapabilityError: no enabled model satisfies the
                required capabilities.
        """
        capabilities = list(required_capabilities or [])
        if task is not None and task in TASK_REQUIREMENTS:
            for cap in TASK_REQUIREMENTS[task]:
                if cap not in capabilities:
                    capabilities.append(cap)

        candidates = self._registry.find_by_capabilities(capabilities, provider=provider)
        if not candidates:
            enabled = self._registry.list_models(enabled_only=True, provider=provider)
            raise UnsupportedCapabilityError(
                f"No enabled model satisfies capabilities {capabilities} "
                f"for task {task!r}. Enabled models: "
                f"{[(m.name, sorted(m.capabilities)) for m in enabled] or 'none'}"
            )

        preferred_name = preferred_model or self._preferred_model
        chosen: ModelDefinition | None = None
        if preferred_name is not None:
            # A named preference that does not exist is a hard error;
            # one that exists but is disabled/unqualified falls through
            # to the best qualifying candidate.
            preferred = self._registry.get(preferred_name)
            if preferred in candidates:
                chosen = preferred
        if chosen is None:
            chosen = sorted(
                candidates,
                key=lambda m: (-m.max_context_window, m.name),
            )[0]

        self.selection_history.append(
            SelectionRecord(
                task=task,
                model_name=chosen.name,
                provider=chosen.provider,
                required_capabilities=capabilities,
                selected_at=datetime.now(timezone.utc),
            )
        )
        return chosen
