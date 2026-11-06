"""Local provider placeholder.

Reserves the provider slot for a future local integration (Ollama or
another local SLM server). No local inference is implemented yet:
generation methods raise :class:`ProviderNotAvailableError` with a
clear message, and ``health_check`` reports unhealthy. The point of
this class is that the registry, router, tasks, and service already
treat "local" as a first-class provider name, so wiring a real local
backend later touches only this file.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..config import AIModelConfig
from ..exceptions import ProviderNotAvailableError
from ..utils.image_utils import PreparedImage
from .base_provider import (
    BaseProvider,
    HealthCheckResult,
    ProviderResponse,
    StructuredProviderResponse,
)

_NOT_IMPLEMENTED = (
    "The local provider is a placeholder and is not implemented yet. "
    "Configure AI_PROVIDER=openai, or implement this provider against "
    "a local server (e.g. Ollama) before selecting a local model."
)


class LocalProvider(BaseProvider):
    name = "local"

    def __init__(self, config: AIModelConfig | None = None) -> None:
        self._config = config or AIModelConfig.from_env()
        self.base_url = self._config.local_base_url

    def generate_text(self, **kwargs) -> ProviderResponse:  # type: ignore[override]
        raise ProviderNotAvailableError(_NOT_IMPLEMENTED)

    def generate_structured_output(self, **kwargs) -> StructuredProviderResponse:  # type: ignore[override]
        raise ProviderNotAvailableError(_NOT_IMPLEMENTED)

    def analyze_images(self, **kwargs) -> ProviderResponse:  # type: ignore[override]
        raise ProviderNotAvailableError(_NOT_IMPLEMENTED)

    def health_check(self) -> HealthCheckResult:
        return HealthCheckResult(
            provider=self.name,
            healthy=False,
            detail="Local provider is a placeholder; no local inference backend is configured.",
        )
