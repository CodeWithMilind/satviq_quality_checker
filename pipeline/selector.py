"""Best-extractor selection based on quality reports."""
from __future__ import annotations

from typing import Any

from .config import PipelineConfig


def _reports_map(reports: Any) -> dict[str, dict[str, Any]]:
    if reports is None:
        return {}
    if isinstance(reports, dict):
        return dict(reports)
    if isinstance(reports, list):
        out: dict[str, dict[str, Any]] = {}
        for r in reports:
            if isinstance(r, dict) and r.get("extractor"):
                out[r["extractor"]] = r
        return out
    return {}


def select_best_extractor(results: list[dict[str, Any]], reports: Any, config: PipelineConfig | None = None) -> dict[str, Any]:
    """Select the highest-quality successful extractor.

    Returns dict with selected_extractor, selected_quality_score,
    selection_reason, status ("selected"/"no_usable_extraction"/"below_threshold").
    """
    cfg = config or PipelineConfig()
    threshold = float(cfg.min_quality_threshold)
    tie_order = list(cfg.tie_break_order or ["tika", "pymupdf", "docling"])
    report_map = _reports_map(reports)

    def tie_index(name: str) -> int:
        try:
            return tie_order.index(name)
        except ValueError:
            return len(tie_order)

    candidates: list[tuple[str, float, dict[str, Any]]] = []
    for res in results or []:
        name = res.get("extractor") or res.get("extractor_name")
        if not name:
            continue
        if res.get("status") != "success":
            continue
        text = res.get("extracted_text") or res.get("text") or ""
        if not isinstance(text, str) or not text.strip():
            continue
        report = report_map.get(name) or {}
        score = report.get("quality_score")
        if score is None:
            continue
        # Report must not itself be failed for a usable candidate;
        # a report score of 0 with success text is still a candidate
        # but will fall below threshold.
        candidates.append((name, float(score), res))

    if not candidates:
        return {
            "selected_extractor": None,
            "selected_quality_score": None,
            "selection_reason": "No usable extraction: all extractors failed, timed out, or produced empty/unusable text.",
            "status": "no_usable_extraction",
        }

    # Highest score wins; ties by tie_break_order then name
    candidates_sorted = sorted(candidates, key=lambda c: (-c[1], tie_index(c[0]), c[0]))
    best_name, best_score, _ = candidates_sorted[0]

    if best_score < threshold:
        return {
            "selected_extractor": None,
            "selected_quality_score": best_score,
            "selection_reason": (
                f"Best extractor '{best_name}' scored {best_score:.1f}, which is below "
                f"the minimum quality threshold of {threshold:.1f}; no extractor was selected."
            ),
            "status": "below_threshold",
        }

    # Build reason, noting ties if any
    tied = [c for c in candidates_sorted if c[1] == best_score]
    if len(tied) > 1:
        reason = (
            f"Selected '{best_name}' with quality score {best_score:.1f} "
            f"(tie among {[t[0] for t in tied]} broken by configured tie-break order {tie_order})."
        )
    else:
        reason = f"Selected '{best_name}' with the highest quality score {best_score:.1f} (threshold {threshold:.1f})."

    return {
        "selected_extractor": best_name,
        "selected_quality_score": best_score,
        "selection_reason": reason,
        "status": "selected",
    }
