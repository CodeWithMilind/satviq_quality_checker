"""Extraction Manager — sequential fallback across the extractors.

Responsibilities:
- Load configuration (priority order, enabled/disabled, acceptance
  thresholds) from ``config/extractor.yaml``, with programmatic
  overrides.
- Run extractors sequentially, capture failures without crashing,
  continue to the next extractor, and return the first *accepted*
  result.
- Record every attempt, and the failure / rejection reason for each
  unsuccessful one, on the returned result (``result.attempts``).
  Errors are never silently discarded.
- Prevent duplicate attempts, respect ``max_attempts``, and honour
  the configurable total extraction safety budget
  (``max_total_timeout_seconds``) when one is configured.

Acceptance is decided by a policy object — NOT by exceptions alone,
and NOT hardcoded inside the extractors. The default is
:class:`BasicAcceptancePolicy` (extraction succeeded + meaningful text
is available).

Future Quality Checker integration (extension point)
----------------------------------------------------
Supply any object with ``accept(result: ExtractionResult) -> bool``
(as ``acceptance_policy=...``) or ``describe_rejection(result) -> str``.
A future policy can internally do exactly::

    report = quality_checker.check(result.text, ...)
    return quality_policy_accepts(report)

The manager does not import, recreate, or depend on the Quality
Checker in any way today.
"""

from __future__ import annotations

import copy
import importlib.metadata
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import yaml  # pyright: ignore[reportMissingModuleSource]

from .base import BaseExtractor
from .exceptions import InvalidFileError
from .extractors import (
    DoclingExtractor,
    OCRExtractor,
    PyMuPDFExtractor,
    TikaExtractor,
)
from .models import AttemptRecord, ExtractionResult

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "extractor.yaml"

_EXTRACTOR_CLASSES: dict[str, type[BaseExtractor]] = {
    "tika": TikaExtractor,
    "pymupdf": PyMuPDFExtractor,
    "docling": DoclingExtractor,
    "ocr": OCRExtractor,
}


class AcceptancePolicy(Protocol):
    """Protocol any acceptance policy (incl. a future QC policy) follows.

    Optional extension: a policy may also define
    ``best_candidate_result() -> ExtractionResult | None``. When no
    attempt was accepted outright, the manager calls it once and, if
    it returns a successful result, returns that result (with the full
    attempt history attached) instead of an all-failed result. This is
    how a quality policy can hold non-failing-but-imperfect extractions
    as candidates and nominate the best one at the end.
    """

    def accept(self, result: ExtractionResult) -> bool: ...

    def describe_rejection(self, result: ExtractionResult) -> str: ...


