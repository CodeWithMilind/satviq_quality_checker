"""Public service interface for the ``ai_model`` module.

This is the stable integration surface for the frontend and the
Quality Checker::

    from ai_model.ai_service import AIService

    service = AIService()
    result = service.recover_resume(
        resume_id="resume_001",
        page_images=page_images,
        extraction_results=extraction_results,
        quality_report=quality_report,
    )

The service owns the registry, router, and provider instances. It
receives extraction results and quality reports as plain arguments —
it imports nothing from the extractor, quality checker, or frontend.
"""

from __future__ import annotations

from typing import Any

from .config import AIModelConfig
from .exceptions import ConfigurationError
from .model_registry import ModelRegistry, create_default_registry
from .model_router import ModelRouter
from .providers.base_provider import BaseProvider, HealthCheckResult
from .providers.local_provider import LocalProvider
from .providers.openai_provider import OpenAIProvider
from .schemas import (
    ModelDefinition,
    ResumeRecoveryInput,
    ResumeRecoveryResult,
    TextRecoveryInput,
    TextRecoveryResult,
)
from .tasks.resume_recovery import ResumeRecoveryTask
from .tasks.text_recovery import TextRecoveryTask


class AIService:
    """Facade over registry + router + providers + tasks."""

    def __init__(
        self,
        config: AIModelConfig | None = None,
        *,
        registry: ModelRegistry | None = None,
        providers: dict[str, BaseProvider] | None = None,
        openai_client: Any = None,
    ) -> None:
        """
        Args:
            config: Module configuration. Defaults to
                ``AIModelConfig.from_env()``.
            registry: Model registry. Defaults to the built-in
                registry (GPT-4o-mini).
            providers: Pre-built providers keyed by provider name.
                Mainly for tests and custom integrations; providers
                are otherwise created lazily on first use.
            openai_client: A pre-built OpenAI SDK client to inject
                into the OpenAI provider (used by tests with mocks).
        """
        self._config = config or AIModelConfig.from_env()
        self._registry = registry or create_default_registry()
        self._router = ModelRouter(
            self._registry, preferred_model=self._config.default_model
        )
        self._providers: dict[str, BaseProvider] = dict(providers or {})
        self._openai_client = openai_client

    # -- introspection ------------------------------------------------------

    @property
    def config(self) -> AIModelConfig:
        return self._config

    @property
    def registry(self) -> ModelRegistry:
        return self._registry

    @property
    def router(self) -> ModelRouter:
        return self._router

    def list_models(self, *, enabled_only: bool = False) -> list[ModelDefinition]:
        return self._registry.list_models(enabled_only=enabled_only)

    def select_model(
        self,
        task: str | None = None,
        required_capabilities: list[str] | None = None,
        *,
        preferred_model: str | None = None,
    ) -> ModelDefinition:
        """Ask the router which model a task would use (recorded in
        the router's selection history)."""
        self._ensure_enabled()
        return self._router.select_model(
            task=task,
            required_capabilities=required_capabilities,
            preferred_model=preferred_model,
        )

    def health_check(self) -> dict[str, HealthCheckResult]:
        """Health-check every provider that has registered models."""
        results: dict[str, HealthCheckResult] = {}
        provider_names = {m.provider for m in self._registry.list_models()}
        for provider_name in sorted(provider_names):
            results[provider_name] = self._get_provider(provider_name).health_check()
        return results

    # -- tasks ----------------------------------------------------------------

    def recover_resume(
        self,
        resume_id: str,
        page_images: list[Any] | None = None,
        extraction_results: dict[str, str] | None = None,
        quality_report: dict[str, Any] | None = None,
        *,
        preferred_model: str | None = None,
    ) -> ResumeRecoveryResult:
        """Recover a resume's text using the routed vision model.

        Raises module exceptions on failure (configuration, routing,
        image, API, malformed output). Never returns a fabricated
        result when the model call fails.
        """
        self._ensure_enabled()
        model = self._router.select_model(
            task="resume_recovery", preferred_model=preferred_model
        )
        provider = self._get_provider(model.provider)
        task = ResumeRecoveryTask(provider, model, self._config)
        return task.run(
            ResumeRecoveryInput(
                resume_id=resume_id,
                page_images=page_images or [],
                extraction_results=extraction_results or {},
                quality_report=quality_report or {},
            )
        )

    def recover_text(
        self,
        text_id: str,
        corrupted_text: str = "",
        reference_texts: dict[str, str] | None = None,
        quality_report: dict[str, Any] | None = None,
        *,
        preferred_model: str | None = None,
    ) -> TextRecoveryResult:
        """Recover text without page images (text-only path)."""
        self._ensure_enabled()
        model = self._router.select_model(
            task="text_recovery", preferred_model=preferred_model
        )
        provider = self._get_provider(model.provider)
        task = TextRecoveryTask(provider, model)
        return task.run(
            TextRecoveryInput(
                text_id=text_id,
                corrupted_text=corrupted_text,
                reference_texts=reference_texts or {},
                quality_report=quality_report or {},
            )
        )

    # -- internals --------------------------------------------------------------

    def _ensure_enabled(self) -> None:
        if not self._config.enabled:
            raise ConfigurationError(
                "The AI model module is disabled (AI_MODEL_ENABLED=false). "
                "Enable it before requesting recovery."
            )

    def _get_provider(self, provider_name: str) -> BaseProvider:
        if provider_name in self._providers:
            return self._providers[provider_name]
        if provider_name == OpenAIProvider.name:
            provider: BaseProvider = OpenAIProvider(
                self._config, client=self._openai_client
            )
        elif provider_name == LocalProvider.name:
            provider = LocalProvider(self._config)
        else:
            raise ConfigurationError(
                f"Unknown AI provider {provider_name!r}. Available "
                "providers: 'openai', 'local'."
            )
        self._providers[provider_name] = provider
        return provider
