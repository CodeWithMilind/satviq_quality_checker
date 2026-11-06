"""Environment-based configuration for the ``ai_model`` module.

Configuration is read from environment variables only. API keys are
never hardcoded, never logged, and never shown in ``repr`` output.

Supported variables::

    OPENAI_API_KEY      OpenAI API key (required when provider=openai)
    OPENAI_MODEL        Default OpenAI model name (default: gpt-4o-mini)
    AI_PROVIDER         Provider name (default: openai)
    AI_MODEL_TIMEOUT    Per-request timeout in seconds (default: 60)
    AI_MODEL_MAX_RETRIES  Retries for transient failures (default: 2)
    AI_MODEL_ENABLED    Master switch (default: true)

Example::

    config = AIModelConfig.from_env()
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field, field_validator

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def _parse_bool(raw: str, *, variable: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError(
        f"{variable} must be a boolean value "
        f"(one of {sorted(_TRUE_VALUES | _FALSE_VALUES)}), got {raw!r}"
    )


class AIModelConfig(BaseModel):
    """Validated configuration for the AI model module."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(default="openai", description="Provider name, e.g. 'openai' or 'local'.")
    openai_api_key: str | None = Field(default=None, repr=False, description="OpenAI API key. Never log this.")
    default_model: str = Field(default="gpt-4o-mini", description="Registry name of the preferred model.")
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    max_retries: int = Field(default=2, ge=0, le=5)
    enabled: bool = Field(default=True)

    # Image preparation limits (not env-driven in v1; sane defaults).
    max_image_dimension: int = Field(default=2000, ge=256, le=8000)
    max_image_bytes: int = Field(default=4_000_000, ge=50_000)

    # Local provider placeholder settings.
    local_base_url: str = Field(default="http://localhost:11434")

    @field_validator("provider", "default_model")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be empty")
        return value

    @field_validator("provider")
    @classmethod
    def _lower_provider(cls, value: str) -> str:
        return value.lower()

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "AIModelConfig":
        """Build a config from environment variables.

        Raises ``pydantic.ValidationError`` for malformed values
        (e.g. a non-numeric timeout). A *missing* OpenAI key does not
        fail here — it fails with :class:`ConfigurationError` only when
        the OpenAI provider is actually used, so tests and non-AI code
        paths can construct a config freely.
        """
        env = os.environ if environ is None else environ
        values: dict[str, object] = {}

        if env.get("OPENAI_API_KEY"):
            values["openai_api_key"] = env["OPENAI_API_KEY"]
        if env.get("OPENAI_MODEL"):
            values["default_model"] = env["OPENAI_MODEL"]
        if env.get("AI_PROVIDER"):
            values["provider"] = env["AI_PROVIDER"]
        if env.get("AI_MODEL_TIMEOUT"):
            values["timeout_seconds"] = float(env["AI_MODEL_TIMEOUT"])
        if env.get("AI_MODEL_MAX_RETRIES"):
            values["max_retries"] = int(env["AI_MODEL_MAX_RETRIES"])
        if env.get("AI_MODEL_ENABLED"):
            values["enabled"] = _parse_bool(env["AI_MODEL_ENABLED"], variable="AI_MODEL_ENABLED")
        if env.get("AI_LOCAL_BASE_URL"):
            values["local_base_url"] = env["AI_LOCAL_BASE_URL"]

        return cls(**values)

    def require_openai_api_key(self) -> str:
        """Return the OpenAI API key or raise ConfigurationError.

        The key value itself is never included in the error message.
        """
        from .exceptions import ConfigurationError

        if not self.openai_api_key:
            raise ConfigurationError(
                "OPENAI_API_KEY is not set. Set it in the environment "
                "before using the OpenAI provider."
            )
        return self.openai_api_key