class BasicAcceptancePolicy:
    """Default policy: success + meaningful text availability.

    This is deliberately a *basic* policy. It knows nothing about
    resume quality — that is the future Quality Checker's job.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self.min_text_chars: int = int(config.get("min_text_chars", 50))
        self.min_word_count: int = int(config.get("min_word_count", 10))
        self.require_success: bool = bool(config.get("require_success", True))

    def accept(self, result: ExtractionResult) -> bool:
        if self.require_success and not result.success:
            return False
        if len(result.text.strip()) < self.min_text_chars:
            return False
        if result.word_count < self.min_word_count:
            return False
        return True

    def describe_rejection(self, result: ExtractionResult) -> str:
        if self.require_success and not result.success:
            return result.error or "Extraction failed."
        return (
            "Rejected by basic acceptance policy: extracted text is not "
            f"meaningful enough ({len(result.text.strip())} chars < "
            f"{self.min_text_chars} minimum, {result.word_count} words < "
            f"{self.min_word_count} minimum)."
        )


#: Distribution names used to report a version for extractors that do
#: not carry one themselves (Tika reports its own, from the JAR name).
_DIST_NAMES = {"pymupdf": "PyMuPDF", "docling": "docling", "ocr": "pytesseract"}


def _extractor_version(extractor: BaseExtractor, name: str) -> str | None:
    version = getattr(extractor, "version", None)
    if version:
        return str(version)
    dist = _DIST_NAMES.get(name)
    if dist:
        try:
            return importlib.metadata.version(dist)
        except Exception:
            return None
    return None


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_manager_config(config: dict[str, Any] | None = None,
                        config_path: str | Path | None = None) -> dict[str, Any]:
    """Load YAML config and apply programmatic overrides on top."""
    path = Path(config_path) if config_path is not None else _CONFIG_PATH
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        if config is None:
            logger.error("Extractor configuration not found: %s", path)
            raise
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Configuration in {path} must be a YAML mapping.")
    return _deep_merge(loaded, config or {})


class ExtractionManager:
    """Run extractors in configured order until one is accepted."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        config_path: str | Path | None = None,
        extractors: dict[str, BaseExtractor] | None = None,
        acceptance_policy: AcceptancePolicy | None = None,
    ) -> None:
        self.config = load_manager_config(config=config, config_path=config_path)
        manager_cfg = self.config.get("manager", {})
        self.order: list[str] = list(manager_cfg.get("order", list(_EXTRACTOR_CLASSES)))
        self.max_attempts: int = int(manager_cfg.get("max_attempts", len(self.order)))
        # Extraction-only total safety budget (seconds); None (the
        # default when unconfigured) means no budget. Configured
        # generously in extractor.yaml as a stuck-process guard.
        _budget = manager_cfg.get("max_total_timeout_seconds")
        self.max_total_timeout_seconds: float | None = \
            float(_budget) if _budget is not None else None
        self.policy: AcceptancePolicy = acceptance_policy or BasicAcceptancePolicy(
            self.config.get("acceptance", {})
        )
        per_extractor: dict[str, Any] = self.config.get("extractors", {})
        self._enabled: dict[str, bool] = {
            name: bool(per_extractor.get(name, {}).get("enabled", True))
            for name in _EXTRACTOR_CLASSES
        }
        if extractors is not None:
            # Injected instances (tests, custom extractors) replace the
            # defaults wholesale; enabled/order still come from config
            # unless the caller overrides them there too.
            self.extractors: dict[str, BaseExtractor] = dict(extractors)
            for name in self.extractors:
                self._enabled.setdefault(name, True)
        else:
            self.extractors = {
                name: cls(config=per_extractor.get(name, {}))
                for name, cls in _EXTRACTOR_CLASSES.items()
            }
    # -- configuration helpers ------------------------------------------
    def is_enabled(self, name: str) -> bool:
        return self._enabled.get(name, False)

    # -- main entry point -------------------------------------------------
    def extract(self, file_path: str | Path) -> ExtractionResult:
        started = time.perf_counter()
        path = Path(file_path) if file_path is not None else None
        logger.info("Extraction requested: file=%s", path.name if path else None)

        if path is None or not path.exists() or not path.is_file():
            message = str(InvalidFileError(f"File not found or not a file: {file_path}"))
            logger.error("Extraction aborted: %s", message)
            return ExtractionResult(
                extractor_name="manager", success=False, text="",
                file_type=path.suffix.lower().lstrip(".") if path and path.suffix else None,
                error=message, attempts=[],
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )

        attempts: list[AttemptRecord] = []
        attempted_names: set[str] = set()
        last_result: ExtractionResult | None = None
        failure_reasons: list[str] = []

        # Deduplicate the configured order while preserving it.
        ordered_names = list(dict.fromkeys(self.order))

        for name in ordered_names:
            if len(attempted_names) >= self.max_attempts:
                logger.info("Max attempts (%d) reached; stopping fallback.", self.max_attempts)
                break
            if self.max_total_timeout_seconds is not None:
                elapsed = time.perf_counter() - started
                if elapsed >= self.max_total_timeout_seconds:
                    reason = (f"Total extraction timeout of "
                              f"{self.max_total_timeout_seconds:.0f}s exceeded before "
                              f"trying '{name}'.")
                    logger.error(reason)
                    failure_reasons.append(reason)
                    break
            if name in attempted_names:
                logger.debug("Skipping duplicate attempt for extractor '%s'.", name)
                continue
            extractor = self.extractors.get(name)
            if extractor is None:
                reason = f"Configured extractor '{name}' is not registered."
                logger.error(reason)
                failure_reasons.append(reason)
                attempts.append(AttemptRecord(
                    extractor_name=name, success=False, error=reason,
                    started_at=_utcnow_iso(), completed_at=_utcnow_iso()))
                attempted_names.add(name)
                continue
            if not self.is_enabled(name):
                logger.info("Extractor '%s' is disabled; skipping.", name)
                continue

            attempted_names.add(name)
            if attempts:
                logger.info("Fallback initiated: trying extractor '%s' after %d attempt(s).",
                            name, len(attempts))
            logger.info("Extractor started: %s (file=%s)", name, path.name)
            attempt_started_at = _utcnow_iso()
            try:
                result = extractor.extract(path)
            except Exception as exc:  # an extractor must never crash the pipeline
                logger.exception("Extractor '%s' raised unexpectedly", name)
                result = ExtractionResult(
                    extractor_name=name, success=False, text="",
                    file_type=path.suffix.lower().lstrip(".") or None,
                    error=f"Extractor raised {type(exc).__name__}: {exc}",
                    error_type=type(exc).__name__,
                )
            attempt_completed_at = _utcnow_iso()
            version = _extractor_version(extractor, name)

            last_result = result
            if self.policy.accept(result):
                logger.info("Extractor completed and accepted: %s (chars=%d)",
                            name, len(result.text))
                attempts.append(AttemptRecord(
                    extractor_name=name, success=True, accepted=True,
                    duration_ms=result.duration_ms,
                    text=result.text, extractor_version=version,
                    started_at=attempt_started_at,
                    completed_at=attempt_completed_at,
                    metadata=result.metadata,
                ))
                result.attempts = attempts
                if result.duration_ms == 0.0:
                    result.duration_ms = round((time.perf_counter() - started) * 1000, 2)
                logger.info("Final extraction result: extractor=%s success=True attempts=%d",
                            name, len(attempts))
                return result

            reason = (result.error if not result.success
                      else self.policy.describe_rejection(result))
            logger.warning("Extractor '%s' not accepted: %s", name, reason)
            failure_reasons.append(f"{name}: {reason}")
            attempts.append(AttemptRecord(
                extractor_name=name, success=result.success, accepted=False,
                error=reason, duration_ms=result.duration_ms,
                # The attempt's own output is preserved even when the
                # policy rejected it — a later extractor never
                # overwrites an earlier tool's text.
                text=result.text, extractor_version=version,
                started_at=attempt_started_at,
                completed_at=attempt_completed_at,
                error_type=result.error_type,
                error_traceback=result.error_traceback,
                metadata=result.metadata,
            ))

        # Optional policy extension point: a policy may keep *candidate*
        # results during the loop (accept() returning False does not
        # have to mean "worthless" — e.g. a Quality-Checker policy that
        # holds a WARNING extraction as a candidate while the next
        # extractor is tried) and nominate the best one once every
        # extractor has been tried, via an optional
        # ``best_candidate_result()`` method. The manager knows nothing
        # about candidates or quality; it only asks the policy.
        # Policies without that method (e.g. BasicAcceptancePolicy)
        # keep the original all-failed behaviour below.
        best_candidate = getattr(self.policy, "best_candidate_result", None)
        if callable(best_candidate):
            candidate = best_candidate()
            if candidate is not None and candidate.success:
                candidate.attempts = attempts
                if candidate.duration_ms == 0.0:
                    candidate.duration_ms = round(
                        (time.perf_counter() - started) * 1000, 2)
                logger.info(
                    "Final extraction result: extractor=%s success=True "
                    "attempts=%d (best candidate nominated by the "
                    "acceptance policy)", candidate.extractor_name,
                    len(attempts))
                return candidate

        # Every extractor failed or was rejected — report all reasons.
        summary = "All extractors failed or were rejected. " + \
            (" | ".join(failure_reasons) if failure_reasons
             else "No extractors were attempted (check order / enabled configuration).")
        logger.error("Final extraction result: success=False. %s", summary)
        final = ExtractionResult(
            extractor_name=last_result.extractor_name if last_result else "manager",
            success=False,
            text=last_result.text if last_result else "",
            file_type=path.suffix.lower().lstrip(".") or None,
            metadata=last_result.metadata if last_result else {},
            page_count=last_result.page_count if last_result else None,
            page_texts=last_result.page_texts if last_result else [],
            warnings=(last_result.warnings if last_result else []) +
                     [f"Attempt failed — {r}" for r in failure_reasons],
            error=summary,
            attempts=attempts,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return final


def extract_document(file_path: str | Path, **manager_kwargs: Any) -> ExtractionResult:
    """One-call convenience wrapper around :class:`ExtractionManager`."""
    return ExtractionManager(**manager_kwargs).extract(file_path)
