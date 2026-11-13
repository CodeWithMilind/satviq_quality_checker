"""Calibration metrics engine (spec Parts F and G).

Pure functions over (ground truth, AI evaluation) pairs. Ground truth
is a validated annotation dict (``calibration.schemas``); the AI side
is the production ``evaluation`` dict stored on an analysis entry
(``matching_sections`` / ``missing_sections`` / ``completeness`` /
``quality`` / ``suitable_roles``).

Honesty rules: a metric with no usable denominator is None (the API
renders it "N/A" / "Insufficient Data") — never 0, never estimated.
Sections a human did not label are excluded from that resume's
denominators rather than guessed.
"""

from __future__ import annotations

import math
from typing import Any

from .schemas import CORE_SECTIONS, canonical_section

SCORE_DEVIATION_HIGH = 20.0
SCORE_DEVIATION_MEDIUM = 10.0


# ---------------------------------------------------------------
# Per-resume comparison
# ---------------------------------------------------------------

def _ai_section_sets(evaluation: dict[str, Any]) -> tuple[set[str], set[str]]:
    present: set[str] = set()
    for entry in evaluation.get("matching_sections") or []:
        name = canonical_section(entry.get("section"))
        if name:
            present.add(name)
    missing: set[str] = set()
    for entry in evaluation.get("missing_sections") or []:
        name = canonical_section(entry.get("section"))
        if name and name not in present:  # present wins on conflict
            missing.add(name)
    return present, missing


def compare_resume(ground_truth: dict[str, Any],
                   evaluation: dict[str, Any]) -> dict[str, Any]:
    """Compare one AI evaluation against one human annotation."""
    gt_present = {canonical_section(s) for s in
                  ground_truth.get("expected_present_sections") or []}
    gt_missing = {canonical_section(s) for s in
                  ground_truth.get("expected_missing_sections") or []}
    gt_present.discard(None)
    gt_missing.discard(None)
    ai_present, ai_missing = _ai_section_sets(evaluation)

    sections: dict[str, dict[str, str]] = {}
    missing_flags: dict[str, dict[str, str]] = {}
    labelled = gt_present | gt_missing
    for section in sorted(labelled | ai_present | ai_missing):
        if section in gt_present:
            gt_state = "present"
        elif section in gt_missing:
            gt_state = "missing"
        else:
            gt_state = "unlabelled"
        if section in ai_present:
            ai_state = "present"
        elif section in ai_missing:
            ai_state = "missing"
        else:
            ai_state = "absent"
        if gt_state == "unlabelled":
            outcome = "excluded"
        elif gt_state == "present":
            outcome = "tp" if ai_state == "present" else "fn"
        else:  # gt missing
            outcome = "fp" if ai_state == "present" else "tn"
        sections[section] = {"section": section, "gt": gt_state,
                             "ai": ai_state, "outcome": outcome}
        if gt_state == "present":
            flag = ("fp" if ai_state == "missing"
                    else "tn" if ai_state == "present" else "excluded")
        elif gt_state == "missing":
            flag = ("tp" if ai_state == "missing"
                    else "fn" if ai_state == "present" else "fn")
        else:
            flag = "excluded"
        if flag != "excluded" or gt_state != "unlabelled":
            missing_flags[section] = {"section": section, "gt": gt_state,
                                      "ai": ai_state, "outcome": flag}

    score_errors: dict[str, dict[str, float] | None] = {}
    for key, gt_key in (("completeness", "expected_completeness_score"),
                        ("quality", "expected_quality_score")):
        gt_score = ground_truth.get(gt_key)
        block = evaluation.get(key) or {}
        ai_score = block.get("score")
        if gt_score is None or ai_score is None:
            score_errors[key] = None
        else:
            score_errors[key] = {
                "ai": float(ai_score), "gt": float(gt_score),
                "error": float(ai_score) - float(gt_score),
            }

    gt_roles = ground_truth.get("expected_suitable_roles")
    roles: dict[str, Any]
    if gt_roles is None:
        roles = {"labelled": False}
    else:
        ai_roles = [str(r) for r in evaluation.get("suitable_roles") or []]
        gt_role_set = {str(r) for r in gt_roles}
        ai_role_set = set(ai_roles)
        per_role: dict[str, str] = {}
        for role in sorted(gt_role_set | ai_role_set):
            if role in gt_role_set and role in ai_role_set:
                per_role[role] = "tp"
            elif role in ai_role_set:
                per_role[role] = "fp"
            else:
                per_role[role] = "fn"
        ranked = list(evaluation.get("role_suitability") or [])
        ranked.sort(key=lambda r: float(r.get("score") or 0),
                    reverse=True)
        ranked_names = [str(r.get("role")) for r in ranked]
        if ranked_names:
            top1_hit: bool | None = ranked_names[0] in gt_role_set
            top3_hit: bool | None = any(
                name in gt_role_set for name in ranked_names[:3])
        else:
            top1_hit = None
            top3_hit = None
        roles = {
            "labelled": True,
            "per_role": per_role,
            "top1_hit": top1_hit,
            "top3_hit": top3_hit,
        }

    return {
        "career_stage": ground_truth.get("career_stage", ""),
        "professional_domain": ground_truth.get("professional_domain", ""),
        "sections": sections,
        "missing_flags": missing_flags,
        "score_errors": score_errors,
        "roles": roles,
    }


