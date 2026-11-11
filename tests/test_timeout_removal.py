"""Timeout removal — spec §8 verification.

Muse AI has NO timeout anywhere (config, client, transport,
pipeline, frontend): a Muse response is waited out however long
it takes, the on-demand generation reports a RUNNING status while
it processes, duplicate generations are refused, and delayed
successes are persisted. Extraction keeps only configurable,
generous safety limits (spec §5), strictly separate from Claude.

All Muse responses are mocked — no real API tokens are consumed.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")
from fastapi.testclient import TestClient  # noqa: E402

from api import store  # noqa: E402
from api.main import app  # noqa: E402
from api.pipeline import (  # noqa: E402
    ai_generation_in_progress,
    generate_ai_output_for_record,
)
from muse_ai import load_muse_config  # noqa: E402
from muse_ai.client import MuseClient, http_post_json  # noqa: E402
from muse_ai.config import MuseConfig  # noqa: E402
from muse_ai.schema import validate_resume_json  # noqa: E402
from pipeline.config import PipelineConfig, load_pipeline_config  # noqa: E402
from pipeline.extractors import run_extractors  # noqa: E402

client = TestClient(app)

VALID_RESUME = {
    "full_name": "Jane Doe",
    "email": "jane.doe@example.com",
    "phone": "+91 98765 43210",
    "location": "Pune, India",
    "summary": "Python developer",
    "skills": ["Python", "FastAPI"],
    "experience": [],
    "education": [],
    "certifications": [],
    "languages": [],
    "links": [],
}
USAGE = {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}
TEXT = "Jane Doe\njane@example.com\n\nSkills\nPython, FastAPI\n"


def _valid_outcome(delay=0.0):
    parsed, _err = validate_resume_json(json.dumps(VALID_RESUME))
    assert parsed is not None
    return {"ok": True, "error": None, "error_type": None,
            "resume_json": parsed, "validation": "valid",
            "provider": "Muse AI", "model": "fake-model",
            "raw_text": json.dumps(VALID_RESUME),
            "usage": dict(USAGE), "duration_ms": 12.0,
            "request_id": "req-test-1"}


class _Service:
    configured = True
    model = "fake-model"
    provider_name = "Muse AI"
    configuration_error = None

    def __init__(self, outcome, delay=0.0, gate=None):
        self._outcome = outcome
        self._delay = delay
        self._gate = gate
        self.calls: list[str] = []

    def generate_resume_json(self, text, hyperlinks=None):
        self.calls.append(text)
        if self._gate is not None:
            self._gate.wait(timeout=15)
        if self._delay:
            time.sleep(self._delay)
        return dict(self._outcome)


def _record(resume_id):
    return {
        "resume_id": resume_id, "filename": "x.pdf", "success": True,
        "extractor_used": "docling", "extracted_text": TEXT,
        "final_extracted_text": TEXT,
        "attempts": [{"extractor_name": "docling", "success": True,
                      "accepted": True, "is_final_selected": True,
                      "extracted_text": TEXT, "text": TEXT}],
        "text_normalization": {
            "status": "applied", "source": "docling",
            "original_extracted_text": TEXT, "normalized_text": TEXT,
            "original_chars": len(TEXT), "normalized_chars": len(TEXT),
            "estimated_reduction_percent": 0.0,
            "normalization_steps": [], "normalization_applied": True},
        "hyperlinks": [], "muse_requests": [], "resume_json": None,
        "pipeline_trace": [{"stage": "Final Result", "type": "final",
                            "status": "FAIL", "message": ""}],
    }


# §8.1 + §8.8 — Muse requests carry NO timeout; no hardcoded
# 20-second (or any) Muse timeout remains in configuration.
def test_1_muse_client_sends_no_timeout(monkeypatch):
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured["timeout"] = timeout
        return {"choices": [{"message": {"content": "hello"}}],
                "model": "muse-spark-1.3", "usage": {}, "id": "r1"}

    monkeypatch.setattr("muse_ai.client.http_post_json", fake_post)
    muse_client = MuseClient(MuseConfig(api_key="test-key"))
    result = muse_client.chat_text("instructions", "user text")
    assert result["text"] == "hello"
    assert captured["timeout"] is None

    # Even a MUSE_TIMEOUT_SECONDS value in the environment is
    # ignored — the setting no longer exists.
    config = load_muse_config({"MUSE_API_KEY": "k",
                               "MUSE_TIMEOUT_SECONDS": "1"})
    assert config.configured is True
    assert config.timeout_seconds is None
    assert config.public_status()["timeout_seconds"] is None


# §8.1 — A slow Muse transport response is NOT cut off: the real
# HTTP seam, called the way the client calls it (timeout=None),
# waits for a delayed server and returns its body.
def test_2_slow_response_is_not_cut_off():
    import http.server

    class SlowHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib handler API
            time.sleep(1.5)
            body = json.dumps({"choices": [
                {"message": {"content": "ok"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), SlowHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/chat/completions"
        started = time.perf_counter()
        out = http_post_json(url, {"model": "m"}, {}, timeout=None)
        elapsed = time.perf_counter() - started
        assert out["choices"][0]["message"]["content"] == "ok"
        assert elapsed >= 1.5
    finally:
        server.shutdown()


# §8.4 — A delayed successful generation is persisted, with token
# usage, model/provider and duration intact.
def test_3_delayed_success_is_persisted():
    service = _Service(_valid_outcome(), delay=0.8)
    record = _record("timeout-test-0003")
    record, attempt = generate_ai_output_for_record(record, service=service)
    assert attempt["status"] == "success"
    assert service.calls == [TEXT]
    store.save_record(record)
    try:
        raw = store.load_raw_record("timeout-test-0003")
        assert raw["resume_json"]["full_name"] == "Jane Doe"
        assert raw["resume_json_meta"]["model"] == "fake-model"
        assert raw["resume_json_meta"]["validation"] == "valid"
        assert raw["token_usage"]["total_tokens"] == 140
        assert raw["ai_last_attempt"]["status"] == "success"
    finally:
        store.delete_record("timeout-test-0003")


# §8.5 — A failed API request is recorded correctly (and is not
# marked successful).
def test_4_failed_request_recorded():
    outcome = {"ok": False, "error": "Muse API returned HTTP 500",
               "error_type": "MuseError", "resume_json": None,
               "validation": None, "provider": "Muse AI",
               "model": "fake-model", "raw_text": None,
               "usage": {}, "duration_ms": 5.0}
    record = _record("timeout-test-0004")
    record, attempt = generate_ai_output_for_record(
        record, service=_Service(outcome))
    assert attempt["status"] == "failed"
    assert record["resume_json"] is None
    assert record["llm_status"] == "failed"
    assert record["ai_last_attempt"]["error"]


# §8.6 (backend) — Only one active generation per resume: a second
# call while one is running is refused without a duplicate Muse
# call, and the guard is released afterwards. (Client-side
# cancellation is "stop waiting" only — see test_6 — so an
# in-flight generation can never corrupt the record.)
def test_5_duplicate_generation_refused():
    gate = threading.Event()
    service = _Service(_valid_outcome(), gate=gate)
    record = _record("timeout-test-0005")
    results = {}

    def run_first():
        results["first"] = generate_ai_output_for_record(
            record, service=service)

    thread = threading.Thread(target=run_first)
    thread.start()
    try:
        deadline = time.time() + 10
        while not service.calls and time.time() < deadline:
            time.sleep(0.02)
        assert service.calls, "first generation never started"
        assert ai_generation_in_progress("timeout-test-0005") is True
        _rec2, attempt2 = generate_ai_output_for_record(
            _record("timeout-test-0005"), service=service)
        assert attempt2["status"] == "already_running"
        assert len(service.calls) == 1  # no duplicate Muse call
    finally:
        gate.set()
        thread.join(timeout=15)
    assert results["first"][1]["status"] == "success"
    assert ai_generation_in_progress("timeout-test-0005") is False
    # The guard is released: a later generation runs normally.
    _rec3, attempt3 = generate_ai_output_for_record(
        _record("timeout-test-0005"), service=service)
    assert attempt3["status"] == "success"
    assert len(service.calls) == 2


# §7 + §4 — The endpoint persists a RUNNING status while the
# generation is in flight (a slow response is RUNNING, not FAILED),
# then replaces it with the real outcome in a single final save.
def test_6_endpoint_marks_running_then_persists(monkeypatch):
    import api.main as main_module

    gate = threading.Event()

    def gated_generate(record):
        gate.wait(timeout=15)
        record["ai_last_attempt"] = {
            "at": "2026-11-11T00:00:00+00:00", "status": "success",
            "ok": True, "provider": "Muse AI", "model": "fake-model",
            "validation": "valid", "error": None, "error_type": None,
            "duration_ms": 12.0}
        return record, {"status": "success", "ok": True}

    monkeypatch.setattr(main_module, "generate_ai_output_for_record",
                        gated_generate)

    document = pymupdf.open()
    page = document.new_page()
    y = 60
    for line in TEXT.splitlines():
        page.insert_text((50, y), line, fontsize=10)
        y += 18
    payload = document.tobytes()
    document.close()
    created = client.post(
        "/api/resumes/process",
        files=[("files", ("running.pdf", payload, "application/pdf"))],
    ).json()["results"][0]
    resume_id = created["resume_id"]
    post_client = TestClient(app)
    outcome = {}

    def do_post():
        outcome["response"] = post_client.post(
            f"/api/resumes/{resume_id}/ai/generate")

    thread = threading.Thread(target=do_post)
    thread.start()
    try:
        deadline = time.time() + 10
        seen = None
        while time.time() < deadline:
            body = client.get(f"/api/resumes/{resume_id}").json()
            seen = (body.get("ai_last_attempt") or {}).get("status")
            if seen == "running":
                break
            time.sleep(0.05)
        assert seen == "running", f"RUNNING status never visible: {seen}"
    finally:
        gate.set()
        thread.join(timeout=15)
    try:
        assert outcome["response"].status_code == 200
        body = client.get(f"/api/resumes/{resume_id}").json()
        assert body["ai_last_attempt"]["status"] == "success"
    finally:
        client.delete(f"/api/resumes/{resume_id}")


# §8.2 + §8.6 (frontend) — The frontend has no timer-based abort;
# its only AbortController is the Generate button's user-triggered
# "stop waiting", which never cancels the server-side generation.
def test_7_frontend_never_aborts_on_a_timer():
    root = Path(__file__).resolve().parent.parent
    api_js = (root / "frontend" / "js" / "api.js").read_text(encoding="utf-8")
    app_js = (root / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    assert "AbortController" not in api_js
    assert "DEFAULT_TIMEOUT" not in api_js
    assert "xhr.timeout = 0" in api_js
    assert "Stop waiting" in app_js
    assert "still running on the server" in app_js
    # RUNNING is a displayed status, distinct from FAILED.
    assert "generation is RUNNING" in app_js


# §8.7 — Extractor safety limits still function independently of
# Muse, and their configured defaults are generous guards, not
# processing limits.
def test_8_extractor_timeouts_independent_and_generous():
    class Slow:
        def __init__(self, name):
            self.name = name

        def extract(self, path):
            time.sleep(1.5)
            from extractor.models import ExtractionResult
            return ExtractionResult(
                extractor_name=self.name, success=True, text=TEXT,
                error=None, duration_ms=1500.0, page_count=1,
                page_texts=[TEXT], metadata={})

    class Fast:
        def __init__(self, name):
            self.name = name

        def extract(self, path):
            from extractor.models import ExtractionResult
            return ExtractionResult(
                extractor_name=self.name, success=True, text=TEXT,
                error=None, duration_ms=5.0, page_count=1,
                page_texts=[TEXT], metadata={})

    results = run_extractors(
        "ignored.pdf",
        extractors={"tika": Slow("tika"), "pymupdf": Fast("pymupdf"),
                    "docling": Fast("docling")},
        timeout_seconds=0.2)
    by = {r["extractor"]: r for r in results}
    assert by["tika"]["status"] == "timeout"
    assert by["pymupdf"]["status"] == "success"

    # The pipeline's configured guard is generous (10 minutes) and
    # the LLM stage has no timeout setting at all.
    assert PipelineConfig().per_extractor_timeout_seconds == 600.0
    assert load_pipeline_config().per_extractor_timeout_seconds == 600.0
    assert not hasattr(PipelineConfig(), "llm_timeout_seconds")
