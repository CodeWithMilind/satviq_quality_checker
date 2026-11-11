"""Analytics aggregation over stored SATVIQ records (Analytics Phase 1).

Pure functions over the record dicts returned by ``api.store.list_records``
— the same read-time aggregation pattern as ``store.compute_stats``. This
module never touches the filesystem, never mutates records, and invents
nothing: every metric follows the definitions in SATVIQ_Analytics_Spec.md
§6, and partial data (missing token usage, missing quality reports, legacy
records without newer fields) is handled by shrinking the denominator and
reporting coverage, never by filling in values.

Phase 1 notes:
- Manual Comparison runs (``record["extractor_results"]``) are EXCLUDED
  from all pipeline statistics, per spec. ``include_manual`` is accepted
  for forward compatibility with the Phase 2 toggle but does not change
  Phase 1 output.
- Day buckets use Asia/Calcutta (spec §2); ``uploaded_at`` is stored UTC.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

# Asia/Calcutta as a fixed UTC+05:30 offset: IST has no daylight saving,
# and a fixed offset avoids depending on system tzdata (absent in some
# environments, and on Windows without the tzdata package).
_IST = timezone(timedelta(hours=5, minutes=30))
_FILTERS = ("text_health", "completeness", "reading_order")
_SECTIONS = (
    "Contact Information", "Summary", "Skills", "Work Experience",
    "Education", "Projects", "Certifications",
)


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _headline(quality: Any) -> float | None:
    """Mean of the filter scores present — the UI's headline rule."""
    if not isinstance(quality, dict):
        return None
    scores = [
        quality[name]["score"]
        for name in _FILTERS
        if isinstance(quality.get(name), dict) and _is_num(quality[name].get("score"))
    ]
    return sum(scores) / len(scores) if scores else None


def _selected_extractor(record: dict[str, Any]) -> str | None:
    used = record.get("extractor_used")
    if isinstance(used, str) and used.strip():
        return used.strip().lower()
    for attempt in record.get("attempts") or []:
        if isinstance(attempt, dict) and attempt.get("is_final_selected"):
            name = attempt.get("extractor_name")
            if isinstance(name, str) and name.strip():
                return name.strip().lower()
    return None


