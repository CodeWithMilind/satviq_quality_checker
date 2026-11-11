"""Small YAML config loader shared by the filters.

Each filter receives either an explicit ``config_path`` or a parsed
``config`` dict. When neither is given, the YAML file bundled under
``quality_checker/config/`` is loaded. Nothing is cached globally, so
tests can inject their own configuration freely.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# YAML Editor AI integration: when the versioned store exists, its
# ACTIVE completeness YAML is the single source of truth for
# completeness checks (the bundled file is only the seed/fallback).
_ACTIVE_COMPLETENESS = (
    Path(__file__).resolve().parents[2] / "yaml files" / "active"
    / "completeness.yaml")


def _default_path(filename: str) -> Path:
    if filename == "completeness.yaml" and _ACTIVE_COMPLETENESS.is_file():
        return _ACTIVE_COMPLETENESS
    return _CONFIG_DIR / filename


def load_config(filename: str, config: dict[str, Any] | None = None,
                config_path: str | Path | None = None) -> dict[str, Any]:
    """Resolve a filter configuration from dict, path, or the bundle."""
    if config is not None:
        return config
    path = Path(config_path) if config_path is not None else _default_path(filename)
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        logger.error("Configuration file not found: %s", path)
        raise
    except yaml.YAMLError:
        logger.exception("Invalid YAML in configuration file: %s", path)
        raise
    if not isinstance(loaded, dict):
        raise ValueError(f"Configuration in {path} must be a YAML mapping.")
    logger.debug("Loaded configuration from %s", path)
    return loaded


def scaled_penalty(ratio: float, warn: float, maximum_ratio: float, max_penalty: float) -> float:
    """Linear penalty: 0 at/below ``warn``, ``max_penalty`` at/above ``maximum_ratio``."""
    if ratio <= warn:
        return 0.0
    if maximum_ratio <= warn:
        return float(max_penalty)
    fraction = min(1.0, (ratio - warn) / (maximum_ratio - warn))
    return round(fraction * max_penalty, 2)


def invalid_input_result(filter_name: str, text: object) -> dict[str, Any]:
    """Common metrics payload for invalid (non-string) input."""
    return {
        "input_type": type(text).__name__,
        "char_count": 0,
        "word_count": 0,
    }
