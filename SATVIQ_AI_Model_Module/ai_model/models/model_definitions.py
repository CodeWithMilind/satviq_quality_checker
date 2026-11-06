"""Built-in model definitions.

Add a new model by adding a definition here (or by registering one at
runtime with ``ModelRegistry.register``) — no service, router, or task
code needs to change.
"""

from __future__ import annotations

from ..schemas import InputType, ModelDefinition

GPT_4O_MINI = ModelDefinition(
    name="gpt-4o-mini",
    provider="openai",
    model_id="gpt-4o-mini",
    input_types=[InputType.TEXT, InputType.IMAGE],
    supports_vision=True,
    supports_structured_output=True,
    max_context_window=128_000,
    enabled=True,
    description="OpenAI GPT-4o mini — multimodal, structured output, default model.",
)

# Example of a future model. Defined for documentation/testing purposes
# but NOT part of the default registry: flip ``enabled`` and add it to
# ``default_model_definitions()`` when it should become selectable.
GPT_4O = ModelDefinition(
    name="gpt-4o",
    provider="openai",
    model_id="gpt-4o",
    input_types=[InputType.TEXT, InputType.IMAGE],
    supports_vision=True,
    supports_structured_output=True,
    max_context_window=128_000,
    enabled=False,
    description="OpenAI GPT-4o — higher capability, higher cost. Disabled by default.",
)

# Placeholder for a future local SLM served via Ollama. The local
# provider is not implemented yet, so this stays disabled.
LOCAL_PLACEHOLDER = ModelDefinition(
    name="local-placeholder",
    provider="local",
    model_id="placeholder",
    input_types=[InputType.TEXT],
    supports_vision=False,
    supports_structured_output=False,
    max_context_window=8_192,
    enabled=False,
    description="Placeholder for a future local (Ollama) model. Not implemented yet.",
)


def default_model_definitions() -> list[ModelDefinition]:
    """Definitions registered by default. Initially: GPT-4o-mini only."""
    return [GPT_4O_MINI]
