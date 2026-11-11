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
        self.max_output_tokens = int(cfg.get("max_output_tokens") or 600)
        self.temperature = float(cfg.get("temperature") or 0.0)
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
    def call(instruction: str, user_text: str) -> dict:
        from muse_ai.client import http_post_json
        payload = {
            "model": config.model,
            "messages": [
                {"role": "developer", "content": instruction},
                {"role": "user", "content": user_text}],
            "max_completion_tokens": config.max_output_tokens,
            "temperature": config.temperature,
        }
        started = time.perf_counter()
        data = http_post_json(
            f"{config.base_url}/chat/completions", payload,
            {"Authorization": f"Bearer {config.api_key}"},
            timeout=config.timeout_seconds)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(
                "Muse API reply had no message content.") from exc
        return {"text": text, "usage": data.get("usage") or {},
                "model": data.get("model") or config.model,
                "duration_ms": duration_ms}
    return call


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
                    "resume_id": resume_id}
        self.store.ensure_seeded()
        current_version = version_manager.version_label(
            self.store.current_version())
        if self.tracker.already_analyzed(resume_id, current_version):
            return {"status": "skipped", "reason": "already_analyzed",
                    "resume_id": resume_id,
                    "yaml_version": current_version}
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
        duration_ms = float(reply.get("duration_ms")
                            or round((time.perf_counter() - started)
                                     * 1000, 2))
        usage = reply.get("usage") or {}
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        model = reply.get("model") or self.config.model
        try:
            raw = _parse_json_reply(reply.get("text") or "")
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("YAML Editor AI returned invalid JSON for "
                           "%s: %s", resume_id, exc)
            return self._record_failure(
                resume_id, filename, current_version, model, duration_ms,
                f"invalid_json: {exc}",
                input_tokens=input_tokens, output_tokens=output_tokens)
        # Critical section: re-read the CURRENT version (another
        # resume may have updated it while our call was in flight),
        # re-validate against it, and apply atomically under the lock.
        with _UPDATE_LOCK, self._lock:
            version_before = version_manager.version_label(
                self.store.current_version())
            if self.tracker.already_analyzed(resume_id, version_before):
                return {"status": "skipped", "reason": "already_analyzed",
                        "resume_id": resume_id,
                        "yaml_version": version_before}
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
                        output_tokens: int | None = None) -> dict[str, Any]:
        entry = {
            "resume_id": resume_id, "filename": filename,
            "timestamp": version_manager._now_iso(),
            "model": model or self.config.model,
            "yaml_version_before": version_before,
            "yaml_version_after": version_before,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "duration_ms": duration_ms,
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
    the record (including its quality scores) is never modified."""
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
