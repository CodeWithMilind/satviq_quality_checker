"""Persistent analytics: live-PostgreSQL tests (migration 004).

Runs only when SATVIQ_TEST_DATABASE_URL points at a scratch
database with the schema + migrations applied. Covers run
history, append-only attempts, dedupe, and SQL reconciliation.
"""

from __future__ import annotations

import copy
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("SATVIQ_TEST_DATABASE_URL"),
    reason="SATVIQ_TEST_DATABASE_URL not set")


@pytest.fixture()
def live():
    os.environ["SATVIQ_DATABASE_URL"] = \
        os.environ["SATVIQ_TEST_DATABASE_URL"]
    from api import db
    return db


def _record() -> dict:
    return {
        "resume_id": "analytics-live-0001",
        "filename": "live.pdf",
        "uploaded_at": "2026-11-12T00:00:00+00:00",
        "success": True, "duration_ms": 4200,
        "file_type": "pdf", "file_size_bytes": 12345, "page_count": 2,
        "final_extractor": "tika", "selected_quality_score": 91.0,
        "final_quality_status": "PASS",
        "quality": {"text_health": {"score": 100.0},
                    "completeness": {"score": 82.0},
                    "reading_order": {"score": 91.0}},
        "attempts": [{
            "attempt_id": "live-a1", "extractor_name": "tika",
            "success": True, "selected": True, "accepted": True,
            "duration_ms": 4000, "extracted_text": "hello world",
            "quality_score": 91.0, "quality_status": "PASS",
            "started_at": "2026-11-12T00:00:01+00:00",
            "completed_at": "2026-11-12T00:00:05+00:00"}],
        "resume_json": {"full_name": "Live Test"},
    }


def _counts(db, resume_id):
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n"
                    " FROM resume_processing_runs"
                    " WHERE resume_id = %s", (resume_id,))
        runs = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n"
                    " FROM candidate_extraction_attempts"
                    " a JOIN candidates c ON c.id = a.candidate_id"
                    " WHERE c.resume_id = %s", (resume_id,))
        attempts = cur.fetchone()["n"]
    return runs, attempts


def test_run_history_dedupe_and_reprocess(live):
    db = live
    rec = _record()
    try:
        db.sync_record(rec)
        assert _counts(db, rec["resume_id"]) == (1, 1)
        db.sync_record(rec)  # same state: no duplicate run/attempts
        assert _counts(db, rec["resume_id"]) == (1, 1)
        rec2 = copy.deepcopy(rec)
        rec2["attempts"][0]["attempt_id"] = "live-a2"
        db.sync_record(rec2)  # reprocess: history appends
        assert _counts(db, rec["resume_id"]) == (2, 2)
    finally:
        db.delete_candidate_by_resume(rec["resume_id"])


def test_sql_analytics_reconciles(live):
    from api import db_analytics
    summary = db_analytics.analytics_summary()
    assert summary is not None
    assert summary["meta"]["source"] == "postgresql"
    assert "llm" in summary and "ai_costs" in summary
    with live.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(total_cost_usd), 0) AS s"
                    " FROM ai_api_costs")
        assert float(summary["ai_costs"]["total_cost_usd"]) == \
            pytest.approx(float(cur.fetchone()["s"]), abs=1e-9)
