"""OpenAI provider, built on the official ``openai`` Python SDK.

Notes:
- The API key comes from configuration (environment) and is never
  logged or included in error messages.
- The SDK's own retry layer is disabled (``max_retries=0``) so this
  module's policy is the only one: retry only transient errors
  (timeouts, rate limits, connection errors, 5xx), up to
  ``AI_MODEL_MAX_RETRIES`` times.
- Structured output is requested as a JSON object and then validated
  locally with Pydantic. Malformed JSON or schema violations raise
  :class:`InvalidModelOutputError` — a fabricated result is never
  substituted.
- A pre-built client may be injected (``client=``), which is how unit
  tests run with mocked responses and no real API key or network.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from ..config import AIModelConfig
from ..exceptions import (
    AIModelError,
    InvalidModelOutputError,
    ModelAPIError,
    ModelTimeoutError,
)
from ..utils.image_utils import PreparedImage
from .base_provider import (
    BaseProvider,
    HealthCheckResult,
    ProviderResponse,
    StructuredProviderResponse,
)

logger = logging.getLogger(__name__)


class OpenAIProvider(BaseProvider):
    name = "openai"

    def __init__(
        self,
        config: AIModelConfig | None = None,
        *,
        client: Any = None,
        retry_backoff_seconds: float = 0.5,
    ) -> None:
        self._config = config or AIModelConfig.from_env()
        self._retry_backoff_seconds = retry_backoff_seconds
        if client is not None:
            self._client = client
        else:
            api_key = self._config.require_openai_api_key()
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - dependency guard
                raise ModelAPIError(
                    "The 'openai' package is not installed. Install it with "
                    "`pip install openai` (see requirements.txt)."
                ) from exc
            self._client = OpenAI(
                api_key=api_key,
                timeout=self._config.timeout_seconds,
                max_retries=0,  # retries are handled here, once
            )

    # -- public interface ---------------------------------------------------

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
        messages = self._messages(system_prompt, prompt)
        response = self._create_completion(
            model_id=model_id, messages=messages, temperature=temperature,
            max_tokens=max_tokens,
        )
        return ProviderResponse(
            text=self._extract_text(response),
            model_name=model_name,
            model_id=model_id,
            provider=self.name,
            token_usage=self._extract_usage(response),
            finish_reason=self._extract_finish_reason(response),
            raw=response,
        )

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
        if images:
            user_content: Any = [{"type": "text", "text": prompt}] + [
                {
                    "type": "image_url",
                    "image_url": {"url": image.data_uri},
                }
                for image in images
            ]
        else:
            user_content = prompt
        messages = self._messages(system_prompt, user_content)
        response = self._create_completion(
            model_id=model_id,
            messages=messages,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        raw_text = self._extract_text(response)
        try:
            data = json.loads(raw_text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise InvalidModelOutputError(
                f"Model {model_name!r} returned non-JSON output for "
                f"structured request: {exc}"
            ) from exc
        try:
            parsed = response_schema.model_validate(data)
        except ValidationError as exc:
            raise InvalidModelOutputError(
                f"Model {model_name!r} returned JSON that does not match "
                f"{response_schema.__name__}: {exc}"
            ) from exc
        return StructuredProviderResponse(
            parsed=parsed,
            model_name=model_name,
            model_id=model_id,
            provider=self.name,
            token_usage=self._extract_usage(response),
            raw_text=raw_text,
            raw=response,
        )

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
        if not images:
            from ..exceptions import ImageProcessingError

            raise ImageProcessingError("analyze_images requires at least one image.")
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": image.data_uri}}
            for image in images
        ]
        messages = self._messages(system_prompt, content)
        response = self._create_completion(
            model_id=model_id, messages=messages, temperature=temperature
        )
        return ProviderResponse(
            text=self._extract_text(response),
            model_name=model_name,
            model_id=model_id,
            provider=self.name,
            token_usage=self._extract_usage(response),
            finish_reason=self._extract_finish_reason(response),
            raw=response,
        )

    def health_check(self) -> HealthCheckResult:
        try:
            self._client.models.list()
            return HealthCheckResult(provider=self.name, healthy=True, detail="models.list succeeded")
        except Exception as exc:  # health check must not raise
            return HealthCheckResult(
                provider=self.name,
                healthy=False,
                detail=f"{type(exc).__name__}: provider unreachable or rejected the request",
            )

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _messages(system_prompt: str | None, user_content: Any) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_content})
        return messages

    def _create_completion(self, *, model_id: str, **kwargs: Any) -> Any:
        # The module's internal name is `model_id`; the OpenAI API
        # calls the same parameter `model`.
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        kwargs["model"] = model_id
        return self._call_with_retry(
            lambda: self._client.chat.completions.create(**kwargs),
            description="chat.completions.create",
        )

    def _call_with_retry(self, fn: Callable[[], Any], *, description: str) -> Any:
        attempts = self._config.max_retries + 1
        last_error: ModelAPIError | None = None
        for attempt in range(1, attempts + 1):
            try:
                return fn()
            except AIModelError:
                raise
            except Exception as exc:
                classified = self._classify_error(exc, description)
                last_error = classified
                if not classified.retryable or attempt == attempts:
                    raise classified from exc
                delay = self._retry_backoff_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "OpenAI %s failed (attempt %d/%d, %s); retrying in %.1fs",
                    description, attempt, attempts, type(exc).__name__, delay,
                )
                if delay > 0:
                    time.sleep(delay)
        assert last_error is not None  # pragma: no cover
        raise last_error

    @staticmethod
    def _classify_error(exc: Exception, description: str) -> ModelAPIError:
        """Map SDK / transport errors onto module exceptions.

        Only the exception type and HTTP status are used — messages
        from the SDK can echo request details, so they are not copied
        verbatim into our errors.
        """
        try:
            import openai
        except ImportError:  # pragma: no cover
            openai = None  # type: ignore[assignment]

        if openai is not None:
            if isinstance(exc, openai.APITimeoutError):
                return ModelTimeoutError(
                    f"OpenAI {description} timed out after the configured "
                    f"timeout."
                )
            if isinstance(exc, openai.RateLimitError):
                return ModelAPIError(
                    f"OpenAI {description} was rate limited (429).",
                    status_code=429, retryable=True,
                )
            if isinstance(exc, openai.AuthenticationError):
                return ModelAPIError(
                    f"OpenAI {description} failed authentication (401). "
                    "Check OPENAI_API_KEY.",
                    status_code=401, retryable=False,
                )
            if isinstance(exc, openai.APIConnectionError):
                return ModelAPIError(
                    f"OpenAI {description} could not reach the API "
                    "(connection error).",
                    retryable=True,
                )
            if isinstance(exc, openai.APIStatusError):
                status = getattr(exc, "status_code", None)
                return ModelAPIError(
                    f"OpenAI {description} failed with HTTP status {status}.",
                    status_code=status,
                    retryable=bool(status and status >= 500),
                )
        return ModelAPIError(
            f"OpenAI {description} failed unexpectedly ({type(exc).__name__}).",
            retryable=False,
        )

    @staticmethod
    def _extract_text(response: Any) -> str:
        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise InvalidModelOutputError(
                "OpenAI response had no message content."
            ) from exc
        if content is None or not str(content).strip():
            raise InvalidModelOutputError("OpenAI response content was empty.")
        return str(content)

    @staticmethod
    def _extract_usage(response: Any) -> dict[str, int] | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        result: dict[str, int] = {}
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(usage, field, None)
            if value is not None:
                result[field] = int(value)
        return result or None

    @staticmethod
    def _extract_finish_reason(response: Any) -> str | None:
        try:
            return response.choices[0].finish_reason
        except (AttributeError, IndexError, TypeError):
            return None
