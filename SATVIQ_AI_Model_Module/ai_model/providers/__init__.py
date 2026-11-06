"""AI providers. Tasks depend on BaseProvider only."""

from .base_provider import (
    BaseProvider,
    HealthCheckResult,
    ProviderResponse,
    StructuredProviderResponse,
)
from .local_provider import LocalProvider
from .openai_provider import OpenAIProvider

__all__ = [
    "BaseProvider",
    "HealthCheckResult",
    "ProviderResponse",
    "StructuredProviderResponse",
    "LocalProvider",
    "OpenAIProvider",
]
