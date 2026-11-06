"""Custom exceptions for the ``ai_model`` module.

Every error raised by this module derives from :class:`AIModelError`, so
callers can catch one base class while still distinguishing failure modes.

Retry policy lives on the exceptions themselves: an exception instance
carries a ``retryable`` flag. Only transient failures (timeouts, rate
limits, connection errors, 5xx responses) are retryable; configuration,
capability, validation and malformed-output errors are not.
"""

from __future__ import annotations


class AIModelError(Exception):
    """Base class for all errors raised by the ai_model module."""

    retryable: bool = False


class ConfigurationError(AIModelError):
    """The module is misconfigured (e.g. missing API key, bad env value,
    unknown provider, or the module is disabled). Not retryable."""


class ModelNotFoundError(AIModelError):
    """A requested model name is not present in the registry."""


class UnsupportedCapabilityError(AIModelError):
    """No enabled model satisfies the capabilities a task requires."""


class ModelAPIError(AIModelError):
    """A provider API call failed.

    ``status_code`` holds the HTTP status when one is known.
    ``retryable`` is True for transient failures (429, 5xx, connection
    errors) and False for permanent ones (auth, bad request).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class ModelTimeoutError(ModelAPIError):
    """A provider API call timed out. Always retryable."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message, status_code=status_code, retryable=True)


class ProviderNotAvailableError(ModelAPIError):
    """A provider is a placeholder / not yet implemented or unreachable
    in a way retrying cannot fix. Not retryable."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=None, retryable=False)


class InvalidModelOutputError(AIModelError):
    """The model returned output that is not valid JSON, does not match
    the required schema, or is unusable (e.g. empty recovered text).
    Not retryable — re-sending the same request would not fix the
    caller's prompt/schema problem and must never be masked by
    fabricating a result."""


class ImageProcessingError(AIModelError):
    """A resume page image is missing, corrupt, of an unsupported
    format, or could not be prepared for the provider."""
