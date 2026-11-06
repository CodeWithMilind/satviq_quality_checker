"""Per-resume Muse token usage monitoring.

Covers: usage normalization from API responses, aggregation across
multiple requests for one resume, no double counting (the vision
request that also produced the Resume JSON is counted once),
persistence through the record store, context-window status, and
the actual-vs-unavailable distinction. All values come from fake
API outcomes — nothing is estimated anywhere in the system.
"""

from pathlib import Path

from api import store
from api.pipeline import (_aggregate_token_usage, _apply_muse_stages,
                          ensure_pipeline_trace, process_resume_file)

VALID_JSON = {"full_name": "Priya Nair", "skills": ["Python"]}


def _outcome(ok=True, usage=None, **extra):
    base = {"ok": ok, "error": None if ok else "boom",
            "error_type": None if ok else "MuseError",
            "resume_json": VALID_JSON if ok else None,
            "validation": "valid" if ok else None,
            "provider": "Muse AI", "model": "muse-spark-1.3",
            "raw_text": "{}", "usage": usage or {},
            "usage_available": bool(usage),
            "request_id": "req-1", "duration_ms": 10.0,
            "timeout_seconds": 20.0, "input_type": "image",
            "visual_provided": True, "tika_text_provided": False,
            "tika_text_chars": 0, "hyperlinks_provided": False,
            "hyperlink_count": 0, "page_count": 1,
            "pages_processed": 1, "pages_skipped": 0}
    base.update(extra)
    return base


class FakeMuse:
    provider_name = "Muse AI"
    model = "muse-spark-1.3"
    configured = True
    vision_enabled = True
    configuration_error = None

    def __init__(self, vision_outcome=None, json_outcome=None):
        self.config = type("Cfg", (), {
            "timeout_seconds": 20.0,
            "context_window_tokens": 1_048_576})()
        self._vision = vision_outcome
        self._json = json_outcome

    def vision_extract(self, path, **kwargs):
        return self._vision

    def generate_resume_json(self, text, **kwargs):
        return self._json


def test_vision_request_usage_becomes_resume_totals(tmp_path):
    png = tmp_path / "resume.png"
    png.write_bytes(b"\x89PNG-fake")
    service = FakeMuse(vision_outcome=_outcome(usage={
        "prompt_tokens": 100, "completion_tokens": 40,
        "total_tokens": 140,
        "prompt_tokens_details": {"cached_tokens": 25}}))
    record = process_resume_file(
        png, resume_id="token-test-1", original_filename="resume.png",
        muse_service=service)
    usage = record["token_usage"]
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 40
    assert usage["total_tokens"] == 140
    assert usage["cached_input_tokens"] == 25
    assert usage["api_calls"] == 1
    assert usage["successful_calls"] == 1
    assert usage["models"] == ["muse-spark-1.3"]
    assert usage["usage_available"] is True
    assert usage["context"]["window_tokens"] == 1_048_576
    assert usage["context"]["status"] == "Normal usage"
    attempt_meta = record["attempts"][-1]["metadata"]
    assert attempt_meta["usage_available"] is True
    assert attempt_meta["request_id"] == "req-1"


def test_aggregation_across_two_requests_no_double_count():
    record = {"resume_id": "token-test-2", "attempts": [
                  {"extractor_name": "tika",
                   "extracted_text": "candidate text",
                   "text": "candidate text"}],
              "attempt_count": 1, "pipeline_trace": [],
              "final_quality_status": "WARNING",
              "extracted_text": "candidate text",
              "resume_json": None, "resume_json_source": None,
              "resume_json_meta": None, "extractor_used": None,
              "selection_reason": "", "hyperlinks": [],
              "hyperlink_error": None, "muse_requests": []}
    service = FakeMuse(
        vision_outcome=_outcome(ok=False, usage={
            "prompt_tokens": 10, "completion_tokens": 5,
            "total_tokens": 15}),
        json_outcome=_outcome(usage={
            "prompt_tokens": 50, "completion_tokens": 20,
            "total_tokens": 70}))
    _apply_muse_stages(record, Path("x.pdf"), service, None)
    usage = record["token_usage"]
    assert usage["api_calls"] == 2
    assert usage["input_tokens"] == 60
    assert usage["output_tokens"] == 25
    assert usage["total_tokens"] == 85
    assert usage["successful_calls"] == 1
    assert usage["failed_calls"] == 1
    assert len(record["muse_requests"]) == 2


