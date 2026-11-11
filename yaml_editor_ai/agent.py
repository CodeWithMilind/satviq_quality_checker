"""YAML Editor AI agent — trigger, one low-token Muse call, update.

Execution contract (from the module spec):
- runs ONLY as a post-processing step after the final extractor has
  been selected — never during extractor fallbacks;
- runs ONLY when the selected attempt's completeness score is low,
  the resume has usable extracted text, and this resume has not
  already been analysed for the current YAML version;
- ONE Muse call with ONE compact instruction (prompt_builder);
- the LLM NEVER writes YAML: its JSON goes through the deterministic
  validator (yaml_manager.validate_suggestions) and only genuinely
  new, in-text keywords are appended, versioned and changelogged
  (version_manager.apply_update) under a lock with atomic writes;
- the current resume's stored quality score is NEVER modified — new
  keywords apply to future evaluations only;
- every failure is logged + recorded and leaves the active YAML
  byte-identical;
- credentials are independent: YAML_EDITOR_API_KEY (falling back to
  MUSE_API_KEY), YAML_EDITOR_MODEL (default
  muse-spark-1.3-contributor), YAML_EDITOR_BASE_URL,
  YAML_EDITOR_ENABLED=false disables the agent.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

import yaml

from . import prompt_builder, version_manager
from .token_tracker import TokenTracker
from .yaml_manager import YamlStore, build_updated_config, project_root, \
    validate_suggestions

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

#: Process-wide lock serialising YAML updates across ALL agent
#: instances in this process (each instance also holds its own
#: lock; this one is what makes concurrent resumes safe when the
#: pipeline builds more than one agent).
_UPDATE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class EditorConfig:
    """Editor settings: config.yaml defaults + YAML_EDITOR_* env."""

    def __init__(self, root: Path | None = None, env: dict | None = None,
                 file_config: dict | None = None):
        if file_config is None:
            try:
                file_config = yaml.safe_load(
                    _CONFIG_PATH.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                file_config = {}
        cfg = file_config
        if env is None:
            try:
                from muse_ai.config import load_env_file
                load_env_file()
            except Exception:  # pragma: no cover - .env is optional
                pass
            env = dict(os.environ)
        self.root = Path(root) if root else project_root()
        if file_config.get("_store_root"):
            self.root = Path(file_config["_store_root"])
        enabled_env = (env.get("YAML_EDITOR_ENABLED") or "").strip().lower()
        self.enabled = bool(cfg.get("enabled", True)) \
            and enabled_env not in ("0", "false", "no", "off")
        self.api_key = (env.get("YAML_EDITOR_API_KEY")
                        or env.get("MUSE_API_KEY") or "").strip() or None
        self.base_url = (env.get("YAML_EDITOR_BASE_URL")
                         or cfg.get("default_base_url")
                         or "https://api.meta.ai/v1").strip().rstrip("/")
        self.model = (env.get("YAML_EDITOR_MODEL")
                      or cfg.get("default_model")
                      or "muse-spark-1.3-contributor").strip()
        self.provider = cfg.get("provider") or "Muse AI"
        self.timeout_seconds = float(cfg.get("timeout_seconds") or 60)
        self.max_output_tokens = int(cfg.get("max_output_tokens") or 1000)
        self.temperature = float(cfg.get("temperature") or 0.0)
        # Reasoning depth for the editor's calls (see config.yaml):
        # "minimal" by default — the documented shortest pass. An
        # explicit empty/invalid value disables the parameter.
        _effort = str(cfg.get("reasoning_effort") or "minimal").strip().lower()
        self.reasoning_effort = _effort if _effort in (
            "minimal", "low", "medium", "high", "xhigh", "max") else None
        self.trigger_below = float(
            cfg.get("trigger_completeness_below") or 75.0)
        self.min_text_chars = int(cfg.get("min_text_chars") or 200)
        self.max_resume_chars = int(cfg.get("max_resume_chars") or 6000)
        self.max_keyword_chars = int(cfg.get("max_keyword_chars") or 60)
        self.max_per_category = int(cfg.get("max_keywords_per_category") or 5)
        self.max_total = int(cfg.get("max_keywords_total") or 12)
        yaml_dir = cfg.get("yaml_dir") or "yaml files"
        store_root = Path(yaml_dir) if Path(yaml_dir).is_absolute() \
            else self.root / yaml_dir
        # YamlStore expects the directory CONTAINING "yaml files".
        self.store = YamlStore(store_root.parent)
        usage = cfg.get("usage_file") or "yaml_editor_ai/token_usage.json"
        self.usage_path = Path(usage) if Path(usage).is_absolute() \
            else self.root / usage

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.api_key)


# ---------------------------------------------------------------------------
# Default Muse client (own minimal transport, own timeout)
# ---------------------------------------------------------------------------

def _default_client(config: EditorConfig) -> Callable[..., dict]:
    """The editor's own Muse transport.

    JSON mode: the Meta Model API documents ``response_format``
    (``{"type": "json_object"}`` / ``json_schema``) for Chat
    Completions, so it is requested by default. If the endpoint
    rejects the parameter (HTTP 400), the SAME request is retried
    once WITHOUT it — a compatibility fallback for a rejected
    parameter, not a retry of the model's answer, and the only
    retry this client ever makes.

    Root cause of the 2026-11-11 production failure this replaces:
    the model spent the entire 600-token output budget on hidden
    reasoning (usage showed completion_tokens=600,
    reasoning_tokens=600, visible text 0) and the old client
    flattened the empty content into "invalid JSON at char 0".
    The reply dict therefore carries finish_reason, the content
    shape, and the usage block so the agent can classify empty /
    truncated / non-JSON outcomes precisely.

    Content extraction follows the documented Chat Completions
    schema: ``choices[0].message.content``, either a plain string or
    a list of typed parts. For list parts, text is taken from parts
    whose ``type`` is ``"text"`` (their ``text`` field); part types
    the schema also defines — ``refusal``, ``reasoning``,
    ``tool_calls`` — carry NO answer text and are skipped, never
    stringified into the reply (a refusal is surfaced via its own
    field so the agent can report it instead of misreading it as
    empty or invalid JSON). The documented ``delta`` field belongs
    to streaming chunks and is never present on a complete
    (non-streamed) response, so it is not consulted.
    """
    def _extract(data: dict, duration_ms: float) -> dict:
        choice = {}
        message: Any = {}
        try:
            choice = data["choices"][0] or {}
            message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError):
            pass
        raw_content = message.get("content") if isinstance(message, dict) else None
        parts: list[str] = []
        shape = type(raw_content).__name__
        refusal_text = None
        if isinstance(raw_content, str):
            parts = [raw_content] if raw_content else []
            shape = "string"
        elif isinstance(raw_content, list):
            shape = "list"
            for part in raw_content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype in (None, "text") and isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif ptype == "refusal":
                    refusal = part.get("refusal")
                    refusal_text = refusal if isinstance(refusal, str) else ""
                # reasoning / tool_calls / any other part type carries
                # no answer text — skipped, never stringified.
        if isinstance(message, dict):
            top_refusal = message.get("refusal")
            if isinstance(top_refusal, str) and top_refusal:
                refusal_text = top_refusal
        return {
            "text": "".join(parts),
            "usage": data.get("usage") or {},
            "model": data.get("model") or config.model,
            "duration_ms": duration_ms,
            "finish_reason": choice.get("finish_reason")
            if isinstance(choice, dict) else None,
            "content_shape": shape,
            "refusal": refusal_text,
        }

    def call(instruction: str, user_text: str) -> dict:
        from muse_ai.client import MuseError, http_post_json
        payload = {
            "model": config.model,
            "messages": [
                {"role": "developer", "content": instruction},
                {"role": "user", "content": user_text}],
            "max_completion_tokens": config.max_output_tokens,
            "temperature": config.temperature,
            "response_format": {"type": "json_object"},
        }
        # THE fix for the recurring empty responses: cap the model's
        # reasoning depth. Without reasoning_effort, Muse Spark
        # reasons at "high" by default and the reasoning pass alone
        # consumed first the 600-token budget, then a 2000-token
        # combined budget — both times finish_reason=length with
        # zero visible content. "minimal" is the shortest supported
        # pass ("none" returns HTTP 400 on Muse reasoning models).
        if config.reasoning_effort:
            payload["reasoning_effort"] = config.reasoning_effort
        url = f"{config.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {config.api_key}"}
        started = time.perf_counter()
        try:
            data = http_post_json(url, payload, headers,
                                  timeout=config.timeout_seconds)
        except MuseError as exc:
            if exc.status != 400:
                raise
            # HTTP 400 = a request parameter was rejected. Drop the
            # optional parameters ONE at a time — response_format
            # first, then reasoning_effort — so a rejection of one
            # never silently disables the other. At most two
            # compatibility retries, only ever on HTTP 400.
            dropped = []
            for optional in ("response_format", "reasoning_effort"):
                if optional not in payload:
                    continue
                payload.pop(optional, None)
                dropped.append(optional)
                logger.info("YAML Editor AI: endpoint returned HTTP "
                            "400; retrying the same request without "
                            "%s", ", ".join(dropped))
                try:
                    data = http_post_json(url, payload, headers,
                                          timeout=config.timeout_seconds)
                    break
                except MuseError as exc2:
                    if exc2.status != 400:
                        raise
                    data = None
            if data is None:
                raise
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        return _extract(data, duration_ms)
    return call


def _compact_resume_text(text: str, cap: int) -> str:
    """Shorten resume text for the single empty-response retry.

    Keeps section-heading-like lines (short lines, or lines ending
    with a colon) plus the first lines of the document (contact
    block), capped at ``cap`` characters. The editor only needs
    headings and terminology, so body prose can go — this roughly
    halves the input tokens of the retry."""
    lines = (text or "").splitlines()
    picked: list[str] = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        is_heading = len(stripped.split()) <= 6 or stripped.endswith(":")
        if idx < 12 or is_heading:
            picked.append(stripped)
    compact = "\n".join(picked)
    return compact[:cap] if len(compact) > cap else compact


def _parse_json_reply(text: str) -> Any:
    """Parse the model's strict-JSON reply, tolerating code fences."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class YamlEditorAgent:
    """One-shot completeness-YAML improvement for a single resume."""

    def __init__(self, config: EditorConfig | None = None, *,
                 client: Callable[..., dict] | None = None,
                 tracker: TokenTracker | None = None):
        self.config = config or EditorConfig()
        self.store = self.config.store
        self.tracker = tracker or TokenTracker(self.config.usage_path)
        self._client = client
        self._lock = threading.Lock()

    # -- trigger ---------------------------------------------------------
    def should_trigger(self, completeness_score: float | None,
                       text: str | None) -> tuple[bool, str]:
        if not self.config.enabled:
            return False, "disabled"
        if not self.config.api_key:
            return False, "not_configured"
        if completeness_score is None:
            return False, "no_completeness_score"
        if float(completeness_score) >= self.config.trigger_below:
            return False, "completeness_not_low"
        if not text or len(text.strip()) < self.config.min_text_chars:
            return False, "insufficient_text"
        return True, "triggered"

    # -- analytics ---------------------------------------------------------
    def analytics(self) -> dict[str, Any]:
        self.store.ensure_seeded()
        summary = self.tracker.summary()
        changelog = version_manager.read_changelog(self.store)
        updates = [e for e in changelog if e.get("status") == "success"]
        return {
            "summary": summary,
            "total_yaml_versions": self.store.current_version(),
            "current_version": version_manager.version_label(
                self.store.current_version()),
            "recent_updates": list(reversed(updates))[:10],
            "history": self.tracker.history(),
            "model": self.config.model,
            "provider": self.config.provider,
            "configured": self.config.configured,
            "enabled": self.config.enabled,
        }

    # -- main entry ---------------------------------------------------------
    def analyze(self, *, resume_id: str, filename: str,
                text: str, completeness_score: float | None,
                missing_sections: list[str] | None = None) -> dict[str, Any]:
        """Run one improvement analysis. Never raises; the returned
        dict always carries a ``status`` (success / no_change /
        failed / skipped) and a ``reason`` for skips."""
        ok, reason = self.should_trigger(completeness_score, text)
        if not ok:
            return {"status": "skipped", "reason": reason,
                    "resume_id": resume_id,
                    "trigger": {
                        "completeness_score": completeness_score,
                        "threshold": self.config.trigger_below,
                        "reason": reason},
                    "model": self.config.model,
                    "configured": self.config.configured,
                    "enabled": self.config.enabled}
        self.store.ensure_seeded()
        current_version = version_manager.version_label(
            self.store.current_version())
        if self.tracker.already_analyzed(resume_id, current_version):
            return {"status": "skipped", "reason": "already_analyzed",
                    "resume_id": resume_id,
                    "trigger": {
                        "completeness_score": completeness_score,
                        "threshold": self.config.trigger_below,
                        "reason": "already_analyzed"},
                    "model": self.config.model,
                    "yaml_version_before": current_version,
                    "yaml_version_after": current_version}
        config = self.store.read_active()
        # Missing sections arrive as display names from the checker;
        # map them to YAML category keys for the prompt.
        sections = config.get("sections") or {}
        display_to_key = {
            str((section or {}).get("display_name") or key): key
            for key, section in sections.items()}
        missing_keys = [display_to_key.get(name, name)
                        for name in (missing_sections or [])]
        instruction, user_text, _cats = prompt_builder.build_prompt(
            config, text, missing_sections=missing_keys or None,
            max_resume_chars=self.config.max_resume_chars)
        client = self._client or _default_client(self.config)
        started = time.perf_counter()
        try:
            reply = client(instruction, user_text)
        except Exception as exc:  # API timeout / rate limit / network
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.warning("YAML Editor AI call failed for %s: %s",
                           resume_id, exc)
            return self._record_failure(
                resume_id, filename, current_version, None,
                duration_ms, f"api_error: {exc}")
        # ONE controlled retry, only for an EMPTY reply: resend a
        # COMPACT request (headings + short lines only, capped well
        # below the normal resume cap, with an explicit
        # no-explanation instruction). A truncated or malformed reply
        # is NOT retried — the model did answer, and re-asking would
        # only re-spend tokens. Never more than one retry.
        if not (reply.get("text") or "").strip():
            compact_text = _compact_resume_text(
                text, self.config.max_resume_chars // 2)
            c_instruction, c_user, _ = prompt_builder.build_prompt(
                config, compact_text, missing_sections=missing_keys or None,
                max_resume_chars=self.config.max_resume_chars // 2)
            c_instruction = (
                c_instruction
                + " Output the JSON object immediately with no "
                  "explanation, no reasoning and no other text.")
            logger.info("YAML Editor AI: empty first reply for %s — "
                        "one compact retry", resume_id)
            try:
                retry_reply = client(c_instruction, c_user)
                retry_reply["attempts"] = 2
                retry_reply["retry_reason"] = "empty_response"
                prev_usage = reply.get("usage") or {}
                retry_usage = retry_reply.get("usage") or {}
                # Analytics must show the TRUE total spend of the
                # execution: sum both calls' usage.
                for key in ("prompt_tokens", "completion_tokens",
                            "total_tokens"):
                    a, b = prev_usage.get(key), retry_usage.get(key)
                    if isinstance(a, int) or isinstance(b, int):
                        retry_usage[key] = (a or 0) + (b or 0)
                d_prev = ((prev_usage.get("completion_tokens_details")
                           or {}).get("reasoning_tokens"))
                d_retry = ((retry_usage.get("completion_tokens_details")
                            or {}).get("reasoning_tokens"))
                if isinstance(d_prev, int) or isinstance(d_retry, int):
                    retry_usage.setdefault(
                        "completion_tokens_details", {})[
                        "reasoning_tokens"] = (d_prev or 0) + (d_retry or 0)
                retry_reply["usage"] = retry_usage
                reply = retry_reply
            except Exception as exc:
                logger.warning("YAML Editor AI compact retry failed "
                               "for %s: %s", resume_id, exc)
        duration_ms = float(reply.get("duration_ms")
                            or round((time.perf_counter() - started)
                                     * 1000, 2))
        usage = reply.get("usage") or {}
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        model = reply.get("model") or self.config.model
        # --- Response classification (Part B) -------------------------
        # Safe diagnostics ONLY: finish reason, response length,
        # content shape, model, token usage. NEVER the response body
        # or any resume content.
        reply_text = reply.get("text") or ""
        details = (usage.get("completion_tokens_details") or {}) \
            if isinstance(usage, dict) else {}
        diagnostics = {
            "finish_reason": reply.get("finish_reason"),
            "response_chars": len(reply_text),
            "content_shape": reply.get("content_shape"),
            "reasoning_tokens": details.get("reasoning_tokens"),
            "completion_tokens": output_tokens,
            "attempts": reply.get("attempts", 1),
            "retry_reason": reply.get("retry_reason"),
            "refusal": bool(reply.get("refusal")),
        }
        if reply.get("refusal") and not reply_text.strip():
            # The model declined (documented ``refusal`` content):
            # report it as what it is — not as an empty response and
            # not as invalid JSON. The refusal TEXT is never logged
            # or stored, only the fact that one occurred.
            logger.warning("YAML Editor AI refusal for %s: %s",
                           resume_id, diagnostics)
            return self._record_failure(
                resume_id, filename, current_version, model, duration_ms,
                "refusal: model declined to answer (refusal content "
                "present, no JSON produced)",
                input_tokens=input_tokens,
                output_tokens=output_tokens, diagnostics=diagnostics)
        if not reply_text.strip():
            # An empty reply is a FAILURE, never an empty success.
            # The known production cause: the whole output budget
            # was spent on hidden reasoning tokens.
            if diagnostics["reasoning_tokens"] and output_tokens \
                    and diagnostics["reasoning_tokens"] >= output_tokens:
                error = ("empty_response: model returned no visible "
                         "content — the full output budget was spent "
                         "on reasoning tokens "
                         f"({diagnostics['reasoning_tokens']} of "
                         f"{output_tokens} completion tokens); no "
                         "JSON was produced")
            else:
                error = ("empty_response: model returned no content "
                         f"(finish_reason={diagnostics['finish_reason']}, "
                         f"completion_tokens={output_tokens})")
            logger.warning("YAML Editor AI empty response for %s: %s",
                           resume_id, diagnostics)
            return self._record_failure(
                resume_id, filename, current_version, model, duration_ms,
                error, input_tokens=input_tokens,
                output_tokens=output_tokens, diagnostics=diagnostics)
        try:
            raw = _parse_json_reply(reply_text)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("YAML Editor AI returned invalid JSON for "
                           "%s: %s | diagnostics=%s",
                           resume_id, exc, diagnostics)
            return self._record_failure(
                resume_id, filename, current_version, model, duration_ms,
                f"invalid_json: {exc}",
                input_tokens=input_tokens, output_tokens=output_tokens,
                diagnostics=diagnostics)
        if not isinstance(raw, dict):
            logger.warning("YAML Editor AI returned non-object JSON "
                           "for %s | diagnostics=%s",
                           resume_id, diagnostics)
            return self._record_failure(
                resume_id, filename, current_version, model, duration_ms,
                "invalid_json: top-level JSON value is not an object",
                input_tokens=input_tokens, output_tokens=output_tokens,
                diagnostics=diagnostics)
        # Critical section: re-read the CURRENT version (another
        # resume may have updated it while our call was in flight),
        # re-validate against it, and apply atomically under the lock.
        with _UPDATE_LOCK, self._lock:
            version_before = version_manager.version_label(
                self.store.current_version())
            if self.tracker.already_analyzed(resume_id, version_before):
                return {"status": "skipped", "reason": "already_analyzed",
                        "resume_id": resume_id,
                        "trigger": {
                            "completeness_score": completeness_score,
                            "threshold": self.config.trigger_below,
                            "reason": "already_analyzed"},
                        "model": self.config.model,
                        "yaml_version_before": version_before,
                        "yaml_version_after": version_before}
            current_text = self.store.read_active_text()
            current_config = self.store.read_active()
            accepted, rejected = validate_suggestions(
                raw, config=current_config, resume_text=text,
                max_keyword_chars=self.config.max_keyword_chars,
                max_per_category=self.config.max_per_category,
                max_total=self.config.max_total)
            suggested = sum(len(v) for v in raw.values()
                            if isinstance(v, list)) \
                if isinstance(raw, dict) else 0
            base_entry = {
                "resume_id": resume_id, "filename": filename,
                "timestamp": version_manager._now_iso(),
                "model": model,
                "yaml_version_before": version_before,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "duration_ms": duration_ms,
                "keywords_suggested": suggested,
                "keywords_accepted": sum(len(v)
                                         for v in accepted.values()),
                "keywords_rejected": len(rejected),
                "rejected": rejected,
                "error": None,
            }
            if not accepted:
                entry = {**base_entry, "status": "no_change",
                         "yaml_version_after": version_before,
                         "keywords_added": {}}
                self.tracker.record(entry)
                return {**entry, "reason": "no_new_keywords"}
            try:
                updated_text = build_updated_config(
                    current_text, current_config, accepted)
                change = version_manager.apply_update(
                    self.store, updated_text, additions=accepted,
                    resume_id=resume_id, filename=filename,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens, model=model,
                    duration_ms=duration_ms)
            except Exception as exc:
                logger.exception("YAML Editor AI update failed for %s",
                                 resume_id)
                entry = {**base_entry, "status": "failed",
                         "yaml_version_after": version_before,
                         "keywords_added": {},
                         "error": f"update_failed: {exc}"}
                self.tracker.record(entry)
                return entry
            entry = {**base_entry, "status": "success",
                     "yaml_version_after": change["new_version"],
                     "keywords_added": accepted}
            self.tracker.record(entry)
            return entry

    def _record_failure(self, resume_id: str, filename: str,
                        version_before: str, model: str | None,
                        duration_ms: float, error: str, *,
                        input_tokens: int | None = None,
                        output_tokens: int | None = None,
                        diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
        entry = {
            "resume_id": resume_id, "filename": filename,
            "timestamp": version_manager._now_iso(),
            "model": model or self.config.model,
            "yaml_version_before": version_before,
            "yaml_version_after": version_before,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "duration_ms": duration_ms,
            "diagnostics": diagnostics,
            "keywords_suggested": 0, "keywords_accepted": 0,
            "keywords_rejected": 0, "keywords_added": {},
            "rejected": [], "status": "failed", "error": error,
        }
        self.tracker.record(entry)
        return entry


# ---------------------------------------------------------------------------
# Pipeline hook
# ---------------------------------------------------------------------------

_default_agent: YamlEditorAgent | None = None
_default_lock = threading.Lock()


def get_default_agent() -> YamlEditorAgent:
    global _default_agent
    with _default_lock:
        if _default_agent is None:
            _default_agent = YamlEditorAgent()
            try:
                _default_agent.store.ensure_seeded()
                version_manager.record_initialization(_default_agent.store)
            except Exception:
                logger.exception("YAML Editor AI store seeding failed")
        return _default_agent


def _record_inputs(record: dict[str, Any]) -> tuple[float | None, str, list[str]]:
    """Completeness score, selected text, and missing sections for a
    finished SATVIQ record (simple or legacy shape)."""
    score: float | None = None
    missing: list[str] = []
    attempts = record.get("attempts") or []
    selected = next((a for a in attempts if a.get("is_final_selected")),
                    None)
    quality = (selected or {}).get("quality") or record.get("quality") or {}
    completeness = quality.get("completeness") or {}
    if isinstance(completeness.get("score"), (int, float)):
        score = float(completeness["score"])
    missing = list(completeness.get("missing_sections") or [])
    text = record.get("final_extracted_text") \
        or record.get("extracted_text") or ""
    return score, text, missing


def maybe_improve_from_record(record: dict[str, Any], *,
                              agent: YamlEditorAgent | None = None) -> dict[str, Any]:
    """Post-processing hook for api.pipeline: analyse a finished
    record when it qualifies. Fully guarded — ANY failure here is
    logged and swallowed; resume processing is never affected, and
    the record's quality scores are never modified."""
    try:
        agent = agent or get_default_agent()
        score, text, missing = _record_inputs(record)
        return agent.analyze(
            resume_id=str(record.get("resume_id") or ""),
            filename=str(record.get("filename") or ""),
            text=text, completeness_score=score,
            missing_sections=missing)
    except Exception:  # pragma: no cover - absolute guard
        logger.exception("YAML Editor AI post-processing failed")
        return {"status": "skipped", "reason": "internal_error"}


def record_yaml_editor_block(record: dict[str, Any],
                             outcome: dict[str, Any], *,
                             agent: YamlEditorAgent | None = None) -> dict[str, Any]:
    """Build the structured ``record["yaml_editor"]`` block (Part C)
    from an analyze() outcome. Every value comes from the actual
    execution — token/keyword values stay null when the execution
    never produced them; nothing is invented. Also used to backfill
    OLD records from the token-tracker history (see
    ``yaml_editor_block_for_record``)."""
    agent = agent or get_default_agent()
    cfg = agent.config
    status_raw = str(outcome.get("status") or "skipped").lower()
    status = {"success": "SUCCESS", "no_change": "NO_CHANGE",
              "failed": "FAILED"}.get(status_raw, "SKIPPED")
    trigger_in = outcome.get("trigger") or {}
    score = trigger_in.get("completeness_score")
    if score is None:
        score = _record_inputs(record)[0]
    reason = outcome.get("reason") or outcome.get("error")
    if status == "FAILED" and outcome.get("error"):
        reason = outcome["error"]
    input_tokens = outcome.get("input_tokens")
    output_tokens = outcome.get("output_tokens")
    total_tokens = (input_tokens + output_tokens) \
        if isinstance(input_tokens, int) \
        and isinstance(output_tokens, int) else None
    keywords_added = outcome.get("keywords_added") or {}
    try:
        current_version = version_manager.version_label(
            agent.store.current_version())
    except Exception:
        current_version = None
    return {
        "status": status,
        "reason": reason,
        "trigger": {
            "completeness_score": score,
            "threshold": trigger_in.get("threshold", cfg.trigger_below),
            "triggered": status != "SKIPPED"
            or trigger_in.get("reason") == "already_analyzed",
            "reason": trigger_in.get("reason")
            or ("below_threshold" if status in ("SUCCESS", "NO_CHANGE", "FAILED")
                else reason),
        },
        "model": outcome.get("model") or cfg.model,
        "configured": cfg.configured,
        "enabled": cfg.enabled,
        "duration_ms": outcome.get("duration_ms"),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "keywords_suggested": outcome.get("keywords_suggested"),
        "keywords_accepted": outcome.get("keywords_accepted"),
        "keywords_rejected": outcome.get("keywords_rejected"),
        "keywords_added": keywords_added,
        "categories_modified": sorted(keywords_added.keys()),
        "rejected": outcome.get("rejected") or [],
        "diagnostics": outcome.get("diagnostics"),
        "error": outcome.get("error"),
        "yaml_version_before": outcome.get("yaml_version_before"),
        "yaml_version_after": outcome.get("yaml_version_after"),
        "current_yaml_version": current_version,
        "timestamp": outcome.get("timestamp"),
    }


def yaml_editor_block_for_record(record: dict[str, Any], *,
                                 agent: YamlEditorAgent | None = None) -> dict[str, Any]:
    """The record's yaml_editor block: the stored one when present;
    for records processed before Part C existed, a block derived
    from the execution-history entry for that resume (so the test
    resume processed on 2026-11-11 shows its real failed execution);
    otherwise an honest SKIPPED/not-executed block."""
    stored = record.get("yaml_editor")
    if isinstance(stored, dict) and stored.get("status"):
        return stored
    agent = agent or get_default_agent()
    resume_id = str(record.get("resume_id") or "")
    for entry in reversed(agent.tracker.all()):
        if entry.get("resume_id") == resume_id:
            return record_yaml_editor_block(record, entry, agent=agent)
    score = _record_inputs(record)[0]
    return record_yaml_editor_block(
        record,
        {"status": "skipped", "reason": "not_executed",
         "trigger": {"completeness_score": score,
                     "threshold": agent.config.trigger_below,
                     "reason": "not_executed"}},
        agent=agent)
