"""Per-resume AI cost tracking tests (muse_ai/pricing + api/ai_costs).

Covers the spec's list: input/cached/output/combined math, resume
totals, multiple calls, retries, failed-with-usage calls, FX
conversion with the rate stamped per row, historical retrieval via
the API, and honest unavailability (no usage -> no rows; unknown
model -> cost None, never $0).
"""

from __future__ import annotations

import json
import os
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("SATVIQ_TEST_DISK_STORE", "1")

from api.ai_costs import derive_call_rows, summarize_rows  # noqa: E402
from muse_ai.pricing import compute_cost, pricing_for  # noqa: E402

MODEL = "muse-spark-1.3-contributor"


# --- pricing math ---------------------------------------------------------
def test_input_output_cost_math_is_exact_decimal():
    cost = compute_cost(1_000_000, 0, 1_000_000, MODEL)
    assert cost["input_cost_usd"] == Decimal("0.10")
    assert cost["output_cost_usd"] == Decimal("0.20")
    assert cost["total_cost_usd"] == Decimal("0.30")


def test_cached_tokens_billed_once_at_cached_rate():
    cost = compute_cost(1_000_000, 400_000, 0, MODEL)
    assert cost["non_cached_input_tokens"] == 600_000
    assert cost["input_cost_usd"] == Decimal("0.06")
    assert cost["cached_input_cost_usd"] == Decimal("0.0008")
    assert cost["total_cost_usd"] == Decimal("0.0608")


def test_unknown_model_or_missing_usage_is_unavailable_not_zero():
    assert compute_cost(100, 0, 100, "no-such-model") is None
    assert compute_cost(None, 0, 100, MODEL) is None
    assert pricing_for(None) is None


# --- derivation -------------------------------------------------------------
def _record():
    return {
        "resume_id": "cost-test-00000001", "filename": "x.pdf",
        "muse_requests": [
            {"stage": "resume_json", "model": MODEL, "status": "success",
             "usage_available": True, "input_tokens": 1536,
             "cached_input_tokens": 0, "output_tokens": 2535,
             "total_tokens": 4071, "duration_ms": 900,
             "request_id": "req-1"},
            {"stage": "vision", "model": MODEL, "status": "failed",
             "usage_available": True, "input_tokens": 2000,
             "cached_input_tokens": 500, "output_tokens": 10,
             "total_tokens": 2010, "duration_ms": 300,
             "request_id": "req-2"},
            {"stage": "resume_json", "model": MODEL, "status": "failed",
             "usage_available": False, "input_tokens": None,
             "output_tokens": None, "total_tokens": None},
        ],
        "analyses": [{
            "analysis_id": "an-1", "status": "success",
            "model": MODEL, "prompt_version": "eval-prompt/2.0",
            "tokens": {"input": 5000, "output": 1800, "total": 6800},
            "api_calls": [
                {"attempt": 1, "status": "success", "model": MODEL,
                 "request_id": "e1", "duration_ms": 4000,
                 "tokens": {"input": 4000, "output": 100,
                            "total": 4100, "cached_input": 1000}},
                {"attempt": 2, "status": "success", "model": MODEL,
                 "request_id": "e2", "duration_ms": 2000,
                 "tokens": {"input": 1000, "output": 1700,
                            "total": 2700, "cached_input": None}},
            ],
            "evaluation": {},
        }],
    }


def test_rows_cover_retries_failed_with_usage_and_skip_no_usage():
    rows = derive_call_rows(_record(), rate=None)
    # 2 extraction (incl. the failed one WITH usage; the no-usage
    # failure is skipped) + 2 evaluation calls (initial + correction)
    assert len(rows) == 4
    refs = [r["call_ref"] for r in rows]
    assert len(set(refs)) == 4  # deterministic, unique -> dedupe key
    failed = [r for r in rows if r["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["total_cost_usd"] is not None


def test_resume_totals_combine_both_operations():
    rows = derive_call_rows(_record(), rate=None)
    summary = summarize_rows(rows)
    ext = compute_cost(1536, 0, 2535, MODEL)["total_cost_usd"] \
        + compute_cost(2000, 500, 10, MODEL)["total_cost_usd"]
    ev = compute_cost(4000, 1000, 100, MODEL)["total_cost_usd"] \
        + compute_cost(1000, 0, 1700, MODEL)["total_cost_usd"]
    assert summary["json_extraction"]["cost_usd"] == ext
    assert summary["ai_evaluation"]["cost_usd"] == ev
    assert summary["totals"]["cost_usd"] == ext + ev
    assert summary["totals"]["calls"] == 4


def test_fx_conversion_stamps_the_rate_used():
    rows = derive_call_rows(_record(), rate=Decimal("88.90"))
    assert all(r["usd_inr_rate"] == Decimal("88.90") for r in rows)
    for r in rows:
        assert r["total_cost_inr"] == r["total_cost_usd"] \
            * Decimal("88.90")
    no_fx = derive_call_rows(_record(), rate=None)
    import muse_ai.pricing as pricing
    if pricing.usd_inr_rate() is None:
        assert all(r["total_cost_inr"] is None for r in no_fx)


def test_historical_entry_without_api_calls_uses_aggregate_once():
    record = _record()
    del record["analyses"][0]["api_calls"]
    rows = derive_call_rows(record, rate=None)
    ev_rows = [r for r in rows if r["operation_type"] == "ai_evaluation"]
    assert len(ev_rows) == 1
    assert ev_rows[0]["input_tokens"] == 5000


# --- API surface ---------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    from api.main import app
    return TestClient(app)


def test_costs_endpoint_and_analytics_block(client):
    records = client.get("/api/resumes").json()["resumes"]
    with_usage = [r for r in records
                  if (r.get("token_usage") or {}).get("usage_available")]
    assert with_usage, "fixture records must include real usage"
    rid = with_usage[0]["resume_id"]
    body = client.get(f"/api/resumes/{rid}/costs").json()
    assert body["summary"]["json_extraction"]["calls"] >= 1
    assert Decimal(body["summary"]["totals"]["cost_usd"]) > 0
    # known fixture: 1536 in / 2535 out at the configured rates
    target = [r for r in with_usage
              if r["token_usage"]["input_tokens"] == 1536]
    if target:
        body = client.get(
            f"/api/resumes/{target[0]['resume_id']}/costs").json()
        assert Decimal(body["summary"]["totals"]["cost_usd"]) == Decimal("0.0006606")
    analytics = client.get("/api/analytics/summary").json()
    assert "ai_costs" in analytics
    assert Decimal(analytics["ai_costs"]["total_cost_usd"]) > 0
    assert client.get("/api/resumes/nope/costs").status_code == 404


def test_frontend_has_cost_views():
    app_js = open("frontend/js/app.js").read()
    assert "AI Cost Breakdown" in app_js
    assert "AI Expenditure" in app_js
