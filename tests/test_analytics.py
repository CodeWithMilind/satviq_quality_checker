"""Tests for the Analytics Phase 1 aggregation (api/analytics.py),
the /api/analytics/summary endpoint, and the frontend wiring.

Metric definitions follow SATVIQ_Analytics_Spec.md §6. Synthetic records
mirror the real stored-record schema (attempts, token_usage,
text_normalization, quality reports).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.analytics import compute_analytics
from api.main import app

ROOT = Path(__file__).resolve().parents[1]
client = TestClient(app)


def _attempt(name, *, success=True, selected=False, duration=1000, chars=4000,
             scores=(90.0, 80.0, 85.0)):
    report = None
    if scores is not None:
        report = {
            "text_health": {"score": scores[0]},
            "completeness": {"score": scores[1]},
            "reading_order": {"score": scores[2]},
        }
    return {
        "extractor_name": name, "success": success,
        "is_final_selected": selected, "duration_ms": duration,
        "extracted_text_length": chars, "quality_checker_report": report,
        "error": None if success else "boom",
    }


def _record(**overrides):
    record = {
        "resume_id": "r-1", "filename": "a.pdf", "file_type": "pdf",
        "success": True, "extractor_used": "tika", "duration_ms": 2000,
        "uploaded_at": "2026-11-06T05:00:00+00:00",
        "fallback_occurred": False,
        "quality_status": "PASS", "final_quality_status": "PASS",
        "quality": {
            "text_health": {"score": 90.0, "flags": []},
            "completeness": {"score": 80.0, "flags": ["MISSING_OPTIONAL_SECTION"],
                             "missing_sections": ["Projects"],
                             "detected_sections": ["Skills"]},
            "reading_order": {"score": 85.0, "flags": []},
        },
        "attempts": [
            _attempt("tika", selected=True, duration=1500),
            _attempt("pymupdf", duration=300, chars=3900),
        ],
        "token_usage": {
            "usage_available": True, "api_calls": 1, "successful_calls": 1,
            "failed_calls": 0, "timed_out_calls": 0,
            "input_tokens": 1000, "output_tokens": 200,
            "total_tokens": 1200, "cached_input_tokens": 0,
        },
        "text_normalization": {
            "normalization_applied": True, "estimated_reduction_percent": 10.0,
            "original_chars": 1100, "normalized_chars": 1000,
            "original_estimated_tokens": 300, "normalized_estimated_tokens": 250,
        },
    }
    record.update(overrides)
    return record


def test_empty_records():
    out = compute_analytics([])
    assert out["meta"]["records_in_range"] == 0
    assert out["kpis"]["total_processed"] == 0
    assert out["kpis"]["success_rate_percent"] is None
    assert out["kpis"]["avg_quality_score"] is None
    assert out["kpis"]["total_tokens"] == 0
    assert out["extractors"] == []
    assert out["tokens"]["by_day"] == []
    assert out["tokens"]["avg_per_resume"] is None
    assert out["quality"]["missed_sections"][0]["of"] == 0


def test_single_record_aggregation():
    out = compute_analytics([_record()])
    k = out["kpis"]
    assert k["total_processed"] == 1
    assert k["success_rate_percent"] == 100.0
    assert k["avg_quality_score"] == 85.0  # mean of 90/80/85
    assert k["total_tokens"] == 1200
    assert k["fallback_rate_percent"] == 0.0 and k["fallback_base"] == 1

    rows = {r["name"]: r for r in out["extractors"]}
    assert rows["tika"]["selected"] == 1
    assert rows["tika"]["win_rate_percent"] == 100.0
    assert rows["tika"]["avg_quality"] == 85.0
    assert rows["pymupdf"]["win_rate_percent"] == 0.0
    assert rows["pymupdf"]["wasted_attempts"] == 1
    assert rows["pymupdf"]["wasted_ms"] == 300

    t = out["tokens"]
    assert (t["input"], t["output"], t["total"]) == (1000, 200, 1200)
    assert t["avg_per_resume"] == 1200.0 and t["p95_per_resume"] == 1200.0
    assert t["normalization_tokens_saved"] == 50

    q = out["quality"]
    assert (q["pass"], q["warning"], q["fail"]) == (1, 0, 0)
    assert q["filters_avg"]["completeness"] == 80.0
    missed = {m["section"]: m for m in q["missed_sections"]}
    assert missed["Projects"]["missing"] == 1
    assert missed["Projects"]["percent"] == 100.0
    assert missed["Skills"]["missing"] == 0
    assert q["top_flags"] == [{"flag": "MISSING_OPTIONAL_SECTION", "count": 1}]

    n = out["normalization"]
    assert n["applied"] == 1 and n["total_chars_removed"] == 100
    assert n["estimated_tokens_saved"] == 50


def test_token_coverage_excludes_unavailable_usage():
    record = _record(token_usage={
        "usage_available": False, "api_calls": 1, "successful_calls": 0,
        "failed_calls": 0, "timed_out_calls": 1,
        "input_tokens": None, "output_tokens": None, "total_tokens": None,
        "cached_input_tokens": None,
    })
    out = compute_analytics([record])
    assert out["kpis"]["total_tokens"] == 0
    assert out["tokens"]["avg_per_resume"] is None
    assert out["tokens"]["timed_out_calls"] == 1
    cov = out["meta"]["token_usage_coverage"]
    assert cov == {"calls_with_usage": 0, "calls_total": 1}


def test_total_tokens_derived_from_input_output_when_total_missing():
    record = _record()
    record["token_usage"] = dict(record["token_usage"], total_tokens=None)
    out = compute_analytics([record])
    assert out["tokens"]["total"] == 1200


def test_win_rate_denominator_counts_only_records_with_selection():
    no_selection = _record(resume_id="r-2", extractor_used=None,
                           attempts=[_attempt("tika"), _attempt("pymupdf")])
    for attempt in no_selection["attempts"]:
        attempt["is_final_selected"] = False
    out = compute_analytics([_record(), no_selection])
    rows = {r["name"]: r for r in out["extractors"]}
    # tika ran in both records but a selection exists in only one.
    assert rows["tika"]["attempts"] == 2
    assert rows["tika"]["win_rate_percent"] == 100.0


def test_range_filter_and_missing_timestamps():
    now = datetime.now(timezone.utc)
    recent = _record(resume_id="recent",
                     uploaded_at=(now - timedelta(days=1)).isoformat())
    old = _record(resume_id="old",
                  uploaded_at=(now - timedelta(days=40)).isoformat())
    no_ts = _record(resume_id="no-ts", uploaded_at=None)
    out_all = compute_analytics([recent, old, no_ts])
    assert out_all["meta"]["records_in_range"] == 3
    assert out_all["meta"]["records_without_timestamp"] == 1
    out_7d = compute_analytics([recent, old, no_ts], days=7)
    assert out_7d["meta"]["records_in_range"] == 1
    assert out_7d["meta"]["range_days"] == 7


def test_by_day_buckets_use_ist():
    # 20:30 UTC = 02:00 IST the next day.
    record = _record(uploaded_at="2026-11-05T20:30:00+00:00")
    out = compute_analytics([record])
    days = {d["date"]: d for d in out["tokens"]["by_day"]}
    assert days["2026-11-06"]["resumes"] == 1
    assert days["2026-11-06"]["tokens"] == 1200


def test_manual_runs_are_excluded_from_pipeline_stats():
    record = _record()
    record["extractor_results"] = {
        "docling": {"status": "completed", "extracted_text": "x" * 5000,
                    "quality_score": 99.0, "duration_ms": 40000},
    }
    out = compute_analytics([record], include_manual=True)  # Phase 1: still excluded
    names = {r["name"] for r in out["extractors"]}
    assert "docling" not in names


def test_fallback_rate_none_without_field():
    record = _record()
    del record["fallback_occurred"]
    out = compute_analytics([record])
    assert out["kpis"]["fallback_rate_percent"] is None
    assert out["kpis"]["fallback_base"] == 0


def test_record_without_quality_is_excluded_from_score_averages():
    record = _record(quality=None, quality_status=None, final_quality_status=None)
    out = compute_analytics([record])
    assert out["kpis"]["avg_quality_score"] is None
    assert out["quality"]["missed_sections"][0]["of"] == 0
    assert out["kpis"]["total_processed"] == 1  # still counted as processed


def test_endpoint_summary_shape():
    response = client.get("/api/analytics/summary")
    assert response.status_code == 200
    body = response.json()
    for key in ("meta", "kpis", "extractors", "tokens", "quality", "normalization"):
        assert key in body
    ranged = client.get("/api/analytics/summary", params={"days": 30})
    assert ranged.status_code == 200
    assert ranged.json()["meta"]["range_days"] == 30


def test_stats_endpoint_untouched_shape():
    response = client.get("/api/stats")
    assert response.status_code == 200
    body = response.json()
    for key in ("total_processed", "successful_extractions", "failed_extractions",
                "quality_pass", "quality_warning", "quality_fail",
                "quality_pass_rate_percent", "recent_activity"):
        assert key in body


def test_frontend_wiring():
    index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    api_js = (ROOT / "frontend" / "js" / "api.js").read_text(encoding="utf-8")
    app_js = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
    # Nav order: History < Analytics < Settings.
    assert index.index('data-nav="history"') < index.index('data-nav="analytics"') \
        < index.index('data-nav="settings"')
    assert 'id="view-analytics"' in index
    assert "getAnalytics" in api_js and "/api/analytics/summary" in api_js
    assert "renderAnalytics" in app_js and '"analytics"' in app_js
    assert ".kpi-card" in css


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
