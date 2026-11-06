"""Persistent, UI-editable Muse system prompt (backend only).

The pipeline's system prompt has a built-in default (see
:mod:`muse_ai.service`). The Settings page can replace it: the custom
prompt is stored in ONE backend JSON file and is used verbatim as the
developer-role instructions for both Muse stages (the vision stage
still appends its source-reconciliation rules, exactly as it does for
the default prompt). Resetting deletes the override and restores the
default. No API key or other secret is ever stored here.

Storage: ``data/prompt_settings.json`` under the project root
(overridable with the ``SATVIQ_PROMPT_SETTINGS_FILE`` environment
variable, used by tests). The file is runtime data, not source.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 50_000


def settings_path() -> Path:
    override = os.environ.get("SATVIQ_PROMPT_SETTINGS_FILE")
    if override:
        return Path(override)
    from .config import project_root

    return project_root() / "data" / "prompt_settings.json"


def default_prompt() -> str:
    """The built-in system prompt, exactly as the service defines it."""
    from .service import _INSTRUCTIONS

    return _INSTRUCTIONS


def _read_override() -> dict[str, Any] | None:
    path = settings_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Prompt settings file unreadable: %s", path)
        return None
    prompt = (data or {}).get("system_prompt")
    if isinstance(prompt, str) and prompt.strip():
        return {"system_prompt": prompt,
                "updated_at": data.get("updated_at")}
    return None


def get_effective_prompt() -> str:
    """Custom prompt when one is saved, otherwise the default."""
    override = _read_override()
    return override["system_prompt"] if override else default_prompt()


def validate_prompt(text: str) -> str:
    """Return the prompt to store, or raise ValueError."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("System prompt must not be empty.")
    if len(text) > MAX_PROMPT_CHARS:
        raise ValueError(
            f"System prompt is {len(text)} characters; the limit is "
            f"{MAX_PROMPT_CHARS}.")
    return text


def save_prompt(text: str) -> dict[str, Any]:
    prompt = validate_prompt(text)
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "system_prompt": prompt,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)
    return prompt_status()


def reset_prompt() -> dict[str, Any]:
    settings_path().unlink(missing_ok=True)
    return prompt_status()


def prompt_status() -> dict[str, Any]:
    """Public status for the Settings API (never contains secrets)."""
    override = _read_override()
    default = default_prompt()
    effective = override["system_prompt"] if override else default
    return {
        "system_prompt": effective,
        "default_prompt": default,
        "is_custom": override is not None,
        "updated_at": override["updated_at"] if override else None,
        "max_chars": MAX_PROMPT_CHARS,
        "chars": len(effective),
    }
