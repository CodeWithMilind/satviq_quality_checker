"""Provider abstraction.

Every AI provider (OpenAI today; local/Ollama and others later) must
implement this interface. Tasks depend only on this base class, never
on a concrete provider or SDK.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from ..utils.image_utils import PreparedImage


@dataclass
class ProviderResponse:
    """Normalised text response from a provider."""

    text: str
    model_name: str  # registry name
    model_id: str  # provider-side identifier actually called
    provider: str
    token_usage: dict[str, int] | None = None
    finish_reason: str | None = None
    raw: Any = field(default=None, repr=False)


@dataclass
class StructuredProviderResponse:
    """Normalised structured response: ``parsed`` is an instance of the
    Pydantic schema the caller requested."""

    parsed: BaseModel
    model_name: str
    model_id: str
    provider: str
    token_usage: dict[str, int] | None = None
    raw_text: str = ""
    raw: Any = field(default=None, repr=False)


@dataclass
class HealthCheckResult:
    provider: str
    healthy: bool
    detail: str = ""

    def __bool__(self) -> bool:
        return self.healthy


class BaseProvider(ABC):
    """Interface every provider must implement."""

    #: Provider name, matching ``ModelDefinition.provider``.
    name: str = "base"

    @abstractmethod
    def generate_text(
        self,
        *,
        model_id: str,
        model_name: str,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        """Generate a plain-text completion."""
        raise NotImplementedError

    @abstractmethod
    def generate_structured_output(
        self,
        *,
        model_id: str,
        model_name: str,
        prompt: str,
        response_schema: type[BaseModel],
        system_prompt: str | None = None,
        images: list[PreparedImage] | None = None,
        temperature: float = 0.0,
    ) -> StructuredProviderResponse:
        """Generate output validated against ``response_schema``.

        Implementations must raise
        :class:`~ai_model.exceptions.InvalidModelOutputError` when the
        model's output cannot be parsed or validated — never return a
        fabricated substitute.
        """
        raise NotImplementedError

    @abstractmethod
    def analyze_images(
        self,
        *,
        model_id: str,
        model_name: str,
        images: list[PreparedImage],
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.0,
    ) -> ProviderResponse:
        """Analyze one or more images with a text prompt."""
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> HealthCheckResult:
        """Return whether the provider is reachable and usable."""
        raise NotImplementedError
