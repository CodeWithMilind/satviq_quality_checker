"""Configuration for the simple production pipeline.

All values have safe defaults; ``pipeline/config.yaml`` (when present)
overrides them. No credentials live here -- the LLM provider/model are
configured through the existing Muse backend ``.env`` mechanism.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_WEIGHTS: dict[str, float] = {
    "availability": 0.10,
    "length_adequacy": 0.30,
    "readability": 0.15,
    "cleanliness": 0.10,
    "non_duplication": 0.10,
    "completeness": 0.15,
    "structure": 0.10,
}

_DEFAULT_TIE_BREAK = ["tika", "pymupdf", "docling"]


@dataclass
class PipelineConfig:
    min_quality_threshold: float = 60.0
    # EXTRACTION-ONLY safety limit (spec §5): the maximum time to
    # WAIT for one extractor in the concurrent run. It is a
    # stuck-process guard, deliberately generous (10 minutes) so it
    # never clips legitimate extraction — Docling on a large PDF
    # legitimately takes minutes. It NEVER applies to Muse AI:
    # Muse calls have no timeout at all. None disables the guard.
    per_extractor_timeout_seconds: float | None = 600.0
    tie_break_order: list[str] = field(default_factory=lambda: list(_DEFAULT_TIE_BREAK))
    llm_max_retries: int = 2
    llm_temperature: float | None = None
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    # alias used in some call sites
    scoring_weights: dict[str, float] | None = None

    def __post_init__(self) -> None:
        if self.scoring_weights is not None:
            # scoring_weights is an alias for weights
            merged = dict(DEFAULT_WEIGHTS)
            merged.update(self.scoring_weights)
            self.weights = merged
        else:
            # ensure all keys present
            merged = dict(DEFAULT_WEIGHTS)
            merged.update(self.weights or {})
            self.weights = merged
            self.scoring_weights = dict(self.weights)
        if not self.tie_break_order:
            self.tie_break_order = list(_DEFAULT_TIE_BREAK)

    def effective_weights(self) -> dict[str, float]:
        return dict(self.weights)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PipelineConfig":
        if not data or not isinstance(data, dict):
            return cls()
        kwargs: dict[str, Any] = {}
        if "min_quality_threshold" in data:
            try:
                kwargs["min_quality_threshold"] = float(data["min_quality_threshold"])
            except Exception:
                pass
        if "per_extractor_timeout_seconds" in data:
            try:
                v = data["per_extractor_timeout_seconds"]
                kwargs["per_extractor_timeout_seconds"] = \
                    float(v) if v is not None else None
            except Exception:
                pass
        if "tie_break_order" in data and isinstance(data["tie_break_order"], list):
            kwargs["tie_break_order"] = [str(x) for x in data["tie_break_order"]]
        if "llm_max_retries" in data:
            try:
                kwargs["llm_max_retries"] = int(data["llm_max_retries"])
            except Exception:
                pass
        if "llm_temperature" in data:
            v = data["llm_temperature"]
            kwargs["llm_temperature"] = float(v) if v is not None else None
        weights = data.get("weights") or data.get("scoring_weights") or data.get("quality_weights")
        if isinstance(weights, dict) and weights:
            merged = dict(DEFAULT_WEIGHTS)
            for k, v in weights.items():
                try:
                    merged[str(k)] = float(v)
                except Exception:
                    continue
            kwargs["weights"] = merged
        return cls(**kwargs)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "PipelineConfig":
        p = Path(path)
        if not p.is_file():
            return cls()
        try:
            import yaml  # type: ignore
            loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            logger.warning("Could not load pipeline config from %s; using defaults", p, exc_info=True)
            return cls()
        if not isinstance(loaded, dict):
            return cls()
        # Support nested 'pipeline:' key as well as flat
        data = loaded.get("pipeline") if isinstance(loaded.get("pipeline"), dict) else loaded
        # weights may be nested under quality/scoring
        if "weights" not in data:
            for key in ("quality", "scoring", "quality_scoring"):
                sub = data.get(key)
                if isinstance(sub, dict) and isinstance(sub.get("weights"), dict):
                    data = dict(data)
                    data["weights"] = sub["weights"]
                    break
        return cls.from_dict(data)


def _default_config_path() -> Path:
    return Path(__file__).resolve().parent / "config.yaml"


def load_pipeline_config(path: str | Path | None = None) -> PipelineConfig:
    """Load config from pipeline/config.yaml if present, else defaults.
    Tolerant of missing file."""
    candidate = Path(path) if path is not None else _default_config_path()
    if candidate.is_file():
        return PipelineConfig.from_yaml(candidate)
    return PipelineConfig()
