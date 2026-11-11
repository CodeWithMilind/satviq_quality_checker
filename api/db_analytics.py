"""PostgreSQL-driven Analytics (persistent analytics architecture).

Builds the SAME summary shape as api.analytics.compute_analytics,
but every number is aggregated from PostgreSQL: the relational
tables (resume_processing_runs, candidate_extraction_attempts,
candidate_section_analysis, candidate_evaluations, ai_api_costs)
and, for the few blocks that exist only as record JSON (quality
filter flags, normalization stats, token coverage), SQL jsonb
aggregation over candidates.pipeline_record.

Semantics mirror the Python implementation (latest processing run
per resume for per-resume metrics; extraction attempts accumulate
across runs as history; missing data stays null, never faked as
zero). Returns None when PostgreSQL is not configured/reachable —
the caller falls back to the record-based computation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import db as _db

_IST = timezone(timedelta(hours=5, minutes=30))
_FILTERS = ("text_health", "completeness", "reading_order")


def _r1(value: Any) -> float | None:
    return None if value is None else round(float(value), 1)


def _i(value: Any) -> int | None:
    return None if value is None else int(round(float(value)))


def analytics_summary(days: int | None = None) -> dict | None:
    if not _db.database_url():
        return None
    try:
        from .ai_costs import jsonable
        return jsonable(_build(days))
    except Exception as exc:  # noqa: BLE001
        _db.logger.warning("SQL analytics failed: %s", exc)
        return None


def _build(days: int | None) -> dict:
    from psycopg.rows import dict_row

    cutoff = None
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with _db.connect() as conn, conn.cursor(
            row_factory=dict_row) as cur:
        # --- range cohort -------------------------------------------
        cur.execute(
            "CREATE TEMP TABLE _in_range ON COMMIT DROP AS"
            " SELECT id, resume_id, pipeline_record,"
            " (pipeline_record->>'uploaded_at')::timestamptz"
            "   AS uploaded_at"
            " FROM candidates"
            " WHERE (%s::timestamptz IS NULL OR"
            "        (pipeline_record->>'uploaded_at')::timestamptz"
            "        >= %s::timestamptz)", (cutoff, cutoff))
        cur.execute(
            "CREATE TEMP TABLE _latest_runs ON COMMIT DROP AS"
            " SELECT DISTINCT ON (r.resume_id) r.*"
            " FROM resume_processing_runs r"
            " JOIN _in_range c ON c.id = r.candidate_id"
            " ORDER BY r.resume_id, r.processed_at DESC NULLS LAST,"
            " r.id DESC")
        cur.execute("SELECT COUNT(*) AS n FROM _in_range")
        total = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM candidates")
        records_total = cur.fetchone()["n"]
        cur.execute(
            "SELECT COUNT(*) AS n FROM _in_range"
            " WHERE uploaded_at IS NULL")
        without_ts = cur.fetchone()["n"]
        cur.execute(
            "SELECT COALESCE(SUM((pipeline_record->'token_usage'"
            " ->>'api_calls')::int), 0) AS calls_total,"
            " COALESCE(SUM(CASE WHEN pipeline_record->'token_usage'"
            " ->>'usage_available' = 'true'"
            " THEN (pipeline_record->'token_usage'->>'api_calls')"
            " ::int ELSE 0 END), 0) AS calls_with_usage"
            " FROM _in_range")
        coverage = cur.fetchone()

        # --- KPIs (latest run per resume) -----------------------------
        cur.execute(
            "SELECT COUNT(*) AS runs,"
            " AVG(CASE WHEN success THEN 1.0 ELSE 0.0 END)"
            "   AS success_rate,"
            " AVG(selected_quality_score) AS avg_quality,"
            " AVG(duration_ms) AS avg_dur,"
            " percentile_cont(0.5) WITHIN GROUP"
            "   (ORDER BY duration_ms) AS med_dur,"
            " percentile_cont(0.95) WITHIN GROUP"
            "   (ORDER BY duration_ms) AS p95_dur,"
            " AVG(CASE WHEN fallback_occurred THEN 1.0 ELSE 0.0 END)"
            "   AS fallback_rate,"
            " COUNT(fallback_occurred) AS fallback_base"
            " FROM _latest_runs")
        k = cur.fetchone()
        cur.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS total"
            " FROM ai_api_costs a JOIN _in_range c"
            "   ON c.resume_id = a.resume_id"
            " WHERE a.operation_type = 'json_extraction'")
        extraction_tokens = cur.fetchone()["total"]
        kpis = {
            "total_processed": total,
            "success_rate_percent": _r1(
                k["success_rate"] * 100 if k["success_rate"]
                is not None else None),
            "avg_quality_score": _r1(k["avg_quality"]),
            "avg_duration_ms": _i(k["avg_dur"]),
            "median_duration_ms": _i(k["med_dur"]),
            "p95_duration_ms": _i(k["p95_dur"]),
            "total_tokens": int(extraction_tokens),
            "fallback_rate_percent": _r1(
                k["fallback_rate"] * 100 if k["fallback_rate"]
                is not None else None),
            "fallback_base": int(k["fallback_base"] or 0),
        }

        # --- extractor performance (all historical attempts) --------
        cur.execute(
            "SELECT lower(a.extractor) AS name, COUNT(*) AS attempts,"
            " COUNT(*) FILTER (WHERE a.success) AS succeeded,"
            " COUNT(*) FILTER (WHERE NOT COALESCE(a.success,false)"
            "   AND NOT COALESCE(a.skipped,false)) AS failed,"
            " COUNT(*) FILTER (WHERE a.selected) AS selected,"
            " COUNT(DISTINCT a.run_id) AS win_base,"
            " AVG(a.quality_score) FILTER (WHERE a.success)"
            "   AS avg_quality,"
            " AVG(a.duration_ms) AS avg_dur,"
            " percentile_cont(0.5) WITHIN GROUP"
            "   (ORDER BY a.duration_ms) AS med_dur,"
            " AVG(a.char_count) FILTER (WHERE a.success) AS avg_chars,"
            " COUNT(*) FILTER (WHERE a.success AND NOT a.selected"
            "   AND a.run_id IN (SELECT run_id"
            "     FROM candidate_extraction_attempts"
            "     WHERE selected)) AS wasted_attempts,"
            " COALESCE(SUM(a.duration_ms) FILTER"
            "   (WHERE a.success AND NOT a.selected"
            "   AND a.run_id IN (SELECT run_id"
            "     FROM candidate_extraction_attempts"
            "     WHERE selected)), 0) AS wasted_ms"
            " FROM candidate_extraction_attempts a"
            " JOIN _in_range c ON c.id = a.candidate_id"
            " WHERE a.extractor IS NOT NULL"
            " GROUP BY lower(a.extractor) ORDER BY attempts DESC")
        extractors = [{
            "name": r["name"], "attempts": r["attempts"],
            "succeeded": r["succeeded"], "failed": r["failed"],
            "selected": r["selected"],
            "win_rate_percent": _r1(
                r["selected"] / r["win_base"] * 100
                if r["win_base"] else None),
            "avg_quality": _r1(r["avg_quality"]),
            "avg_duration_ms": _i(r["avg_dur"]),
            "median_duration_ms": _i(r["med_dur"]),
            "avg_chars": _i(r["avg_chars"]),
            "wasted_attempts": r["wasted_attempts"],
            "wasted_ms": int(r["wasted_ms"] or 0),
        } for r in cur.fetchall()]

        # --- durations block (points + Tukey outliers) --------------
        cur.execute(
            "SELECT resume_id, filename, uploaded_at, duration_ms,"
            " final_extractor FROM _latest_runs"
            " WHERE duration_ms IS NOT NULL ORDER BY uploaded_at")
        points = [{
            "resume_id": r["resume_id"], "filename": r["filename"],
            "uploaded_at": r["uploaded_at"].isoformat()
            if r["uploaded_at"] else None,
            "duration_ms": int(r["duration_ms"]),
            "extractor": r["final_extractor"], "is_outlier": False,
        } for r in cur.fetchall()]
        durations_block = _durations_block(points)

        blocks = {
            "meta": {
                "generated_at": datetime.now(timezone.utc)
                .isoformat().replace("+00:00", "Z"),
                "range_days": days,
                "records_in_range": total,
                "records_total": records_total,
                "records_without_timestamp": without_ts,
                "token_usage_coverage": {
                    "calls_with_usage": int(
                        coverage["calls_with_usage"] or 0),
                    "calls_total": int(coverage["calls_total"] or 0),
                },
                "source": "postgresql",
            },
            "kpis": kpis, "extractors": extractors,
            "durations": durations_block,
        }
        blocks.update(_tokens_and_costs(cur))
        qn = _quality_and_normalization(cur)
        blocks["tokens"]["normalization_tokens_saved"] = \
            qn.pop("_norm_tokens_saved")
        blocks.update(qn)
        blocks["evaluation"] = _evaluation_block(cur)
        return blocks


def _durations_block(points: list[dict]) -> dict:
    values = sorted(p["duration_ms"] for p in points)

    def pct(p: float):
        if not values:
            return None
        idx = (len(values) - 1) * p
        lo, hi = int(idx), min(int(idx) + 1, len(values) - 1)
        return values[lo] + (values[hi] - values[lo]) * (idx - lo)

    threshold = None
    if len(values) >= 4:
        q1, q3 = pct(0.25), pct(0.75)
        threshold = q3 + 1.5 * (q3 - q1)
        for p in points:
            p["is_outlier"] = p["duration_ms"] > threshold
    return {
        "points": points,
        "mean_ms": _i(sum(values) / len(values)) if values else None,
        "median_ms": _i(pct(0.5)) if values else None,
        "p95_ms": _i(pct(0.95)) if values else None,
        "max_ms": max(values) if values else None,
        "outlier_threshold_ms": _i(threshold),
        "outlier_count": sum(1 for p in points if p["is_outlier"]),
    }


def _tokens_and_costs(cur) -> dict:
    """Tokens block + ai_costs block + llm block, all from
    ai_api_costs joined to the in-range cohort."""
    cur.execute(
        "SELECT COALESCE(SUM(input_tokens),0) AS inp,"
        " COALESCE(SUM(output_tokens),0) AS outp,"
        " COALESCE(SUM(cached_input_tokens),0) AS cached,"
        " COALESCE(SUM(total_tokens),0) AS total,"
        " COUNT(*) AS calls,"
        " COUNT(*) FILTER (WHERE status='success') AS ok,"
        " COUNT(*) FILTER (WHERE status='failed') AS failed,"
        " COUNT(*) FILTER (WHERE status='timeout') AS timed_out"
        " FROM ai_api_costs a JOIN _in_range c"
        "   ON c.resume_id = a.resume_id"
        " WHERE a.operation_type = 'json_extraction'")
    t = cur.fetchone()
    cur.execute(
        "SELECT percentile_cont(0.95) WITHIN GROUP"
        " (ORDER BY per.total) AS p95, AVG(per.total) AS avg"
        " FROM (SELECT a.resume_id, SUM(a.total_tokens) AS total"
        " FROM ai_api_costs a JOIN _in_range c"
        "   ON c.resume_id = a.resume_id"
        " WHERE a.operation_type = 'json_extraction'"
        " GROUP BY a.resume_id) per")
    per = cur.fetchone()
    cur.execute(
        "SELECT COALESCE(SUM(char_count),0) AS chars"
        " FROM candidate_extraction_attempts a"
        " JOIN _in_range c ON c.id = a.candidate_id"
        " WHERE a.selected")
    selected_chars = cur.fetchone()["chars"] or 0
    cur.execute(
        "SELECT (c.uploaded_at AT TIME ZONE 'Asia/Kolkata')::date"
        "   AS day,"
        " COALESCE(SUM(a.total_tokens),0) AS tokens"
        " FROM _in_range c LEFT JOIN ai_api_costs a"
        "   ON a.resume_id = c.resume_id"
        "  AND a.operation_type = 'json_extraction'"
        " WHERE c.uploaded_at IS NOT NULL GROUP BY 1")
    token_days = {str(r["day"]): int(r["tokens"])
                  for r in cur.fetchall()}
    cur.execute(
        "SELECT (uploaded_at AT TIME ZONE 'Asia/Kolkata')::date"
        " AS day, COUNT(*) AS n FROM _in_range"
        " WHERE uploaded_at IS NOT NULL GROUP BY 1")
    resume_days = {str(r["day"]): int(r["n"]) for r in cur.fetchall()}
    by_day = [{"date": d, "tokens": token_days.get(d, 0),
               "resumes": resume_days[d]}
              for d in sorted(resume_days)]
    tokens_block = {
        "input": int(t["inp"]), "output": int(t["outp"]),
        "cached_input": int(t["cached"]), "total": int(t["total"]),
        "api_calls": int(t["calls"]),
        "successful_calls": int(t["ok"]),
        "failed_calls": int(t["failed"]),
        "timed_out_calls": int(t["timed_out"]),
        "avg_per_resume": _r1(per["avg"]),
        "p95_per_resume": _r1(per["p95"]),
        "tokens_per_1000_chars": _r1(
            t["total"] / (selected_chars / 1000)
            if selected_chars else None),
        "normalization_tokens_saved": 0,  # filled below (jsonb)
        "by_day": by_day,
    }

    # --- costs + LLM analytics --------------------------------------
    cur.execute(
        "SELECT operation_type, COUNT(*) AS calls,"
        " COALESCE(SUM(total_cost_usd),0) AS cost_usd,"
        " COALESCE(SUM(total_cost_inr),0) AS cost_inr,"
        " COALESCE(SUM(input_tokens),0) AS inp,"
        " COALESCE(SUM(cached_input_tokens),0) AS cached,"
        " COALESCE(SUM(output_tokens),0) AS outp,"
        " COALESCE(SUM(reasoning_tokens),0) AS reasoning,"
        " COALESCE(SUM(total_tokens),0) AS total"
        " FROM ai_api_costs a JOIN _in_range c"
        "   ON c.resume_id = a.resume_id"
        " GROUP BY operation_type")
    by_op = {r["operation_type"]: dict(r) for r in cur.fetchall()}
    cur.execute(
        "SELECT model, COUNT(*) AS calls,"
        " COALESCE(SUM(total_tokens),0) AS total,"
        " COALESCE(SUM(total_cost_usd),0) AS cost_usd"
        " FROM ai_api_costs a JOIN _in_range c"
        "   ON c.resume_id = a.resume_id"
        " GROUP BY model ORDER BY total DESC")
    by_model = [dict(r) for r in cur.fetchall()]
    cur.execute(
        "SELECT COUNT(DISTINCT a.resume_id) AS n,"
        " COALESCE(SUM(total_cost_usd),0) AS usd,"
        " COALESCE(SUM(total_cost_inr),0) AS inr"
        " FROM ai_api_costs a JOIN _in_range c"
        "   ON c.resume_id = a.resume_id")
    totals = cur.fetchone()
    cur.execute(
        "SELECT a.resume_id, SUM(a.total_cost_usd) AS cost_usd,"
        " (SELECT c2.pipeline_record->>'filename' FROM candidates c2"
        "  WHERE c2.resume_id = a.resume_id) AS filename"
        " FROM ai_api_costs a JOIN _in_range c"
        "   ON c.resume_id = a.resume_id"
        " GROUP BY a.resume_id ORDER BY cost_usd DESC NULLS LAST"
        " LIMIT 1")
    top = cur.fetchone()
    cur.execute(
        "SELECT to_char(date_trunc('month', a.created_at),"
        " 'YYYY-MM') AS month, SUM(a.total_cost_usd) AS cost_usd,"
        " SUM(a.total_cost_inr) AS cost_inr, COUNT(*) AS calls"
        " FROM ai_api_costs a JOIN _in_range c"
        "   ON c.resume_id = a.resume_id"
        " GROUP BY 1 ORDER BY 1")
    monthly = [dict(r) for r in cur.fetchall()]
    resumes_with = int(totals["n"] or 0)
    total_usd = totals["usd"]
    ai_costs = {
        "source": "postgresql",
        "total_cost_usd": total_usd,
        "total_cost_inr": totals["inr"],
        "average_cost_per_resume_usd": (
            total_usd / resumes_with if resumes_with else None),
        "resumes_with_costs": resumes_with,
        "json_extraction": by_op.get("json_extraction"),
        "ai_evaluation": by_op.get("ai_evaluation"),
        "json_correction": by_op.get("json_correction"),
        "by_model": by_model,
        "most_expensive_resume": dict(top) if top else None,
        "monthly": monthly,
    }
    all_tokens = sum(int(v["total"]) for v in by_op.values())
    corr = by_op.get("json_correction") or {}
    evals = by_op.get("ai_evaluation") or {}
    llm = {
        "total_input_tokens": sum(int(v["inp"])
                                  for v in by_op.values()),
        "total_output_tokens": sum(int(v["outp"])
                                   for v in by_op.values()),
        "total_reasoning_tokens": sum(int(v["reasoning"])
                                      for v in by_op.values()),
        "total_tokens": all_tokens,
        "by_operation": by_op,
        "by_model": by_model,
        "avg_tokens_per_resume": _r1(
            all_tokens / resumes_with if resumes_with else None),
        "json_correction": {
            "calls": int(corr.get("calls") or 0),
            "cost_usd": corr.get("cost_usd"),
            "frequency_percent": _r1(
                (corr.get("calls") or 0)
                / max(1, (evals.get("calls") or 0)
                      + (corr.get("calls") or 0)) * 100),
        },
    }
    return {"tokens": tokens_block, "ai_costs": ai_costs, "llm": llm}


def _quality_and_normalization(cur) -> dict:
    cur.execute(
        "SELECT quality_status,"
        " COUNT(*) AS n FROM _latest_runs"
        " WHERE quality_status IS NOT NULL"
        " GROUP BY quality_status")
    status_counts = {"PASS": 0, "WARNING": 0, "FAIL": 0}
    for r in cur.fetchall():
        key = str(r["quality_status"]).upper()
        if key in status_counts:
            status_counts[key] = int(r["n"])
    cur.execute(
        "SELECT AVG(qc_text_health_score) AS th,"
        " AVG(qc_completeness_score) AS comp,"
        " AVG(qc_reading_order_score) AS ro,"
        " COUNT(*) FILTER (WHERE selected_quality_score < 50)"
        "   AS b0,"
        " COUNT(*) FILTER (WHERE selected_quality_score >= 50"
        "   AND selected_quality_score < 75) AS b1,"
        " COUNT(*) FILTER (WHERE selected_quality_score >= 75"
        "   AND selected_quality_score < 90) AS b2,"
        " COUNT(*) FILTER (WHERE selected_quality_score >= 90)"
        "   AS b3 FROM _latest_runs")
    f = cur.fetchone()
    cur.execute(
        "SELECT section, COUNT(*) AS missing FROM"
        " candidate_section_analysis s JOIN _in_range c"
        "   ON c.id = s.candidate_id"
        " WHERE s.source = 'qc' AND s.finding = 'not_detected'"
        " GROUP BY section ORDER BY missing DESC")
    missed = cur.fetchall()
    cur.execute(
        "SELECT COUNT(DISTINCT candidate_id) AS n FROM"
        " candidate_section_analysis s JOIN _in_range c"
        "   ON c.id = s.candidate_id WHERE s.source = 'qc'")
    qc_base = cur.fetchone()["n"] or 0
    cur.execute(
        "SELECT flag, COUNT(*) AS n FROM _in_range c,"
        " jsonb_each(CASE WHEN jsonb_typeof("
        "   c.pipeline_record->'quality') = 'object'"
        "  THEN c.pipeline_record->'quality' ELSE '{}'::jsonb END)"
        "  AS f(key, val),"
        " jsonb_array_elements_text(CASE WHEN"
        "  jsonb_typeof(f.val->'flags') = 'array'"
        "  THEN f.val->'flags' ELSE '[]'::jsonb END) AS flag"
        " GROUP BY flag ORDER BY n DESC LIMIT 8")
    flags = [{"flag": r["flag"], "count": int(r["n"])}
             for r in cur.fetchall()]
    quality_block = {
        "pass": status_counts["PASS"],
        "warning": status_counts["WARNING"],
        "fail": status_counts["FAIL"],
        "filters_avg": {
            "text_health": _r1(f["th"]), "completeness": _r1(f["comp"]),
            "reading_order": _r1(f["ro"])},
        "histogram": [
            {"bucket": "0-49", "count": int(f["b0"] or 0)},
            {"bucket": "50-74", "count": int(f["b1"] or 0)},
            {"bucket": "75-89", "count": int(f["b2"] or 0)},
            {"bucket": "90-100", "count": int(f["b3"] or 0)}],
        "missed_sections": [
            {"section": r["section"], "missing": int(r["missing"]),
             "of": qc_base,
             "percent": _r1(r["missing"] / qc_base * 100)
             if qc_base else None}
            for r in missed],
        "top_flags": flags,
    }
    cur.execute(
        "SELECT COUNT(*) FILTER (WHERE pipeline_record->"
        " 'text_normalization'->>'normalization_applied' = 'true')"
        "   AS applied,"
        " AVG((pipeline_record->'text_normalization'"
        "   ->>'estimated_reduction_percent')::float)"
        "   AS avg_reduction,"
        " COALESCE(SUM(GREATEST(0,"
        "   (pipeline_record->'text_normalization'"
        "   ->>'original_chars')::int"
        "  - (pipeline_record->'text_normalization'"
        "   ->>'normalized_chars')::int)), 0) AS chars_removed,"
        " COALESCE(SUM((pipeline_record->'text_normalization'"
        "   ->>'original_estimated_tokens')::int"
        "  - (pipeline_record->'text_normalization'"
        "   ->>'normalized_estimated_tokens')::int), 0)"
        "   AS tokens_saved"
        " FROM _in_range WHERE jsonb_typeof(pipeline_record->"
        " 'text_normalization') = 'object'")
    n = cur.fetchone()
    normalization_block = {
        "applied": int(n["applied"] or 0),
        "avg_reduction_percent": _r1(n["avg_reduction"]),
        "total_chars_removed": int(n["chars_removed"] or 0),
        "estimated_tokens_saved": int(n["tokens_saved"] or 0),
    }
    return {"quality": quality_block,
            "normalization": normalization_block,
            "_norm_tokens_saved": int(n["tokens_saved"] or 0)}


def _evaluation_block(cur) -> dict:
    cur.execute(
        "SELECT COUNT(*) AS total,"
        " COUNT(*) FILTER (WHERE status='success') AS ok,"
        " COUNT(*) FILTER (WHERE status='failed') AS failed,"
        " COUNT(*) FILTER (WHERE status='failed' AND"
        "  (error ILIKE '%%json%%' OR error ILIKE '%%validation%%'))"
        "   AS invalid_json,"
        " AVG(completeness_score) FILTER (WHERE status='success')"
        "   AS avg_comp,"
        " AVG(quality_score) FILTER (WHERE status='success')"
        "   AS avg_qual,"
        " COALESCE(SUM(input_tokens) FILTER"
        "  (WHERE total_tokens IS NOT NULL),0) AS tok_in,"
        " COALESCE(SUM(output_tokens) FILTER"
        "  (WHERE total_tokens IS NOT NULL),0) AS tok_out,"
        " COALESCE(SUM(total_tokens),0) AS tok_total,"
        " COUNT(*) FILTER (WHERE total_tokens IS NOT NULL)"
        "   AS with_usage,"
        " AVG(duration_ms) AS avg_dur"
        " FROM candidate_evaluations e JOIN _in_range c"
        "   ON c.id = e.candidate_id")
    e = cur.fetchone()
    cur.execute(
        "SELECT role, COUNT(*) AS n FROM candidate_evaluations e"
        " JOIN _in_range c ON c.id = e.candidate_id,"
        " jsonb_array_elements_text(CASE WHEN jsonb_typeof("
        "  e.evaluation->'suitable_roles') = 'array'"
        "  THEN e.evaluation->'suitable_roles'"
        "  ELSE '[]'::jsonb END) AS role"
        " WHERE e.status = 'success'"
        " GROUP BY role ORDER BY n DESC LIMIT 8")
    roles = [{"role": r["role"], "count": int(r["n"])}
             for r in cur.fetchall()]
    cur.execute(
        "SELECT sec->>'section' AS section, COUNT(*) AS n FROM"
        " candidate_evaluations e JOIN _in_range c"
        "   ON c.id = e.candidate_id,"
        " jsonb_array_elements(CASE WHEN jsonb_typeof("
        "  e.evaluation->'missing_sections') = 'array'"
        "  THEN e.evaluation->'missing_sections'"
        "  ELSE '[]'::jsonb END) AS sec"
        " WHERE e.status = 'success'"
        " GROUP BY 1 ORDER BY n DESC LIMIT 8")
    missing = [{"section": r["section"], "count": int(r["n"])}
               for r in cur.fetchall()]
    cur.execute(
        "SELECT DISTINCT yaml_version FROM candidate_evaluations e"
        " JOIN _in_range c ON c.id = e.candidate_id"
        " WHERE yaml_version IS NOT NULL")
    yamls = sorted(r["yaml_version"] for r in cur.fetchall())
    return {
        "total_evaluations": int(e["total"] or 0),
        "successful_evaluations": int(e["ok"] or 0),
        "failed_evaluations": int(e["failed"] or 0),
        "invalid_json_failures": int(e["invalid_json"] or 0),
        "avg_completeness_score": _r1(e["avg_comp"]),
        "avg_quality_score": _r1(e["avg_qual"]),
        "most_recommended_roles": roles,
        "most_missing_sections": missing,
        "tokens": {"input": int(e["tok_in"] or 0),
                   "output": int(e["tok_out"] or 0),
                   "total": int(e["tok_total"] or 0),
                   "evaluations_with_usage": int(
                       e["with_usage"] or 0)},
        "avg_duration_ms": _i(e["avg_dur"]),
        "yaml_versions_used": yamls,
    }
