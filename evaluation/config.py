"""Configuration for the Muse AI Resume Evaluation stage.

Two layers, deliberately separate:

1. ``evaluation/config.yaml`` — the evaluation catalogue (score
   classification bands, standard sections, good-to-have sections,
   career stages, role taxonomy). Loaded once, validated by
   :func:`validate_config`, and versioned via its ``config_version``
   string. This file is maintained by humans under the candidate →
   validation → regression → approval → activation flow documented
   in ``evaluation/yaml_versions/README.md``; no code in this project
   writes it.

2. Environment overrides for the evaluation Muse call itself,
   independent of the extraction stage's ``MUSE_*`` settings:

   - ``SATVIQ_EVALUATION_ENABLED`` (default true) — master switch.
   - ``SATVIQ_EVALUATION_MAX_OUTPUT_TOKENS`` (default 9000 —
     raised from 4000 after a real evaluation response was cut
     off mid-JSON; reasoning tokens share this budget).
   - ``SATVIQ_EVALUATION_REASONING_EFFORT`` (default ``low``) —
     Muse Spark is a reasoning model whose reasoning tokens count
     toward the output budget; at the API's default (high) effort a
     reasoning pass can consume the entire budget before any visible
     output is produced (observed on this project), so the
     evaluation call always sets an explicit effort.

The API key and model still come from the shared Muse configuration
(``muse_ai.config.load_muse_config`` — backend ``.env`` only, never
hardcoded, never exposed).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import yaml

_PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = _PACKAGE_DIR.parent
_CONFIG_PATH = _PACKAGE_DIR / "config.yaml"
_COMPLETENESS_YAML = (PROJECT_ROOT / "quality_checker" / "config"
                      / "completeness.yaml")

DEFAULT_MAX_OUTPUT_TOKENS = 9000
DEFAULT_REASONING_EFFORT = "low"
_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")

# Evaluation importance (Critical/High/Medium/Low, used in
# missing-section reporting) mapped onto the deterministic Quality
# Checker's importance scale, for cross-checks between the two.
IMPORTANCE_TO_QC = {
    "Critical": "required",
    "High": "recommended",
    "Medium": "optional",
    "Low": "optional",
}
IMPORTANCE_LEVELS = tuple(IMPORTANCE_TO_QC)

_cached_config: dict[str, Any] | None = None


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate an evaluation configuration mapping.

    Raises ValueError with a specific message on the first problem:
    missing/empty required keys, non-monotonic classification bands,
    duplicate section or role names, or a role with no required
    skills. Returns the config unchanged when valid.
    """
    if not isinstance(config, dict):
        raise ValueError("Evaluation configuration must be a mapping.")
    if not str(config.get("config_version") or "").strip():
        raise ValueError("Evaluation configuration needs a config_version.")

    bands = config.get("classification_bands")
    if not isinstance(bands, list) or len(bands) < 2:
        raise ValueError("classification_bands must list at least two bands.")
    previous: float | None = None
    for band in bands:
        if not isinstance(band, dict) or not str(band.get("label") or "").strip():
            raise ValueError("Every classification band needs a label.")
        minimum = band.get("min_score")
        if not isinstance(minimum, (int, float)) or isinstance(minimum, bool) \
                or not 0 <= minimum <= 100:
            raise ValueError(
                f"Classification band {band.get('label')!r} has an invalid "
                f"min_score {minimum!r} (must be a number in 0-100).")
        if previous is not None and minimum >= previous:
            raise ValueError(
                "classification_bands must be strictly decreasing by "
                f"min_score (got {minimum} after {previous}).")
        previous = minimum
    if bands[-1].get("min_score") != 0:
        raise ValueError(
            "The lowest classification band must start at min_score 0 so "
            "every score in 0-100 is classified.")

    sections = config.get("standard_sections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("standard_sections must be a non-empty list.")
    seen_sections: set[str] = set()
    for section in sections:
        name = str((section or {}).get("name") or "").strip()
        if not name:
            raise ValueError("Every standard section needs a name.")
        if name.lower() in seen_sections:
            raise ValueError(f"Duplicate standard section name: {name!r}.")
        seen_sections.add(name.lower())
        importance = str((section or {}).get("importance") or "").strip()
        if importance not in IMPORTANCE_TO_QC:
            raise ValueError(
                f"Standard section {name!r} has invalid importance "
                f"{importance!r} (expected one of "
                f"{', '.join(IMPORTANCE_LEVELS)}).")

    good_to_have = config.get("good_to_have_sections")
    if not isinstance(good_to_have, list) or not good_to_have:
        raise ValueError("good_to_have_sections must be a non-empty list.")
    seen_gth: set[str] = set()
    for name in good_to_have:
        key = str(name or "").strip().lower()
        if not key:
            raise ValueError("Good-to-have section names must not be empty.")
        if key in seen_gth:
            raise ValueError(f"Duplicate good-to-have section: {name!r}.")
        seen_gth.add(key)

    stages = config.get("career_stages")
    if not isinstance(stages, list) or not stages:
        raise ValueError("career_stages must be a non-empty list.")
    seen_stages: set[str] = set()
    for stage in stages:
        name = str((stage or {}).get("name") or "").strip()
        if not name:
            raise ValueError("Every career stage needs a name.")
        if name.lower() in seen_stages:
            raise ValueError(f"Duplicate career stage name: {name!r}.")
        seen_stages.add(name.lower())

    roles = config.get("role_taxonomy")
    if not isinstance(roles, list) or not roles:
        raise ValueError("role_taxonomy must be a non-empty list.")
    seen_roles: set[str] = set()
    for role in roles:
        name = str((role or {}).get("name") or "").strip()
        if not name:
            raise ValueError("Every role in the taxonomy needs a name.")
        if name.lower() in seen_roles:
            raise ValueError(f"Duplicate role name in taxonomy: {name!r}.")
        seen_roles.add(name.lower())
        required = (role or {}).get("required_skills")
        if not isinstance(required, list) or not [s for s in required if str(s).strip()]:
            raise ValueError(
                f"Role {name!r} must list at least one required skill.")
    return config


def load_config() -> dict[str, Any]:
    """Load and validate ``evaluation/config.yaml`` (cached)."""
    global _cached_config
    if _cached_config is None:
        with _CONFIG_PATH.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        _cached_config = validate_config(loaded)
    return _cached_config


def classification_for(score: float, config: dict[str, Any] | None = None) -> str:
    """The classification label for a 0-100 score under the bands."""
    config = config or load_config()
    for band in config["classification_bands"]:
        if score >= band["min_score"]:
            return str(band["label"])
    return str(config["classification_bands"][-1]["label"])


def role_names(config: dict[str, Any] | None = None) -> list[str]:
    config = config or load_config()
    return [str(role["name"]) for role in config["role_taxonomy"]]


def career_stage_names(config: dict[str, Any] | None = None) -> list[str]:
    config = config or load_config()
    return [str(stage["name"]) for stage in config["career_stages"]]


def completeness_yaml_digest() -> str:
    """First 12 hex chars of the SHA-256 of the Quality Checker's
    completeness.yaml — the fingerprint every analysis records, so
    an evaluation can be tied to the exact checker configuration in
    force when it ran."""
    digest = hashlib.sha256()
    with _COMPLETENESS_YAML.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()[:12]


def yaml_version(config: dict[str, Any] | None = None) -> str:
    """The configuration version string stored on every analysis,
    e.g. ``completeness@1a2b3c4d5e6f+eval-1.0.0``.

    ``config_version`` in evaluation/config.yaml already carries
    the ``eval-`` prefix (``"eval-1.0.0"``), so it is appended
    as-is; an earlier build formatted it as ``+eval-{version}``
    and produced the doubled ``eval-eval-1.0.0`` label seen on the
    dashboard. Stored entries written by that build keep their old
    string (history is never rewritten); only new analyses get
    the corrected label, and both forms parse to the same parts."""
    config = config or load_config()
    return (f"completeness@{completeness_yaml_digest()}"
            f"+{config['config_version']}")


# --------------------------------------------------------------------- #
# Environment overrides for the evaluation Muse call
# --------------------------------------------------------------------- #

def _env() -> dict[str, str]:
    try:
        from muse_ai.config import load_env_file

        load_env_file()
    except Exception:
        pass
    return dict(os.environ)


def evaluation_enabled(env: dict[str, str] | None = None) -> bool:
    env = env if env is not None else _env()
    raw = (env.get("SATVIQ_EVALUATION_ENABLED") or "").strip().lower()
    if not raw:
        return True
    return raw not in ("false", "0", "no", "off")


def evaluation_max_output_tokens(env: dict[str, str] | None = None) -> int:
    env = env if env is not None else _env()
    raw = (env.get("SATVIQ_EVALUATION_MAX_OUTPUT_TOKENS") or "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_MAX_OUTPUT_TOKENS


def evaluation_reasoning_effort(env: dict[str, str] | None = None) -> str:
    env = env if env is not None else _env()
    raw = (env.get("SATVIQ_EVALUATION_REASONING_EFFORT")
           or "").strip().lower()
    if raw in _REASONING_EFFORTS:
        return raw
    return DEFAULT_REASONING_EFFORT


def public_metadata(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Non-secret evaluation configuration metadata for the API:
    versions, bands, section and role catalogues. Never contains
    credentials or prompt text."""
    config = config or load_config()
    return {
        "config_version": config["config_version"],
        "yaml_version": yaml_version(config),
        "enabled": evaluation_enabled(),
        "classification_bands": [
            {"min_score": band["min_score"], "label": band["label"]}
            for band in config["classification_bands"]],
        "suitable_role_min_score": config.get("suitable_role_min_score", 50),
        "standard_sections": [
            {"name": section["name"], "importance": section["importance"],
             "qc_importance": IMPORTANCE_TO_QC[section["importance"]],
             "qc_display_name": section.get("qc_display_name")}
            for section in config["standard_sections"]],
        "good_to_have_sections": list(config["good_to_have_sections"]),
        "career_stages": career_stage_names(config),
        "roles": role_names(config),
    }