# ---------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------

def _prf(counts: dict[str, int]) -> dict[str, Any]:
    tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
    precision = tp / (tp + fp) if (tp + fp) > 0 else None
    recall = tp / (tp + fn) if (tp + fn) > 0 else None
    if precision is None or recall is None or (precision + recall) == 0:
        f1 = None
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {**counts, "precision": precision, "recall": recall, "f1": f1,
            "support": tp + fn}


def _empty_counts() -> dict[str, int]:
    return {"tp": 0, "fp": 0, "fn": 0, "tn": 0}


def _pearson(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 2:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    num = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    den_x = sum((x - mean_x) ** 2 for x in xs)
    den_y = sum((y - mean_y) ** 2 for y in ys)
    if den_x == 0 or den_y == 0:
        return None
    return num / math.sqrt(den_x * den_y)


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = average
        i = j + 1
    return ranks


def _score_block(pairs: list[tuple[float, float]]) -> dict[str, Any] | None:
    """pairs are (ai_score, gt_score). None when no pairs exist."""
    if not pairs:
        return None
    errors = [ai - gt for ai, gt in pairs]
    n = len(pairs)
    within_5 = sum(1 for e in errors if abs(e) <= 5) / n
    within_10 = sum(1 for e in errors if abs(e) <= 10) / n
    rank_pairs = list(zip(_ranks([p[0] for p in pairs]),
                          _ranks([p[1] for p in pairs])))
    return {
        "n": n,
        "mae": sum(abs(e) for e in errors) / n,
        "rmse": math.sqrt(sum(e * e for e in errors) / n),
        "bias": sum(errors) / n,
        "pearson": _pearson(pairs),
        "spearman": _pearson(rank_pairs),
        "within_5_pct": within_5,
        "within_10_pct": within_10,
        "mean_ai": sum(p[0] for p in pairs) / n,
        "mean_gt": sum(p[1] for p in pairs) / n,
    }


def aggregate_metrics(comparisons: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-resume comparisons into run-level metrics."""
    overall = _empty_counts()
    per_section: dict[str, dict[str, int]] = {}
    per_stage: dict[str, dict[str, int]] = {}
    per_domain: dict[str, dict[str, int]] = {}
    missing_counts = _empty_counts()
    score_pairs: dict[str, list[tuple[float, float]]] = {
        "completeness": [], "quality": []}
    role_counts = _empty_counts()
    role_labelled = 0
    top1_hits: list[bool] = []
    top3_hits: list[bool] = []

    def _add(table: dict[str, dict[str, int]], key: str, outcome: str):
        if outcome in ("tp", "fp", "fn", "tn"):
            table.setdefault(key, _empty_counts())[outcome] += 1

    for comparison in comparisons:
        stage = comparison.get("career_stage", "")
        domain = comparison.get("professional_domain", "")
        for section, outcome_row in (comparison.get("sections")
                                     or {}).items():
            outcome = outcome_row["outcome"]
            if outcome in ("tp", "fp", "fn", "tn"):
                overall[outcome] += 1
                _add(per_section, section, outcome)
                _add(per_stage, stage, outcome)
                _add(per_domain, domain, outcome)
        for _section, flag_row in (comparison.get("missing_flags")
                                   or {}).items():
            outcome = flag_row["outcome"]
            if outcome in ("tp", "fp", "fn", "tn"):
                missing_counts[outcome] += 1
        for key in ("completeness", "quality"):
            entry = (comparison.get("score_errors") or {}).get(key)
            if entry:
                score_pairs[key].append((entry["ai"], entry["gt"]))
        roles = comparison.get("roles") or {}
        if roles.get("labelled"):
            role_labelled += 1
            for _role, outcome in (roles.get("per_role") or {}).items():
                role_counts[outcome] += 1
            if roles.get("top1_hit") is not None:
                top1_hits.append(bool(roles["top1_hit"]))
            if roles.get("top3_hit") is not None:
                top3_hits.append(bool(roles["top3_hit"]))

    roles_block: dict[str, Any] | None = None
    if role_labelled:
        roles_block = _prf(role_counts)
        roles_block["labelled_resumes"] = role_labelled
        roles_block["top1_accuracy"] = (
            sum(top1_hits) / len(top1_hits) if top1_hits else None)
        roles_block["top3_accuracy"] = (
            sum(top3_hits) / len(top3_hits) if top3_hits else None)

    return {
        "resumes_compared": len(comparisons),
        "sections": {
            "overall": _prf(overall),
            "per_section": {k: _prf(v) for k, v in
                            sorted(per_section.items())},
            "per_career_stage": {k: _prf(v) for k, v in
                                 sorted(per_stage.items())},
            "per_domain": {k: _prf(v) for k, v in
                           sorted(per_domain.items())},
        },
        "missing_sections": _prf(missing_counts),
        "scores": {
            "completeness": _score_block(score_pairs["completeness"]),
            "quality": _score_block(score_pairs["quality"]),
        },
        "roles": roles_block,
    }


# ---------------------------------------------------------------
# Error records (calibration_errors rows, as dicts)
# ---------------------------------------------------------------

def error_records(comparison: dict[str, Any],
                  calibration_resume_id: str) -> list[dict[str, Any]]:
    """Flatten one comparison into persistable error records."""
    records: list[dict[str, Any]] = []

    def _add(error_type: str, section: str, expected: str | None,
             predicted: str | None, severity: str,
             details: dict[str, Any] | None = None):
        records.append({
            "calibration_resume_id": calibration_resume_id,
            "error_type": error_type,
            "section_name": section,
            "expected_value": expected,
            "predicted_value": predicted,
            "severity": severity,
            "details": details or {},
        })

    for section, row in (comparison.get("sections") or {}).items():
        severity = "high" if section in CORE_SECTIONS else "medium"
        if row["outcome"] == "fn":
            _add("false_negative_section", section, "present",
                 row["ai"], severity)
        elif row["outcome"] == "fp":
            _add("false_positive_section", section, "missing",
                 "present", severity)
    for section, row in (comparison.get("missing_flags") or {}).items():
        if row["outcome"] == "fp":
            _add("false_missing_section", section, "present",
                 "missing", "high")
        elif row["outcome"] == "fn" and row["gt"] == "missing":
            _add("missed_missing_section", section, "missing",
                 row["ai"], "medium")
    for key in ("completeness", "quality"):
        entry = (comparison.get("score_errors") or {}).get(key)
        if not entry:
            continue
        deviation = abs(entry["error"])
        if deviation >= SCORE_DEVIATION_HIGH:
            severity = "high"
        elif deviation >= SCORE_DEVIATION_MEDIUM:
            severity = "medium"
        else:
            continue
        _add("score_deviation", "", f"{key}={entry['gt']}",
             f"{key}={entry['ai']}", severity,
             {"score_type": key, "deviation": entry["error"]})
    roles = comparison.get("roles") or {}
    if roles.get("labelled"):
        for role, outcome in (roles.get("per_role") or {}).items():
            if outcome == "fp":
                _add("role_error", "", "not suitable", role, "medium",
                     {"role": role, "kind": "false_positive"})
            elif outcome == "fn":
                _add("role_error", "", role, "not predicted", "medium",
                     {"role": role, "kind": "false_negative"})
    return records
