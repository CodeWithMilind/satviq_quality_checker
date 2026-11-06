"""LLM processing stage for the simple pipeline.

The LLM receives ONLY the normalized text of the selected extractor.
No other extractor texts are ever passed here.

Uses the existing MuseService (configurable system prompt + sir's
schema validation) when a service is supplied, or a plain callable
``llm_callable(text) -> outcome dict | JSON string | dict`` for tests
and alternative wiring. Retries happen ONLY for transient failures
(timeout / connection / 429 / 5xx), up to ``llm_max_retries`` after the
first attempt. Invalid JSON / schema validation failures are NOT
retried. Nothing is ever fabricated.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from .config import PipelineConfig

# The 16 instruction points the system prompt must encode (spec §6).
# The existing MuseService already applies the configurable Settings
# prompt which embeds the schema and the never-invent rules; this
# constant documents the required guidance for the service path and
# for anyone wiring a callable provider.
DEFAULT_SYSTEM_PROMPT_GUIDANCE = """\
System prompt guidance for resume extraction (16 points):
1. Read the complete resume text provided.
2. Extract all available candidate information.
3. Identify personal and contact details (name, email, phone, location, links).
4. Extract the professional summary, if present.
5. Extract skills (categorised where the schema requires it).
6. Extract work experience (employer, title, dates, highlights).
7. Extract education and qualifications.
8. Extract projects.
9. Extract certifications.
10. Extract achievements.
11. Extract languages and any other relevant sections.
12. Preserve dates, organisations, job titles, qualifications and numerical details exactly as written.
13. Never invent missing information.
14. Return null or empty arrays / empty strings for genuinely unavailable information, according to the JSON schema.
15. Return valid JSON only -- no prose, no markdown fences around the object.
16. Follow the defined output schema strictly.
"""

_TRANSIENT_ERROR_TYPES = {"TimeoutError", "MuseTimeoutError", "ConnectionError", "APIConnectionError", "Timeout", "ReadTimeout", "ConnectTimeout"}
_TRANSIENT_SUBSTRINGS = ("timeout", "timed out", "connection", "temporar", "429", "rate limit", "rate_limit")


def _is_transient(error_type: str | None, error_text: str | None) -> bool:
    if error_type and error_type in _TRANSIENT_ERROR_TYPES:
        return True
    text = (error_text or "").lower()
    if any(s in text for s in _TRANSIENT_SUBSTRINGS):
        return True
    # 5xx detection: look for standalone 500-599 codes in the message
    if error_text:
        for token in ("500", "501", "502", "503", "504", "505", "507", "529"):
            if token in error_text:
                return True
    if error_type and "timeout" in error_type.lower():
        return True
    if error_type and "connection" in error_type.lower():
        return True
    return False


def _validate_text_or_dict(raw: Any) -> tuple[dict | None, str | None, str | None]:
    """Validate a callable's raw return as Resume JSON.

    Returns (resume_json, validation, error)."""
    try:
        from muse_ai.schema import validate_resume_json
    except Exception as exc:
        return None, "invalid", f"Schema validator unavailable: {exc}"
    if isinstance(raw, dict):
        # dict resume payload -> serialise and validate
        try:
            text = json.dumps(raw)
        except Exception as exc:
            return None, "invalid", f"Could not serialise LLM dict output: {exc}"
        parsed, error = validate_resume_json(text)
        return parsed, ("valid" if parsed is not None else "invalid"), error
    if isinstance(raw, str):
        parsed, error = validate_resume_json(raw)
        return parsed, ("valid" if parsed is not None else "invalid"), error
    return None, "invalid", f"LLM returned unsupported type {type(raw).__name__}"


class LLMProcessor:
    """One LLM stage: normalized text -> validated Resume JSON."""

    def __init__(self, service: Any = None, config: PipelineConfig | None = None, llm_callable: Any = None, **kwargs: Any):
        # Accept alternate kw names for convenience
        if service is None and "llm_service" in kwargs:
            service = kwargs.pop("llm_service")
        if llm_callable is None and "callable" in kwargs:
            llm_callable = kwargs.pop("callable")
        self.config = config or PipelineConfig()
        self.llm_callable = llm_callable
        self.service = None
        # Decide service vs callable
        if llm_callable is not None:
            self.llm_callable = llm_callable
            self.service = service  # may be None
        elif service is not None:
            if hasattr(service, "generate_resume_json"):
                self.service = service
            elif callable(service):
                self.llm_callable = service
                self.service = None
            else:
                self.service = service

    # -- helpers ---------------------------------------------------------
    def _not_configured(self, provider: Any = None, model: Any = None) -> dict[str, Any]:
        return {
            "status": "not_configured",
            "provider": provider or "Muse AI",
            "model": model,
            "resume_json": None,
            "validation": None,
            "error": "Muse AI is not configured. Set MUSE_API_KEY in the backend .env file; no LLM call was made.",
            "error_type": "ConfigurationError",
            "attempts": 0,
            "duration_ms": None,
            "usage": {},
            "temperature": self.config.llm_temperature,
        }

    def _call_service_once(self, text: str) -> dict[str, Any]:
        return self.service.generate_resume_json(text)  # type: ignore[union-attr]

    def _call_callable_once(self, text: str) -> Any:
        fn = self.llm_callable
        timeout = float(self.config.llm_timeout_seconds) if self.config.llm_timeout_seconds else None
        if not timeout:
            return fn(text)
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(fn, text)
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            raise TimeoutError(f"LLM call timed out after {timeout:g}s")
        finally:
            executor.shutdown(wait=False)

    # -- main ------------------------------------------------------------
    def process(self, normalized_text: str) -> dict[str, Any]:
        cfg = self.config
        max_attempts = 1 + max(0, int(cfg.llm_max_retries))

        # No provider at all
        if self.service is None and self.llm_callable is None:
            return self._not_configured()

        # Service path: check configured flag without calling
        if self.service is not None and self.llm_callable is None:
            configured = getattr(self.service, "configured", None)
            if configured is False:
                model = getattr(self.service, "model", None)
                provider = getattr(self.service, "provider_name", None) or "Muse AI"
                result = self._not_configured(provider=provider, model=model)
                # Preserve the service's own configuration error if present
                cfg_err = getattr(self.service, "configuration_error", None)
                if cfg_err:
                    result["error"] = cfg_err
                return result

        attempts = 0
        total_duration_ms: float = 0.0
        last_error: str | None = None
        last_error_type: str | None = None
        last_provider: Any = None
        last_model: Any = None
        last_usage: dict[str, Any] = {}
        last_validation: str | None = None

        while attempts < max_attempts:
            attempts += 1
            started = time.perf_counter()
            try:
                if self.llm_callable is not None:
                    raw = self._call_callable_once(normalized_text)
                    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
                    total_duration_ms += elapsed_ms
                    # Outcome-dict form (like MuseService outcome)
                    if isinstance(raw, dict) and ("ok" in raw or "resume_json" in raw or "validation" in raw) and not any(k in raw for k in ("full_name", "personal_information")):
                        # Treat as outcome dict, unless it looks like resume data itself
                        # Heuristic: outcome dicts have ok/error/provider keys
                        if "ok" in raw or "error_type" in raw or "provider" in raw:
                            ok = bool(raw.get("ok"))
                            last_provider = raw.get("provider") or last_provider or "Muse AI"
                            last_model = raw.get("model") or last_model
                            last_usage = raw.get("usage") or {}
                            if raw.get("duration_ms") is not None:
                                # already counted? use service duration instead of elapsed for total accuracy: adjust
                                pass
                            if ok and raw.get("resume_json") is not None and raw.get("validation") in (None, "valid"):
                                return {
                                    "status": "success",
                                    "provider": last_provider,
                                    "model": last_model,
                                    "resume_json": raw.get("resume_json"),
                                    "validation": raw.get("validation") or "valid",
                                    "error": None,
                                    "error_type": None,
                                    "attempts": attempts,
                                    "duration_ms": total_duration_ms,
                                    "usage": last_usage,
                                    "temperature": cfg.llm_temperature,
                                }
                            # Failure outcome
                            last_error = raw.get("error") or "LLM processing failed."
                            last_error_type = raw.get("error_type")
                            last_validation = raw.get("validation")
                            # Invalid JSON / validation failures are NOT retried
                            if last_validation == "invalid" or last_error_type == "ValidationError":
                                break
                            if _is_transient(last_error_type, last_error) and attempts < max_attempts:
                                continue
                            break
                    # JSON string or resume dict form
                    parsed, validation, verror = _validate_text_or_dict(raw)
                    last_validation = validation
                    # provider/model for callable path
                    last_provider = last_provider or "Muse AI"
                    last_model = last_model
                    if parsed is not None:
                        return {
                            "status": "success",
                            "provider": last_provider,
                            "model": last_model,
                            "resume_json": parsed,
                            "validation": "valid",
                            "error": None,
                            "error_type": None,
                            "attempts": attempts,
                            "duration_ms": total_duration_ms,
                            "usage": last_usage,
                            "temperature": cfg.llm_temperature,
                        }
                    else:
                        last_error = f"JSON validation failed: {verror}" if verror else "LLM returned invalid JSON."
                        last_error_type = "ValidationError"
                        # Invalid JSON must NOT be retried
                        break
                else:
                    # Service path
                    outcome = self._call_service_once(normalized_text)
                    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
                    dur = outcome.get("duration_ms") if isinstance(outcome, dict) else None
                    total_duration_ms += float(dur) if dur is not None else elapsed_ms
                    if not isinstance(outcome, dict):
                        last_error = "LLM service returned a non-dict outcome."
                        last_error_type = "TypeError"
                        break
                    last_provider = outcome.get("provider") or getattr(self.service, "provider_name", None) or "Muse AI"
                    last_model = outcome.get("model") or getattr(self.service, "model", None)
                    last_usage = outcome.get("usage") or {}
                    last_validation = outcome.get("validation")
                    if outcome.get("ok") and outcome.get("resume_json") is not None:
                        return {
                            "status": "success",
                            "provider": last_provider,
                            "model": last_model,
                            "resume_json": outcome.get("resume_json"),
                            "validation": outcome.get("validation") or "valid",
                            "error": None,
                            "error_type": None,
                            "attempts": attempts,
                            "duration_ms": total_duration_ms,
                            "usage": last_usage,
                            "temperature": cfg.llm_temperature,
                        }
                    last_error = outcome.get("error") or "LLM processing failed."
                    last_error_type = outcome.get("error_type")
                    if outcome.get("error_type") == "ConfigurationError":
                        # Not configured via service outcome
                        return {
                            "status": "not_configured",
                            "provider": last_provider,
                            "model": last_model,
                            "resume_json": None,
                            "validation": last_validation,
                            "error": last_error,
                            "error_type": last_error_type,
                            "attempts": attempts,
                            "duration_ms": total_duration_ms,
                            "usage": last_usage,
                            "temperature": cfg.llm_temperature,
                        }
                    if last_validation == "invalid" or last_error_type == "ValidationError":
                        break
                    if _is_transient(last_error_type, last_error) and attempts < max_attempts:
                        continue
                    break
            except TimeoutError as exc:
                elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
                total_duration_ms += elapsed_ms
                last_error = str(exc)
                last_error_type = "TimeoutError"
                last_provider = last_provider or (getattr(self.service, "provider_name", None) if self.service else "Muse AI")
                last_model = last_model or (getattr(self.service, "model", None) if self.service else None)
                if attempts < max_attempts:
                    continue
                break
            except Exception as exc:
                elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
                total_duration_ms += elapsed_ms
                last_error = f"{type(exc).__name__}: {exc}"
                last_error_type = type(exc).__name__
                last_provider = last_provider or (getattr(self.service, "provider_name", None) if self.service else "Muse AI")
                last_model = last_model or (getattr(self.service, "model", None) if self.service else None)
                if _is_transient(last_error_type, last_error) and attempts < max_attempts:
                    continue
                break

        return {
            "status": "failed",
            "provider": last_provider or "Muse AI",
            "model": last_model,
            "resume_json": None,
            "validation": last_validation,
            "error": last_error or "LLM processing failed.",
            "error_type": last_error_type,
            "attempts": attempts,
            "duration_ms": total_duration_ms if total_duration_ms else None,
            "usage": last_usage,
            "temperature": cfg.llm_temperature,
        }