def test_vision_success_is_not_double_counted_on_read_backfill():
    record = {"resume_id": "token-test-3",
              "extractor_used": "muse_ai",
              "resume_json_source": "muse_ai",
              "resume_json_meta": {"model": "muse-spark-1.3",
                                   "usage": {"prompt_tokens": 100,
                                             "completion_tokens": 40,
                                             "total_tokens": 140},
                                   "error": None},
              "attempts": [{"extractor_name": "muse_ai",
                            "success": True, "error_type": None,
                            "model": "muse-spark-1.3",
                            "duration_ms": 5.0,
                            "metadata": {"usage": {
                                "prompt_tokens": 100,
                                "completion_tokens": 40,
                                "total_tokens": 140},
                                "request_id": "req-9"}}],
              "pipeline_trace": [{"stage": "x", "type": "final"}],
              "token_usage": None}
    ensure_pipeline_trace(record)
    assert record["token_usage"]["api_calls"] == 1
    assert record["token_usage"]["total_tokens"] == 140


def test_usage_persists_through_the_record_store(tmp_path):
    record = {"resume_id": "token-test-4", "attempts": [],
              "pipeline_trace": [{"stage": "x", "type": "final"}],
              "token_usage": None, "extractor_used": "muse_ai",
              "resume_json_source": "muse_ai",
              "resume_json_meta": {"model": "muse-spark-1.3",
                                   "usage": {"prompt_tokens": 7,
                                             "completion_tokens": 3,
                                             "total_tokens": 10},
                                   "error": None},
              "attempts_detail": None}
    record["attempts"] = [{"extractor_name": "muse_ai", "success": True,
                           "error_type": None, "model": "muse-spark-1.3",
                           "duration_ms": 1.0,
                           "metadata": {"usage": {"prompt_tokens": 7,
                                                  "completion_tokens": 3,
                                                  "total_tokens": 10}}}]
    try:
        store.save_record(record)
        loaded = store.get_record("token-test-4")
        assert loaded["token_usage"]["total_tokens"] == 10
        assert loaded["token_usage"]["api_calls"] == 1
    finally:
        store.delete_record("token-test-4")


def test_context_limit_statuses():
    def agg(input_tokens, window=1000):
        return _aggregate_token_usage(
            [{"stage": "vision", "model": "m", "request_id": None,
              "status": "success", "duration_ms": 1.0,
              "input_tokens": input_tokens, "output_tokens": 1,
              "total_tokens": input_tokens + 1,
              "cached_input_tokens": None, "usage_available": True}],
            window)
    assert agg(100)["context"]["status"] == "Normal usage"
    assert agg(100)["context"]["remaining_tokens"] == 900
    assert agg(850)["context"]["status"] == "Near context limit"
    assert agg(1200)["context"]["status"] == "Context limit exceeded"


def test_missing_usage_stays_unavailable_not_zero():
    aggregate = _aggregate_token_usage(
        [{"stage": "vision", "model": "m", "request_id": None,
          "status": "success", "duration_ms": 1.0,
          "input_tokens": None, "output_tokens": None,
          "total_tokens": None, "cached_input_tokens": None,
          "usage_available": False}], 1_048_576)
    assert aggregate["api_calls"] == 1
    assert aggregate["input_tokens"] is None
    assert aggregate["total_tokens"] is None
    assert aggregate["usage_available"] is False
    assert aggregate["context"] is None


def test_frontend_displays_token_usage_and_hyperlinks():
    """Production UI (v14): the inspector renders token usage from
    token_usage / muse_requests and the PDF hyperlink list from
    record.hyperlinks — all from real backend data."""
    root = Path(__file__).resolve().parent.parent
    js = (root / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    assert "token_usage" in js
    assert "muse_requests" in js
    assert "LLM Token Usage" in js
    assert "hyperlinks" in js
    assert "PDF Hyperlinks" in js