def _selected_chars(record: dict[str, Any], selected: str | None) -> int:
    for attempt in record.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        name = str(attempt.get("extractor_name") or "").strip().lower()
        if attempt.get("is_final_selected") or (selected and name == selected):
            length = attempt.get("extracted_text_length")
            if _is_num(length):
                return int(length)
    for key in ("final_extracted_text", "extracted_text"):
        text = record.get(key)
        if isinstance(text, str) and text:
            return len(text)
    return 0


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: list[float]) -> float | None:
    """Standard median: the middle value, or the mean of the two
    middle values for an even count. Unlike the mean it is not
    dragged by a single very slow resume."""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _round1(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def compute_analytics(
    records: list[dict[str, Any]],
    *,
    days: int | None = None,
    include_manual: bool = False,
) -> dict[str, Any]:
    """Aggregate stored records into the Analytics summary (spec §5.3).

    ``days=None`` means all time. ``include_manual`` is accepted for the
    Phase 2 API shape; Phase 1 always excludes Manual Comparison runs.
    """
    _ = include_manual  # Phase 2 toggle — see module docstring.
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(days=days) if days else None

    in_range: list[dict[str, Any]] = []
    without_ts = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        ts = _parse_ts(record.get("uploaded_at"))
        if ts is None:
            without_ts += 1
            if cutoff is None:
                in_range.append(record)
            continue
        if cutoff is None or ts >= cutoff:
            in_range.append(record)

    # ---------------------------------------------------------------- #
    # KPIs
    # ---------------------------------------------------------------- #
    total = len(in_range)
    succeeded = sum(1 for r in in_range if r.get("success") is True)
    headlines = [h for r in in_range if (h := _headline(r.get("quality"))) is not None]
    durations = [float(r["duration_ms"]) for r in in_range if _is_num(r.get("duration_ms"))]
    fallback_base = [r for r in in_range if "fallback_occurred" in r]
    fallback_true = sum(1 for r in fallback_base if r.get("fallback_occurred") is True)

    # ---------------------------------------------------------------- #
    # Token accounting (shared by the KPI total and the tokens block)
    # ---------------------------------------------------------------- #
    tok_in = tok_out = tok_cached = tok_total = 0
    calls = calls_ok = calls_failed = calls_timed_out = 0
    calls_with_usage = 0
    per_resume_tokens: list[float] = []
    selected_chars_total = 0
    norm_tokens_saved = 0
    norm_records_with_estimates = 0
    day_buckets: dict[str, dict[str, int]] = {}

    for record in in_range:
        usage = record.get("token_usage") or {}
        if isinstance(usage, dict):
            for key, attr in (("api_calls", "calls"), ("successful_calls", "calls_ok"),
                              ("failed_calls", "calls_failed"),
                              ("timed_out_calls", "calls_timed_out")):
                if _is_num(usage.get(key)):
                    if attr == "calls":
                        calls += int(usage[key])
                    elif attr == "calls_ok":
                        calls_ok += int(usage[key])
                    elif attr == "calls_failed":
                        calls_failed += int(usage[key])
                    else:
                        calls_timed_out += int(usage[key])
            if usage.get("usage_available") is True:
                if _is_num(usage.get("api_calls")):
                    calls_with_usage += int(usage["api_calls"])
                inp, out = usage.get("input_tokens"), usage.get("output_tokens")
                tot = usage.get("total_tokens")
                if not _is_num(tot) and _is_num(inp) and _is_num(out):
                    tot = inp + out
                if _is_num(inp):
                    tok_in += int(inp)
                if _is_num(out):
                    tok_out += int(out)
                if _is_num(usage.get("cached_input_tokens")):
                    tok_cached += int(usage["cached_input_tokens"])
                if _is_num(tot):
                    tok_total += int(tot)
                    per_resume_tokens.append(float(tot))
        selected_chars_total += _selected_chars(record, _selected_extractor(record))

        norm = record.get("text_normalization") or {}
        if isinstance(norm, dict):
            orig_est, norm_est = norm.get("original_estimated_tokens"), norm.get("normalized_estimated_tokens")
            if _is_num(orig_est) and _is_num(norm_est):
                norm_tokens_saved += int(orig_est) - int(norm_est)
                norm_records_with_estimates += 1

        ts = _parse_ts(record.get("uploaded_at"))
        if ts is not None:
            day = ts.astimezone(_IST).date().isoformat()
            bucket = day_buckets.setdefault(day, {"tokens": 0, "resumes": 0})
            bucket["resumes"] += 1
            usage = record.get("token_usage") or {}
            if isinstance(usage, dict) and usage.get("usage_available") is True:
                tot = usage.get("total_tokens")
                if not _is_num(tot) and _is_num(usage.get("input_tokens")) and _is_num(usage.get("output_tokens")):
                    tot = usage["input_tokens"] + usage["output_tokens"]
                if _is_num(tot):
                    bucket["tokens"] += int(tot)

    by_day: list[dict[str, Any]] = []
    if day_buckets:
        if days:
            end_day = now_utc.astimezone(_IST).date()
            start_day = end_day - timedelta(days=days - 1)
        else:
            start_day = min(datetime.fromisoformat(d).date() for d in day_buckets)
            end_day = max(datetime.fromisoformat(d).date() for d in day_buckets)
        span = (end_day - start_day).days + 1
        if span <= 366:
            cursor = start_day
            while cursor <= end_day:
                key = cursor.isoformat()
                bucket = day_buckets.get(key, {"tokens": 0, "resumes": 0})
                by_day.append({"date": key, "tokens": bucket["tokens"],
                               "resumes": bucket["resumes"]})
                cursor += timedelta(days=1)
        else:  # very long history: only days with activity, still ascending
            for key in sorted(day_buckets):
                by_day.append({"date": key, **day_buckets[key]})

    tokens_block = {
        "input": tok_in, "output": tok_out, "cached_input": tok_cached,
        "total": tok_total,
        "api_calls": calls, "successful_calls": calls_ok,
        "failed_calls": calls_failed, "timed_out_calls": calls_timed_out,
        "avg_per_resume": _round1(_mean(per_resume_tokens)),
        "p95_per_resume": _round1(_percentile(per_resume_tokens, 95)),
        "tokens_per_1000_chars": (
            _round1(tok_total / (selected_chars_total / 1000))
            if selected_chars_total > 0 and per_resume_tokens else None
        ),
        "normalization_tokens_saved": norm_tokens_saved,
        "by_day": by_day,
    }

    kpis = {
        "total_processed": total,
        "success_rate_percent": _round1(succeeded / total * 100) if total else None,
        "avg_quality_score": _round1(_mean(headlines)),
        "avg_duration_ms": int(round(_mean(durations))) if durations else None,
        "median_duration_ms": int(round(_median(durations))) if durations else None,
        "p95_duration_ms": int(round(_percentile(durations, 95))) if durations else None,
        "total_tokens": tok_total,
        "fallback_rate_percent": (
            _round1(fallback_true / len(fallback_base) * 100) if fallback_base else None
        ),
        "fallback_base": len(fallback_base),
    }

    # ---------------------------------------------------------------- #
    # Extractor performance (pipeline attempts only — manual runs in
    # record["extractor_results"] are never read here)
    # ---------------------------------------------------------------- #
    tool: dict[str, dict[str, Any]] = {}

    def _tool(name: str) -> dict[str, Any]:
        return tool.setdefault(name, {
            "attempts": 0, "succeeded": 0, "failed": 0, "selected": 0,
            "win_base": 0, "durations": [], "chars": [], "quality_scores": [],
            "wasted_attempts": 0, "wasted_ms": 0.0,
        })

    for record in in_range:
        attempts = [a for a in (record.get("attempts") or []) if isinstance(a, dict)]
        selected = _selected_extractor(record)
        ran: set[str] = set()
        for attempt in attempts:
            name = str(attempt.get("extractor_name") or "").strip().lower()
            if not name:
                continue
            ran.add(name)
            row = _tool(name)
            row["attempts"] += 1
            if attempt.get("success") is True:
                row["succeeded"] += 1
                score = _headline(attempt.get("quality_checker_report"))
                if score is not None:
                    row["quality_scores"].append(score)
                if _is_num(attempt.get("extracted_text_length")):
                    row["chars"].append(float(attempt["extracted_text_length"]))
                if selected is not None and name != selected:
                    row["wasted_attempts"] += 1
                    if _is_num(attempt.get("duration_ms")):
                        row["wasted_ms"] += float(attempt["duration_ms"])
            else:
                row["failed"] += 1
            if _is_num(attempt.get("duration_ms")):
                row["durations"].append(float(attempt["duration_ms"]))
        if selected is not None:
            _tool(selected)["selected"] += 1
            for name in ran:
                _tool(name)["win_base"] += 1

    extractors = []
    for name, row in tool.items():
        extractors.append({
            "name": name,
            "attempts": row["attempts"], "succeeded": row["succeeded"],
            "failed": row["failed"], "selected": row["selected"],
            "win_rate_percent": (
                _round1(row["selected"] / row["win_base"] * 100) if row["win_base"] else None
            ),
            "avg_quality": _round1(_mean(row["quality_scores"])),
            "avg_duration_ms": int(round(_mean(row["durations"]))) if row["durations"] else None,
            "median_duration_ms": int(round(_median(row["durations"]))) if row["durations"] else None,
            "avg_chars": int(round(_mean(row["chars"]))) if row["chars"] else None,
            "wasted_attempts": row["wasted_attempts"],
            "wasted_ms": int(round(row["wasted_ms"])),
        })
    extractors.sort(key=lambda r: (-r["attempts"], r["name"]))

    # ---------------------------------------------------------------- #
    # Processing-time distribution & outliers (record-level durations)
    # One ~1-minute resume drags the mean far above the typical
    # 10-15 s, so alongside the mean this block carries the median,
    # p95, a per-resume series for charting, and Tukey outliers
    # (above Q3 + 1.5 x IQR; needs at least 4 points).
    # ---------------------------------------------------------------- #
    duration_points: list[dict[str, Any]] = []
    for record in in_range:
        if not _is_num(record.get("duration_ms")):
            continue
        duration_points.append({
            "resume_id": record.get("resume_id"),
            "filename": record.get("filename"),
            "uploaded_at": record.get("uploaded_at"),
            "duration_ms": int(round(float(record["duration_ms"]))),
            "extractor": _selected_extractor(record),
            "is_outlier": False,
        })
    duration_points.sort(key=lambda p: str(p.get("uploaded_at") or ""))
    duration_values = [float(p["duration_ms"]) for p in duration_points]
    outlier_threshold: float | None = None
    if len(duration_values) >= 4:
        q1 = _percentile(duration_values, 25)
        q3 = _percentile(duration_values, 75)
        if q1 is not None and q3 is not None:
            outlier_threshold = q3 + 1.5 * (q3 - q1)
            for point in duration_points:
                point["is_outlier"] = point["duration_ms"] > outlier_threshold
    durations_block = {
        "points": duration_points,
        "mean_ms": int(round(_mean(duration_values))) if duration_values else None,
        "median_ms": int(round(_median(duration_values))) if duration_values else None,
        "p95_ms": int(round(_percentile(duration_values, 95))) if duration_values else None,
        "max_ms": int(max(duration_values)) if duration_values else None,
        "outlier_threshold_ms": (
            int(round(outlier_threshold)) if outlier_threshold is not None else None
        ),
        "outlier_count": sum(1 for p in duration_points if p["is_outlier"]),
    }

    # ---------------------------------------------------------------- #
    # Quality
    # ---------------------------------------------------------------- #
    status_counts = Counter()
    filter_scores: dict[str, list[float]] = {name: [] for name in _FILTERS}
    histogram_counts = Counter()
    missed: Counter[str] = Counter()
    completeness_base = 0
    flag_counts: Counter[str] = Counter()

    for record in in_range:
        status = record.get("final_quality_status") or record.get("quality_status")
        if isinstance(status, str) and status.upper() in ("PASS", "WARNING", "FAIL"):
            status_counts[status.upper()] += 1
        quality = record.get("quality")
        if not isinstance(quality, dict):
            continue
        for name in _FILTERS:
            part = quality.get(name)
            if isinstance(part, dict):
                if _is_num(part.get("score")):
                    filter_scores[name].append(float(part["score"]))
                for flag in part.get("flags") or []:
                    if isinstance(flag, str):
                        flag_counts[flag] += 1
        headline = _headline(quality)
        if headline is not None:
            bucket = "0-49" if headline < 50 else "50-74" if headline < 75 \
                else "75-89" if headline < 90 else "90-100"
            histogram_counts[bucket] += 1
        completeness = quality.get("completeness")
        if isinstance(completeness, dict) and isinstance(completeness.get("missing_sections"), list):
            completeness_base += 1
            missing_set = set(completeness["missing_sections"])
            for section in _SECTIONS:
                if section in missing_set:
                    missed[section] += 1

    quality_block = {
        "pass": status_counts["PASS"], "warning": status_counts["WARNING"],
        "fail": status_counts["FAIL"],
        "filters_avg": {name: _round1(_mean(scores)) for name, scores in filter_scores.items()},
        "histogram": [{"bucket": b, "count": histogram_counts[b]}
                      for b in ("0-49", "50-74", "75-89", "90-100")],
        "missed_sections": [
            {"section": section, "missing": missed[section], "of": completeness_base,
             "percent": _round1(missed[section] / completeness_base * 100) if completeness_base else None}
            for section in sorted(_SECTIONS, key=lambda s: -missed[s])
        ],
        "top_flags": [{"flag": flag, "count": count}
                      for flag, count in flag_counts.most_common(8)],
    }

    # ---------------------------------------------------------------- #
    # Normalization
    # ---------------------------------------------------------------- #
    norm_applied = 0
    reductions: list[float] = []
    chars_removed = 0
    for record in in_range:
        norm = record.get("text_normalization") or {}
        if not isinstance(norm, dict):
            continue
        if norm.get("normalization_applied") is True:
            norm_applied += 1
        if _is_num(norm.get("estimated_reduction_percent")):
            reductions.append(float(norm["estimated_reduction_percent"]))
        orig, new = norm.get("original_chars"), norm.get("normalized_chars")
        if _is_num(orig) and _is_num(new):
            chars_removed += max(0, int(orig) - int(new))

    normalization_block = {
        "applied": norm_applied,
        "avg_reduction_percent": _round1(_mean(reductions)),
        "total_chars_removed": chars_removed,
        "estimated_tokens_saved": norm_tokens_saved,
    }

    # ---------------------------------------------------------------- #
    # AI Evaluation (evaluation/ package) — strictly separate from
    # the extraction metrics above: evaluation token usage lives on
    # the analysis entries (record["analyses"]) and is never merged
    # into the extraction token totals. Averages cover successful
    # analyses only; token totals sum only entries whose API call
    # actually reported usage.
    # ---------------------------------------------------------------- #
    ev_total = ev_succeeded = ev_failed = 0
    ev_invalid_json = 0
    ev_completeness: list[float] = []
    ev_quality: list[float] = []
    ev_role_counts: Counter[str] = Counter()
    ev_missing_counts: Counter[str] = Counter()
    ev_tok_in = ev_tok_out = ev_tok_total = 0
    ev_with_usage = 0
    ev_durations: list[float] = []
    ev_yaml_versions: set[str] = set()

    for record in in_range:
        entries = record.get("analyses")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            ev_total += 1
            if isinstance(entry.get("yaml_version"), str):
                ev_yaml_versions.add(entry["yaml_version"])
            if _is_num(entry.get("duration_ms")):
                ev_durations.append(float(entry["duration_ms"]))
            tokens = entry.get("tokens")
            if isinstance(tokens, dict):
                tin, tout, ttot = (tokens.get("input"),
                                   tokens.get("output"),
                                   tokens.get("total"))
                if _is_num(tin) or _is_num(tout) or _is_num(ttot):
                    ev_with_usage += 1
                    if _is_num(tin):
                        ev_tok_in += int(tin)
                    if _is_num(tout):
                        ev_tok_out += int(tout)
                    if _is_num(ttot):
                        ev_tok_total += int(ttot)
                    elif _is_num(tin) and _is_num(tout):
                        ev_tok_total += int(tin) + int(tout)
            if entry.get("status") == "success":
                ev_succeeded += 1
                evaluation = entry.get("evaluation")
                if isinstance(evaluation, dict):
                    for key, bucket in (("completeness", ev_completeness),
                                        ("quality", ev_quality)):
                        block = evaluation.get(key)
                        if isinstance(block, dict) and _is_num(block.get("score")):
                            bucket.append(float(block["score"]))
                    for role in evaluation.get("suitable_roles") or []:
                        if isinstance(role, str) and role:
                            ev_role_counts[role] += 1
                    for missing in evaluation.get("missing_sections") or []:
                        if isinstance(missing, dict):
                            section = missing.get("section")
                            if isinstance(section, str) and section:
                                ev_missing_counts[section] += 1
            elif entry.get("status") == "failed":
                ev_failed += 1
                error_text = str(entry.get("error") or "").lower()
                if "validation" in error_text or "json" in error_text:
                    ev_invalid_json += 1

    evaluation_block = {
        "total_evaluations": ev_total,
        "successful_evaluations": ev_succeeded,
        "failed_evaluations": ev_failed,
        "invalid_json_failures": ev_invalid_json,
        "avg_completeness_score": _round1(_mean(ev_completeness)),
        "avg_quality_score": _round1(_mean(ev_quality)),
        "most_recommended_roles": [
            {"role": role, "count": count}
            for role, count in ev_role_counts.most_common(8)],
        "most_missing_sections": [
            {"section": section, "count": count}
            for section, count in ev_missing_counts.most_common(8)],
        "tokens": {
            "input": ev_tok_in, "output": ev_tok_out,
            "total": ev_tok_total, "evaluations_with_usage": ev_with_usage,
        },
        "avg_duration_ms": (
            int(round(_mean(ev_durations))) if ev_durations else None),
        "yaml_versions_used": sorted(ev_yaml_versions),
    }

    return {
        "meta": {
            "generated_at": now_utc.isoformat().replace("+00:00", "Z"),
            "range_days": days,
            "records_in_range": total,
            "records_total": len([r for r in records if isinstance(r, dict)]),
            "records_without_timestamp": without_ts,
            "token_usage_coverage": {
                "calls_with_usage": calls_with_usage,
                "calls_total": calls,
            },
        },
        "kpis": kpis,
        "extractors": extractors,
        "durations": durations_block,
        "tokens": tokens_block,
        "quality": quality_block,
        "normalization": normalization_block,
        "evaluation": evaluation_block,
    }
