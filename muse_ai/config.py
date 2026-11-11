"""Backend-only Muse AI configuration, loaded from ``.env``.

Muse AI (Meta Model API) is the project's ONLY LLM provider, and
it is configured exclusively through backend environment variables —
never through the frontend, never hardcoded:

    MUSE_API_KEY=            (required; secret)
    MUSE_BASE_URL=https://api.meta.ai/v1
    MUSE_MODEL=muse-spark-1.3
    MUSE_MAX_OUTPUT_TOKENS=  (optional)
    MUSE_TEMPERATURE=        (optional)
    MUSE_VISION_ENABLED=true

Verified against the official documentation
(https://dev.meta.ai/docs/overview): base URL ``https://api.meta.ai/v1``,
standard model ``muse-spark-1.3``, Bearer-token auth, Chat
Completions surface. A local ``.env`` file at the project root is
loaded at startup; real environment variables take precedence.
The API key is never exposed: :meth:`MuseConfig.public_status`
contains no credential material.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROVIDER_NAME = "Muse AI"
DEFAULT_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.3"

# Documented inline-file limit for the Model API (file handling docs).
INLINE_FILE_LIMIT_BYTES = 50_000_000

# Documented context windows (dev.meta.ai Models / overview pages:
# Muse Spark and Muse Wonder both document a 1,048,576-token
# context). Used for token-usage monitoring only; an explicit
# MUSE_CONTEXT_WINDOW_TOKENS in .env overrides these.
DOCUMENTED_CONTEXT_WINDOWS = {
    "muse-spark-1.3": 1_048_576,
    "muse-spark-1.2": 1_048_576,
    "muse-spark-1.1": 1_048_576,
    "muse-wonder-1.0": 1_048_576,
}


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_env_file(path: str | Path | None = None) -> Path | None:
    """Load ``KEY=VALUE`` pairs from a .env file into os.environ.

    Existing environment variables are NOT overridden. Returns the
    file used, or None when no .env file exists.
    """
    candidate = (Path(path) if path else project_root() / ".env")
    if not candidate.is_file():
        return None
    for line in candidate.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    return candidate


@dataclass
class MuseConfig:
    """Validated Muse settings. ``configured`` is False (with the
    reasons in ``errors``) when the API key is missing or a value is
    invalid — the rest of the application keeps running and AI
    stages report the configuration error instead of guessing."""

    api_key: str | None = None
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    # Timeouts are REMOVED: Muse requests are made with no timeout
    # (None), so a slow model response is waited out instead of
    # being cut off. The field is kept (always None unless a caller
    # constructs a config explicitly) so outcome records keep their
    # "timeout_seconds" key; MUSE_TIMEOUT_SECONDS is no longer read.
    timeout_seconds: float | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    vision_enabled: bool = True
    context_window_tokens: int | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and not self.errors

    @property
    def configuration_error(self) -> str | None:
        return "; ".join(self.errors) if self.errors else None

    def public_status(self) -> dict:
        """Non-secret status for /api/health and the UI status line."""
        return {
            "provider": PROVIDER_NAME,
            "model": self.model,
            "base_url": self.base_url,
            "configured": self.configured,
            "vision_enabled": self.vision_enabled,
            "timeout_seconds": self.timeout_seconds,
            "context_window_tokens": self.context_window_tokens,
            "error": self.configuration_error,
        }


def load_muse_config(env: dict | None = None) -> MuseConfig:
    """Build a MuseConfig from a mapping (default: os.environ,
    after loading the project .env file)."""
    if env is None:
        load_env_file(os.environ.get("SATVIQ_ENV_FILE"))
        env = dict(os.environ)
    config = MuseConfig()
    config.api_key = (env.get("MUSE_API_KEY") or "").strip() or None
    if not config.api_key:
        config.errors.append(
            "MUSE_API_KEY is not set — add it to the backend .env "
            "file (see .env.example).")
    config.base_url = (env.get("MUSE_BASE_URL")
                       or DEFAULT_BASE_URL).strip().rstrip("/") \
        or DEFAULT_BASE_URL
    config.model = (env.get("MUSE_MODEL") or DEFAULT_MODEL).strip() \
        or DEFAULT_MODEL

    # MUSE_TIMEOUT_SECONDS is intentionally NOT read: request
    # timeouts were removed, so config.timeout_seconds stays None
    # and the client waits for the API however long it takes.

    raw_tokens = (env.get("MUSE_MAX_OUTPUT_TOKENS") or "").strip()
    if raw_tokens:
        try:
            config.max_output_tokens = int(raw_tokens)
            if config.max_output_tokens <= 0:
                raise ValueError
        except ValueError:
            config.errors.append(
                f"MUSE_MAX_OUTPUT_TOKENS must be a positive integer, "
                f"got {raw_tokens!r}.")
            config.max_output_tokens = None

    raw_temp = (env.get("MUSE_TEMPERATURE") or "").strip()
    if raw_temp:
        try:
            config.temperature = float(raw_temp)
            if not 0.0 <= config.temperature <= 2.0:
                raise ValueError
        except ValueError:
            config.errors.append(
                f"MUSE_TEMPERATURE must be a number between 0 and 2, "
                f"got {raw_temp!r}.")
            config.temperature = None

    raw_vision = (env.get("MUSE_VISION_ENABLED") or "").strip().lower()
    if raw_vision:
        config.vision_enabled = raw_vision not in (
            "false", "0", "no", "off")

    raw_window = (env.get("MUSE_CONTEXT_WINDOW_TOKENS") or "").strip()
    if raw_window:
        try:
            config.context_window_tokens = int(raw_window)
            if config.context_window_tokens <= 0:
                raise ValueError
        except ValueError:
            config.errors.append(
                f"MUSE_CONTEXT_WINDOW_TOKENS must be a positive "
                f"integer, got {raw_window!r}.")
            config.context_window_tokens = None
    if config.context_window_tokens is None:
        config.context_window_tokens = DOCUMENTED_CONTEXT_WINDOWS.get(
            config.model)
    return config
