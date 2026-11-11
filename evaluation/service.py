"""The Muse AI resume EVALUATION stage.

One evaluation = one Muse call (plus at most two retries on
transient errors) against a record the existing pipeline already
processed. The call uses ONLY the record's Selected Normalized
Text, the deterministic Quality Checker context and a compact
Resume JSON digest (see :mod:`evaluation.prompt`) — extraction is
never repeated and no other extractor's text is sent.

Every attempt — success or failure — is appended to the record's
analysis history via :mod:`evaluation.store`; a failure NEVER
overwrites the latest successful analysis. Token figures come from
the API's own usage report only (never estimated) and are stored on
the evaluation entry, labelled as evaluation usage — strictly
separate from the record's extraction ``token_usage``.

Muse Spark is a reasoning model: reasoning tokens count toward
the output budget and the API's default reasoning effort can spend
the whole budget before any visible output exists. The evaluation
call therefore always sends an explicit ``reasoning_effort``
(``SATVIQ_EVALUATION_REASONING_EFFORT``, default ``low``) and JSON
mode; :meth:`muse_ai.client.MuseClient.chat_json` drops those
optional parameters one at a time if the deployment rejects them
with HTTP 400. Like every Muse call in this project, the request
has NO timeout.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from muse_ai.client import MuseClient, MuseError, MuseTimeoutError
from muse_ai.config import MuseConfig, load_muse_config

from . import config as eval_config
from . import prompt as eval_prompt
from . import store as eval_store

_PROMPT_VERSION = eval_prompt.PROMPT_VERSION
from .schema import validate_evaluation

logger = logging.getLogger(__name__)

# One active evaluation per resume (in-process registry, mirroring
# the AI-generation guard in api/pipeline.py): a second concurrent
# request is refused as "already_running" instead of firing a
# duplicate Muse call.
_RUNNING_LOCK = threading.Lock()
_RUNNING: set[str] = set()

_MAX_TRANSIENT_RETRIES = 2


def analysis_in_progress(resume_id: str | None) -> bool:
    """True while an evaluation is running for this resume."""
    if not resume_id:
        return False
    with _RUNNING_LOCK:
        return resume_id in _RUNNING


def _try_acquire(resume_id: str) -> bool:
    with _RUNNING_LOCK:
        if resume_id in _RUNNING:
            return False
        _RUNNING.add(resume_id)
        return True


def _release(resume_id: str) -> None:
    with _RUNNING_LOCK:
        _RUNNING.discard(resume_id)


def _is_transient(exc: MuseError) -> bool:
    if isinstance(exc, MuseTimeoutError):
        return True
    if exc.status is not None and (exc.status == 429 or exc.status >= 500):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in
               ("timeout", "timed out", "connection", "temporar",
                "rate limit", "rate_limit"))


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _usage_tokens(usage: Any) -> tuple[dict[str, int | None], int | None]:
    """(tokens, reasoning_tokens) from an API usage block.

    Actual reported usage only: when the API reported nothing, all
    values are None — never estimated."""
    tokens: dict[str, int | None] = {"input": None, "output": None,
                                     "total": None}
    reasoning: int | None = None
    if not isinstance(usage, dict):
        return tokens, reasoning
    inp = usage.get("prompt_tokens")
    out = usage.get("completion_tokens")
    tot = usage.get("total_tokens")
    if _is_num(inp):
        tokens["input"] = int(inp)
    if _is_num(out):
        tokens["output"] = int(out)
    if _is_num(tot):
        tokens["total"] = int(tot)
    elif tokens["input"] is not None and tokens["output"] is not None:
        tokens["total"] = tokens["input"] + tokens["output"]
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict) and _is_num(details.get("reasoning_tokens")):
        reasoning = int(details["reasoning_tokens"])
    return tokens, reasoning


def _base_entry(record: dict[str, Any], model: str | None) -> dict[str, Any]:
    history = record.get("analyses")
    version = (len(history) if isinstance(history, list) else 0) + 1
    return {
        "analysis_id": str(uuid.uuid4()),
        "resume_id": record.get("resume_id"),
        "analysis_version": version,
        "status": "failed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "prompt_version": _PROMPT_VERSION,
        "yaml_version": eval_store.yaml_version(),
        "usage_type": "evaluation",
        "tokens": {"input": None, "output": None, "total": None},
        "api_calls": [],
        "reasoning_tokens": None,
        "duration_ms": None,
        "evaluation": None,
        "error": None,
    }


def analyze_record(record: dict[str, Any],
                   client: MuseClient | None = None,
                   config: MuseConfig | None = None,
                   ) -> tuple[dict[str, Any] | None, str | None]:
    """Evaluate one stored record with Muse AI.

    Returns ``(entry, error)``:

    - success: ``(entry, None)`` with ``entry["status"] == "success"``;
    - Muse/validation failure: ``(entry, entry["error"])`` — the
      failed entry IS persisted to the history, and the record's
      latest successful analysis is left untouched;
    - refused before any attempt: ``(None, "already_running")`` or
      ``(None, "evaluation_disabled")`` — nothing is persisted.
    """
    resume_id = record.get("resume_id")
    if not eval_config.evaluation_enabled():
        return None, "evaluation_disabled"
    if resume_id is not None and not _try_acquire(str(resume_id)):
        return None, "already_running"
    try:
        return _analyze_acquired(record, client, config)
    finally:
        if resume_id is not None:
            _release(str(resume_id))


def _analyze_acquired(record: dict[str, Any],
                      client: MuseClient | None,
                      config: MuseConfig | None,
                      ) -> tuple[dict[str, Any] | None, str | None]:
    muse_config = config or getattr(client, "config", None) \
        or load_muse_config()
    entry = _base_entry(record, muse_config.model)

    if client is None and not muse_config.configured:
        entry["error"] = (
            "Muse AI is not configured — "
            + (muse_config.configuration_error
               or "set MUSE_API_KEY in the backend .env file."))
        eval_store.append_analysis(record, entry)
        return entry, entry["error"]

    text = eval_prompt.selected_text(record)
    if not text.strip():
        entry["error"] = (
            "No extracted text is stored for this resume, so there "
            "is nothing to evaluate. Reprocess the resume first.")
        eval_store.append_analysis(record, entry)
        return entry, entry["error"]

    eval_cfg = eval_config.load_config()
    instructions, user_text = eval_prompt.build_prompt(record, eval_cfg)
    active_client = client or MuseClient(muse_config)
    extra = {
        "max_completion_tokens": eval_config.evaluation_max_output_tokens(),
        "reasoning_effort": eval_config.evaluation_reasoning_effort(),
        "response_format": {"type": "json_object"},
    }
    started = time.perf_counter()

    def _call(extra_payload: dict[str, Any]) -> dict[str, Any]:
        """One evaluation call with the transient-retry policy."""
        retries = 0
        while True:
            try:
                return active_client.chat_json(instructions, user_text,
                                               extra=extra_payload)
            except MuseError as exc:
                if retries < _MAX_TRANSIENT_RETRIES and _is_transient(exc):
                    retries += 1
                    logger.warning(
                        "Evaluation Muse call for resume_id=%s hit a "
                        "transient error (retry %d): %s",
                        record.get("resume_id"), retries, exc)
                    continue
                raise

    total_tokens: dict[str, int | None] = {"input": None, "output": None,
                                           "total": None}
    total_reasoning: int | None = None

    def _accumulate(result: dict[str, Any],
                    kind: str = "evaluation") -> None:
        nonlocal total_reasoning
        tokens, reasoning = _usage_tokens(result.get("usage"))
        for key in total_tokens:
            if tokens[key] is not None:
                total_tokens[key] = (total_tokens[key] or 0) + tokens[key]
        if reasoning is not None:
            total_reasoning = (total_reasoning or 0) + reasoning
        # Per-call capture for cost tracking (api/ai_costs.py): the
        # entry's aggregate tokens above are unchanged; this list
        # preserves each call's own provider-reported usage —
        # including the JSON-correction call — so retries and
        # multi-call evaluations are costed per call, never folded.
        usage = result.get("usage") or {}
        details = usage.get("prompt_tokens_details")
        cached = details.get("cached_tokens")             if isinstance(details, dict) else None
        call_tokens = dict(tokens)
        call_tokens["cached_input"] = int(cached)             if _is_num(cached) else None
        entry["api_calls"].append({
            "attempt": len(entry["api_calls"]) + 1,
            "kind": kind,
            "reasoning_tokens": reasoning,
            "status": "success",
            "model": result.get("model"),
            "request_id": result.get("request_id"),
            "duration_ms": result.get("duration_ms"),
            "tokens": call_tokens,
        })

    try:
        result = _call(extra)
    except MuseError as exc:
        entry["error"] = str(exc)
        entry["duration_ms"] = round(
            (time.perf_counter() - started) * 1000, 2)
        eval_store.append_analysis(record, entry)
        return entry, entry["error"]
    except Exception as exc:  # transport/programming errors
        logger.exception("Evaluation call failed unexpectedly")
        entry["error"] = f"{type(exc).__name__}: {exc}"
        entry["duration_ms"] = round(
            (time.perf_counter() - started) * 1000, 2)
        eval_store.append_analysis(record, entry)
        return entry, entry["error"]

    _accumulate(result)
    if result.get("model"):
        entry["model"] = result["model"]
    raw_text = result.get("text") or ""
    evaluation, error = validate_evaluation(raw_text, eval_cfg)
    recovery: dict[str, Any] = {"attempted": False}

    if evaluation is None:
        # ---- controlled recovery: ONE constrained JSON-correction
        # call. The broken response is sent back with an instruction
        # to repair its FORMAT only; the corrected output goes
        # through the same validation, and nothing is persisted
        # unless it fully validates.
        category, detail = _failure_diagnosis(result, error)
        recovery = {"attempted": True, "kind": "json_correction",
                    "first_error": error, "success": False}
        logger.warning(
            "Evaluation for resume_id=%s failed validation "
            "(category=%s, finish_reason=%s, response_chars=%d): %s",
            record.get("resume_id"), category,
            result.get("finish_reason"), len(raw_text), detail)
        from .schema import Evaluation as _EvalModel
        correction_instructions = (
            "You repair a malformed resume-evaluation response. "
            "The text you are given failed JSON validation "
            f"({detail}). Return ONLY one valid JSON object "
            "matching this schema:\n"
            + json.dumps(_EvalModel.model_json_schema(),
                         separators=(",", ":"))
            + "\nRules: (1) PRESERVE every value present in the "
            "given text exactly — repair JSON syntax (fences, stray "
            "prose, truncation damage, escaping) and null/mistyped "
            "fields only; never change a score, skill, evidence or "
            "statement that is present. (2) The given text may be "
            "TRUNCATED — the schema's later sections (up to and "
            "including recruiter_assessment) may be missing "
            "entirely. Complete ONLY those missing sections from "
            "the RESUME TEXT provided with this request, under the "
            "original evidence rules: base every statement on the "
            "resume text, never invent; a limitation or verified "
            "skill gap requires resume evidence — mere absence "
            "belongs in unmentioned_areas. (3) Anything supported "
            "by neither text stays empty. No commentary.")
        correction_extra = {
            "max_completion_tokens": eval_config.evaluation_max_output_tokens(),
            "reasoning_effort": "minimal",
            "response_format": {"type": "json_object"},
        }
        try:
            corrected = active_client.chat_json(
                correction_instructions,
                "Repair this evaluation response into valid JSON "
                "only:\n" + raw_text[:12000]
                + "\n\nRESUME TEXT (use ONLY to complete sections "
                  "lost to truncation):\n" + text,
                extra=correction_extra)
            _accumulate(corrected, kind="json_correction")
            evaluation, error = validate_evaluation(
                corrected.get("text") or "", eval_cfg)
            recovery["success"] = evaluation is not None
            if evaluation is None:
                recovery["second_error"] = error
                result = corrected
        except MuseError as exc:
            recovery["correction_error"] = str(exc)
        entry["recovery"] = recovery

    entry["tokens"] = total_tokens
    entry["reasoning_tokens"] = total_reasoning
    entry["duration_ms"] = result.get("duration_ms")
    if entry["duration_ms"] is None:
        entry["duration_ms"] = round(
            (time.perf_counter() - started) * 1000, 2)
    if result.get("model"):
        entry["model"] = result["model"]

    if evaluation is None:
        category, detail = _failure_diagnosis(result, error)
        entry["diagnostics"] = {
            "failure_category": category,
            "failure_detail": detail,
            "finish_reason": result.get("finish_reason"),
            "response_chars": len(result.get("text") or ""),
            "model": entry["model"],
        }
        logger.warning(
            "Evaluation for resume_id=%s FAILED (category=%s, "
            "model=%s, finish_reason=%s, tokens=%s, "
            "response_chars=%d): %s",
            record.get("resume_id"), category, entry["model"],
            result.get("finish_reason"), total_tokens,
            entry["diagnostics"]["response_chars"], detail)
        entry["error"] = f"Evaluation response failed validation: {error}"
        eval_store.append_analysis(record, entry)
        return entry, entry["error"]

    # Deterministic content-evidence reconciliation: the Quality
    # Checker detects HEADINGS, so duties/outcomes documented
    # inside Work Experience were reported "missing". The resume
    # TEXT is the authority — correct the verdicts before storing.
    from .content_evidence import (
        reconcile_content_sections, reconcile_section_classification)

    entry["content_evidence"] = reconcile_content_sections(
        evaluation, text)
    classification_actions = reconcile_section_classification(
        evaluation)
    if classification_actions:
        entry["content_evidence"]["section_classification"] = \
            classification_actions
    entry["diagnostics"] = {
        "finish_reason": result.get("finish_reason"),
        "response_chars": len(raw_text),
        "model": entry["model"],
        "recovered_via_correction": bool(recovery.get("success")),
    }
    if recovery.get("attempted"):
        entry["recovery"] = recovery
    entry["status"] = "success"
    entry["evaluation"] = evaluation
    if evaluation.get("recruiter_assessment") is None:
        # A eval-prompt/2.0 evaluation should always carry the
        # recruiter assessment; record the loss explicitly instead
        # of letting it pass silently (historical entries predate
        # the field and are unaffected — this note is informational
        # only and changes nothing about the stored evaluation).
        entry.setdefault("diagnostics", {})[
            "recruiter_assessment"] = (
            "missing_after_json_correction"
            if recovery.get("attempted") else "missing_from_response")
    entry["error"] = None
    eval_store.append_analysis(record, entry)
    logger.info("Evaluation %s for resume_id=%s completed "
                "(completeness=%s, quality=%s)",
                entry["analysis_version"], record.get("resume_id"),
                evaluation["completeness"]["score"],
                evaluation["quality"]["score"])
    return entry, None


def _failure_diagnosis(result: dict[str, Any],
                       error: str | None) -> tuple[str, str]:
    """(category, detail) for a failed validation — from the error
    prefix validate_evaluation assigns, enriched with the response
    metadata (finish reason, reasoning share) for empty responses."""
    from .schema import diagnose_failure

    tokens, reasoning = _usage_tokens(result.get("usage"))
    raw = result.get("text") or ""
    if error and ":" in error and not raw.strip():
        category, detail = diagnose_failure(
            raw, result.get("finish_reason"), reasoning,
            tokens.get("output"))
        return category, detail
    if error and ":" in error:
        category, _, detail = error.partition(": ")
        return category.strip(), detail or error
    category, detail = diagnose_failure(
        raw, result.get("finish_reason"), reasoning, tokens.get("output"))
    return category, detail
