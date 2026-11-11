"""Default-pipeline integration: Selected Normalized Text → Muse AI
→ Resume JSON → persistence → frontend display.

Regression coverage for the reported bug: a resume could be
extracted (Docling), quality-checked, selected and show Selected
Normalized Text, yet the AI Output tab said "No AI output is stored"
because (1) /api/resumes/process defaulted to the LEGACY pipeline —
the simple pipeline was opt-in only — and (2) the simple pipeline's
record builder dropped the Muse outcome on the floor
(text_normalization / resume_json_meta were stored as None,
muse_requests as [], token_usage as None), while the frontend read
only record.resume_json and showed a bare "no output" note.

All 15 required cases are covered with fake extractors and fake
Muse services — no network, no Java/Tika server, no Docling
install. The API-level cases use the real PyMuPDF extractor on a
generated PDF (PyMuPDF is a hard dependency of the project).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")
from fastapi.testclient import TestClient  # noqa: E402

from api import store  # noqa: E402
from api.main import app  # noqa: E402
from api.pipeline import (  # noqa: E402
    generate_ai_output_for_record,
    process_resume_file,
    process_resume_file_simple,
    run_manual_extractors,
)
from api.text_normalization import normalize_text  # noqa: E402
from extractor.models import ExtractionResult  # noqa: E402
from muse_ai.schema import validate_resume_json  # noqa: E402
from pipeline import ExtractionPipeline, PipelineConfig  # noqa: E402

client = TestClient(app)

GOOD_TEXT = """Jane Doe
jane.doe@example.com | +91 98765 43210 | Pune, India

Professional Summary
Python developer building practical AI and backend applications for real users.
Focused on FastAPI services, data pipelines, and LLM powered tools with tested code.

Skills
Python, FastAPI, REST APIs, SQL, MySQL, Pandas, NumPy, LangChain, and Git are used daily.
Built backend services with authentication, validation, and structured logging.

Work Experience
Data Scientist Intern at Example AI Solutions from May 2025 to November 2025.
Cleaned datasets, trained classification models, and documented experiments.
Developed APIs and wrote tests for data processing pipelines used in production.

Education
Master of Computer Applications at Example University, Pune, 2025 to 2027.
Bachelor of Computer Applications completed in 2025 with distinction.

Projects
Data workbench for uploading, analysing, and visualising datasets with FastAPI.
PDF Chat Assistant using RAG, LangChain, FAISS, and FastAPI for document questions.

Certifications
Certified Data Scientist, completed in 2025 with additional ML coursework.
- Python programming
- FastAPI development
- Machine learning
"""

GOOD_TIKA = GOOD_TEXT.replace("Jane Doe", "Jane Doe TIKAMARKER")
GOOD_PYMUPDF = GOOD_TEXT.replace("Jane Doe", "Jane Doe PYMUPDFMARKER")
GOOD_DOCLING = GOOD_TEXT.replace("Jane Doe", "Jane Doe DOCLINGMARKER")

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


class FakeExtractor:
    def __init__(self, name, text="", success=True, error=None):
        self.name = name
        self._text = text
        self._success = success
        self._error = error

    def extract(self, path):
        if not self._success:
            return ExtractionResult(
                extractor_name=self.name, success=False, text="",
                error=self._error or "fake extractor failure",
                error_type="ExtractionFailedError", duration_ms=5.0,
                page_count=None, page_texts=[], metadata={})
        return ExtractionResult(
            extractor_name=self.name, success=True, text=self._text,
            error=None, duration_ms=5.0, page_count=1,
            page_texts=[self._text] if self._text else [], metadata={})


def _extractors(tika, pymupdf_e, docling):
    return {"tika": tika, "pymupdf": pymupdf_e, "docling": docling}


class FakeMuseService:
    """MuseService-shaped fake returning a schema-valid outcome."""

    configured = True
    model = "fake-model"
    provider_name = "Muse AI"
    configuration_error = None

    def __init__(self):
        self.calls: list[str] = []

    def generate_resume_json(self, text, hyperlinks=None):
        self.calls.append(text)
        parsed, _err = validate_resume_json(json.dumps(VALID_RESUME))
        assert parsed is not None
        return {"ok": True, "error": None, "error_type": None,
                "resume_json": parsed, "validation": "valid",
                "provider": "Muse AI", "model": "fake-model",
                "raw_text": json.dumps(VALID_RESUME),
                "usage": dict(USAGE), "duration_ms": 12.0,
                "request_id": "req-test-1"}


def _record(tmp_path, extractors, service, resume_id="flow-test-0001"):
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-fake-content")
    pipe = ExtractionPipeline(
        config=PipelineConfig(), extractors=extractors, llm_service=service)
    return process_resume_file_simple(
        pdf, resume_id=resume_id, original_filename="resume.pdf",
        pipeline=pipe)


def _winner_record(tmp_path, winner, service):
    texts = {"tika": GOOD_TIKA, "pymupdf": GOOD_PYMUPDF,
             "docling": GOOD_DOCLING}
    exts = _extractors(*[
        FakeExtractor(name, texts[name]) if name == winner
        else FakeExtractor(name, success=False, error=f"{name} unavailable")
        for name in ("tika", "pymupdf", "docling")])
    return _record(tmp_path, exts, service,
                   resume_id=f"flow-{winner}-0001")


def _assert_full_flow(record, winner, service):
    assert record["pipeline_mode"] == "simple"
    assert record["selected_extractor"] == winner
    assert record["extractor_used"] == winner
    assert record["success"] is True
    # Selected normalized text is stored, sourced from the winner.
    norm = record["text_normalization"]
    assert norm is not None
    assert norm["source"] == winner
    assert norm["original_extracted_text"]
    assert winner.upper() + "MARKER" in norm["original_extracted_text"]
    assert norm["normalized_text"]
    # Muse received exactly the stored selected normalized text, once.
    assert service.calls == [norm["normalized_text"]]
    # Validated JSON persisted under every field the frontend reads.
    assert record["resume_json"] is not None
    assert record["resume_json"]["full_name"] == "Jane Doe"
    assert record["resume_data"] == record["resume_json"]
    assert record["resume_json_source"] == "muse_ai"
    meta = record["resume_json_meta"]
    assert meta["provider"] == "Muse AI"
    assert meta["model"] == "fake-model"
    assert meta["validation"] == "valid"
    assert meta["error"] is None
    assert meta["duration_ms"] == 12.0
    assert record["llm_status"] == "success"
    # Trace tells the whole story honestly.
    stages = {s["stage"]: s for s in record["pipeline_trace"]}
    assert stages["Best Extractor Selection"]["status"] == "PASS"
    assert stages["Text Normalization"]["status"] == "PASS"
    muse_stage = next(s for s in record["pipeline_trace"]
                      if s["stage"].startswith("Muse AI — Resume JSON"))
    assert muse_stage["status"] == "PASS"
    assert stages["JSON Validation"]["status"] == "PASS"
    assert record["pipeline_trace"][-1]["status"] == "PASS"


# 1 — Tika selected → normalized → Muse → valid JSON -------------------
def test_01_tika_selected_flows_to_muse_json(tmp_path):
    service = FakeMuseService()
    record = _winner_record(tmp_path, "tika", service)
    _assert_full_flow(record, "tika", service)


# 2 — PyMuPDF selected → normalized → Muse → valid JSON ----------------
def test_02_pymupdf_selected_flows_to_muse_json(tmp_path):
    service = FakeMuseService()
    record = _winner_record(tmp_path, "pymupdf", service)
    _assert_full_flow(record, "pymupdf", service)


# 3 — Docling selected → normalized → Muse → valid JSON (acceptance) ---
def test_03_docling_selected_flows_to_muse_json(tmp_path):
    service = FakeMuseService()
    record = _winner_record(tmp_path, "docling", service)
    _assert_full_flow(record, "docling", service)
    # The exact reported repro: Docling text is the record's text.
    assert record["extracted_text"] == GOOD_DOCLING
    assert record["final_extracted_text"] == GOOD_DOCLING


# 4 — A failed extractor is never selected ------------------------------
def test_04_failed_extractor_is_not_selected(tmp_path):
    service = FakeMuseService()
    exts = _extractors(
        FakeExtractor("tika", success=False, error="tika boom"),
        FakeExtractor("pymupdf", GOOD_PYMUPDF),
        FakeExtractor("docling", success=False, error="docling boom"))
    record = _record(tmp_path, exts, service)
    assert record["selected_extractor"] == "pymupdf"
    selected = [a for a in record["attempts"] if a["is_final_selected"]]
    assert [a["extractor_name"] for a in selected] == ["pymupdf"]

    # All three fail → nothing selected, no Muse call, honest record.
    service2 = FakeMuseService()
    exts2 = _extractors(
        FakeExtractor("tika", success=False, error="a"),
        FakeExtractor("pymupdf", success=False, error="b"),
        FakeExtractor("docling", success=False, error="c"))
    record2 = _record(tmp_path, exts2, service2,
                      resume_id="flow-test-0004")
    assert record2["selected_extractor"] is None
    assert record2["success"] is False
    assert record2["resume_json"] is None
    assert record2["llm_status"] == "skipped"
    assert service2.calls == []


# 5 — Best-extractor selection is respected ------------------------------
def test_05_best_extractor_selection_is_respected(tmp_path):
    service = FakeMuseService()
    long_bad = "\ufffd garbled corrupted text \ufffd\n" * 80
    exts = _extractors(
        FakeExtractor("tika", long_bad),
        FakeExtractor("pymupdf", GOOD_PYMUPDF),
        FakeExtractor("docling", success=False, error="docling boom"))
    record = _record(tmp_path, exts, service, resume_id="flow-test-0005")
    assert record["selected_extractor"] == "pymupdf"
    assert record["extractor_reports"]["pymupdf"]["quality_score"] > \
        record["extractor_reports"]["tika"]["quality_score"]
    assert "PYMUPDFMARKER" in record["text_normalization"]["original_extracted_text"]


# 6 — The EXACT selected normalized text is sent to Muse ----------------
def test_06_exact_selected_normalized_text_is_sent(tmp_path):
    service = FakeMuseService()
    messy = GOOD_DOCLING.replace("\n", "\r\n") + "\r\n\r\n\r\n"
    assert normalize_text(messy) != messy  # normalization really changes it
    exts = _extractors(
        FakeExtractor("tika", success=False, error="tika boom"),
        FakeExtractor("pymupdf", success=False, error="pymupdf boom"),
        FakeExtractor("docling", messy))
    record = _record(tmp_path, exts, service, resume_id="flow-test-0006")
    assert record["selected_extractor"] == "docling"
    sent = service.calls[0]
    assert sent == normalize_text(messy)
    assert sent == record["text_normalization"]["normalized_text"]
    assert sent != record["text_normalization"]["original_extracted_text"]
    # Only the selected extractor's text was sent — no other output.
    assert "TIKAMARKER" not in sent and "PYMUPDFMARKER" not in sent


# 7 — Exactly one Muse request per successful processing ---------------
def test_07_exactly_one_muse_request(tmp_path):
    service = FakeMuseService()
    record = _winner_record(tmp_path, "docling", service)
    assert len(service.calls) == 1
    assert len(record["muse_requests"]) == 1
    assert record["muse_requests"][0]["status"] == "success"
    assert record["resume_json_meta"]["attempts"] == 1


# 8 — Valid JSON is persisted and returned through the API ---------------
def test_08_json_persisted_and_returned_via_api(tmp_path):
    service = FakeMuseService()
    record = _winner_record(tmp_path, "pymupdf", service)
    record["_stored_name"] = None
    store.save_record(record)
    try:
        loaded = store.get_record(record["resume_id"])
        assert loaded["resume_json"]["full_name"] == "Jane Doe"
        assert loaded["text_normalization"]["normalized_text"] == \
            record["text_normalization"]["normalized_text"]
        assert loaded["token_usage"]["total_tokens"] == 140
    finally:
        store.delete_record(record["resume_id"])

    # The default upload endpoint runs the simple pipeline (no
    # /process-simple, no environment variable needed).
    document = pymupdf.open()
    page = document.new_page()
    y = 60
    for line in GOOD_TEXT.splitlines():
        page.insert_text((50, y), line, fontsize=10)
        y += 18
    payload = document.tobytes()
    document.close()
    response = client.post(
        "/api/resumes/process",
        files=[("files", ("api_flow.pdf", payload, "application/pdf"))])
    assert response.status_code == 200
    (api_record,) = response.json()["results"]
    try:
        assert api_record["pipeline_mode"] == "simple"
        assert api_record["selected_extractor"] is not None
        assert api_record["text_normalization"] is not None
        assert api_record["text_normalization"]["normalized_text"]
        # Muse is not configured in the test environment: the AI
        # stage outcome is persisted and visible, not silently empty.
        assert api_record["llm_status"] == "not_configured"
        assert api_record["resume_json_meta"]["status"] == "not_configured"
        assert api_record["resume_json"] is None
        detail = client.get(f"/api/resumes/{api_record['resume_id']}").json()
        assert detail["llm_status"] == "not_configured"
        assert detail["text_normalization"]["normalized_text"]
    finally:
        client.delete(f"/api/resumes/{api_record['resume_id']}")


# 9 — Token usage is persisted correctly ---------------------------------
def test_09_token_usage_persisted(tmp_path):
    service = FakeMuseService()
    record = _winner_record(tmp_path, "tika", service)
    usage = record["token_usage"]
    assert usage is not None
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 40
    assert usage["total_tokens"] == 140
    assert usage["api_calls"] == 1
    assert usage["successful_calls"] == 1
    assert usage["usage_available"] is True
    assert record["muse_requests"][0]["total_tokens"] == 140
    assert record["resume_json_meta"]["usage"] == USAGE


# 10 — Invalid JSON is recorded as a validation failure -------------------
def test_10_invalid_json_recorded_as_validation_failure(tmp_path):
    class InvalidService(FakeMuseService):
        def generate_resume_json(self, text, hyperlinks=None):
            self.calls.append(text)
            return {"ok": False,
                    "error": "JSON validation failed: not valid JSON.",
                    "error_type": "ValidationError",
                    "resume_json": None, "validation": "invalid",
                    "provider": "Muse AI", "model": "fake-model",
                    "raw_text": "this is not json",
                    "usage": dict(USAGE), "duration_ms": 5.0}

    service = InvalidService()
    record = _winner_record(tmp_path, "docling", service)
    assert record["resume_json"] is None
    assert record["resume_json_source"] is None
    assert record["llm_status"] == "failed"
    assert record["resume_json_meta"]["validation"] == "invalid"
    assert record["resume_json_meta"]["error"]
    # Extraction success stays separate from the LLM failure.
    assert record["success"] is True
    assert record["selected_extractor"] == "docling"
    # Invalid JSON is never retried.
    assert len(service.calls) == 1
    stages = {s["stage"]: s for s in record["pipeline_trace"]}
    assert stages["JSON Validation"]["status"] == "FAIL"
    assert record["pipeline_trace"][-1]["status"] == "FAIL"


# 11 — Muse API timeout / connection errors are recorded -----------------
def test_11_timeout_and_connection_errors_recorded(tmp_path):
    class TimeoutService(FakeMuseService):
        def generate_resume_json(self, text, hyperlinks=None):
            self.calls.append(text)
            return {"ok": False, "error": "Request timed out",
                    "error_type": "TimeoutError",
                    "resume_json": None, "validation": None,
                    "provider": "Muse AI", "model": "fake-model",
                    "raw_text": None, "usage": {}, "duration_ms": 10.0}

    service = TimeoutService()
    record = _winner_record(tmp_path, "pymupdf", service)
    assert record["resume_json"] is None
    assert record["llm_status"] == "failed"
    assert record["resume_json_meta"]["error_type"] == "TimeoutError"
    assert record["resume_json_meta"]["error"]
    # Transient failures retry per configuration (1 + 2 retries) and
    # the request log shows the timed-out call honestly.
    assert len(service.calls) == 3
    assert record["resume_json_meta"]["attempts"] == 3
    assert record["muse_requests"][0]["status"] == "timeout"
    assert record["token_usage"]["timed_out_calls"] == 1
    assert record["success"] is True  # extraction still succeeded


# 12 — Muse not configured is reported correctly --------------------------
def test_12_muse_not_configured_reported(tmp_path):
    class UnconfiguredService:
        configured = False
        model = "muse-spark-1.3"
        provider_name = "Muse AI"
        configuration_error = "MUSE_API_KEY is not set."

        def generate_resume_json(self, text, hyperlinks=None):
            raise AssertionError("must not be called when unconfigured")

    exts = _extractors(
        FakeExtractor("tika", success=False, error="tika boom"),
        FakeExtractor("pymupdf", success=False, error="pymupdf boom"),
        FakeExtractor("docling", GOOD_DOCLING))
    record = _record(tmp_path, exts, UnconfiguredService(),
                     resume_id="flow-test-0012")
    assert record["selected_extractor"] == "docling"
    assert record["success"] is True
    assert record["resume_json"] is None
    assert record["llm_status"] == "not_configured"
    assert record["resume_json_meta"]["status"] == "not_configured"
    assert "MUSE_API_KEY" in (record["resume_json_meta"]["error"] or "")
    assert record["muse_requests"] == []
    assert record["token_usage"] is None
    assert record["text_normalization"]["normalized_text"]
    muse_stage = next(s for s in record["pipeline_trace"]
                      if s["stage"].startswith("Muse AI — Resume JSON"))
    assert muse_stage["status"] == "FAIL"
    assert "NOT CONFIGURED" in muse_stage["message"]


# 13 — The frontend can display the saved JSON ------------------------------
def test_13_frontend_displays_saved_json():
    root = Path(__file__).resolve().parent.parent
    app_js = (root / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    api_js = (root / "frontend" / "js" / "api.js").read_text(encoding="utf-8")
    # The AI Output tab resolves the JSON from every field a stored
    # record may carry it under — not resume_json alone.
    assert "record.resume_data" in app_js
    assert "llm.resume_json" in app_js
    # …and surfaces provider / model / validation / duration / tokens.
    assert "aiValidation" in app_js and "aiDuration" in app_js
    # A not-configured or failed Muse stage shows its real error;
    # the bare fallback message is only the last resort (it appears
    # AFTER the specific branches in the source).
    assert "not_configured" in app_js
    assert "Muse AI is not configured" in app_js
    assert app_js.index("not_configured") < \
        app_js.index("No AI output is stored")
    # Reprocess is wired end to end.
    assert "reprocessResume" in api_js and "/reprocess" in api_js
    assert 'data-menu-action="reprocess"' in app_js


# 14 — Existing legacy records remain readable ------------------------------
def test_14_legacy_records_remain_readable(tmp_path):
    from api.pipeline import ensure_pipeline_trace, failed_record

    legacy = failed_record(
        resume_id="flow-legacy-0001", original_filename="old.pdf",
        file_type="pdf", mime_type="application/pdf", file_size=10,
        error="Unsupported file type '.exe'.")
    enriched = ensure_pipeline_trace(dict(legacy))
    assert enriched["pipeline_trace"]
    assert enriched["resume_id"] == "flow-legacy-0001"

    # The legacy pipeline itself still runs when explicitly requested.
    pdf = tmp_path / "legacy.pdf"
    pdf.write_bytes(b"%PDF-fake-content")
    record = process_resume_file(
        pdf, resume_id="flow-legacy-0002", original_filename="legacy.pdf",
        pipeline_mode="legacy")
    assert record.get("pipeline_mode") != "simple"
    assert "ocr" in (record.get("extractor_order") or [])


# 15 — Manual comparison never modifies the final AI output -----------------
def test_15_manual_comparison_does_not_modify_ai_output(tmp_path):
    service = FakeMuseService()
    record = _winner_record(tmp_path, "docling", service)
    before_json = record["resume_json"]
    before_used = record["extractor_used"]
    manual = {"tika": FakeExtractor("tika", GOOD_TIKA)}
    run_manual_extractors(record, tmp_path / "resume.pdf", ["tika"],
                          extractors=manual)
    assert record["extractor_results"]["tika"]["status"] == "success"
    assert record["resume_json"] == before_json
    assert record["extractor_used"] == before_used
    assert record["selected_extractor"] == "docling"
    assert len(service.calls) == 1  # no extra LLM call was made


# Extra — process_resume_file defaults to the simple pipeline ---------------
def test_default_mode_is_simple(tmp_path, monkeypatch):
    import api.pipeline as ap

    called = {}

    def fake_simple(file_path, *, resume_id, original_filename,
                    muse_service=None, pipeline=None):
        called["mode"] = "simple"
        return {"resume_id": resume_id, "pipeline_mode": "simple"}

    monkeypatch.setattr(ap, "process_resume_file_simple", fake_simple)
    monkeypatch.delenv("SATVIQ_PIPELINE_MODE", raising=False)
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF")
    record = ap.process_resume_file(
        pdf, resume_id="flow-default-01", original_filename="r.pdf")
    assert record["pipeline_mode"] == "simple"
    assert called["mode"] == "simple"


# Extra — reprocess updates the SAME record, preserving manual runs ---------
def test_reprocess_updates_existing_record():
    document = pymupdf.open()
    page = document.new_page()
    y = 60
    for line in GOOD_TEXT.splitlines():
        page.insert_text((50, y), line, fontsize=10)
        y += 18
    payload = document.tobytes()
    document.close()
    created = client.post(
        "/api/resumes/process",
        files=[("files", ("reprocess_me.pdf", payload, "application/pdf"))],
    ).json()["results"][0]
    resume_id = created["resume_id"]
    try:
        total_before = client.get("/api/resumes").json()["total"]
        # A manual comparison run exists on the record.
        ran = client.post(f"/api/resumes/{resume_id}/extractors/run",
                          json={"extractors": ["pymupdf"]})
        assert ran.status_code == 200

        response = client.post(f"/api/resumes/{resume_id}/reprocess")
        assert response.status_code == 200
        updated = response.json()
        assert updated["resume_id"] == resume_id  # same record
        assert updated["pipeline_mode"] == "simple"
        assert updated["uploaded_at"] == created["uploaded_at"]
        assert updated.get("extractor_results"), \
            "manual comparison results must survive reprocessing"
        total_after = client.get("/api/resumes").json()["total"]
        assert total_after == total_before  # no duplicate history entry

        assert client.post(
            "/api/resumes/does-not-exist-1234/reprocess").status_code == 404
        orphan = dict(created)
        orphan["resume_id"] = "flow-orphan-0001"
        orphan["_stored_name"] = None
        store.save_record(orphan)
        try:
            assert client.post(
                "/api/resumes/flow-orphan-0001/reprocess").status_code == 409
        finally:
            store.delete_record("flow-orphan-0001")
    finally:
        client.delete(f"/api/resumes/{resume_id}")


# --- On-demand AI generation (AI Output tab button) ------------------------

class _InvalidService(FakeMuseService):
    def generate_resume_json(self, text, hyperlinks=None):
        self.calls.append(text)
        return {"ok": False,
                "error": "JSON validation failed: not valid JSON.",
                "error_type": "ValidationError",
                "resume_json": None, "validation": "invalid",
                "provider": "Muse AI", "model": "fake-model",
                "raw_text": "this is not json",
                "usage": dict(USAGE), "duration_ms": 5.0}


class _UnconfiguredService:
    configured = False
    model = "muse-spark-1.3"
    provider_name = "Muse AI"
    configuration_error = "MUSE_API_KEY is not set."

    def generate_resume_json(self, text, hyperlinks=None):
        raise AssertionError("must not be called when unconfigured")


def test_16_on_demand_generation_after_failure(tmp_path):
    service = _InvalidService()
    record = _winner_record(tmp_path, "docling", service)
    assert record["resume_json"] is None
    assert record["llm_status"] == "failed"
    attempts_before = [(a["extractor_name"], a.get("extracted_text"))
                       for a in record["attempts"]]

    good = FakeMuseService()
    record, attempt = generate_ai_output_for_record(record, service=good)
    assert attempt["status"] == "success"
    # Exactly ONE call, carrying EXACTLY the stored selected
    # normalized text — no re-extraction, no other extractor's text.
    assert good.calls == [record["text_normalization"]["normalized_text"]]
    assert "DOCLINGMARKER" in good.calls[0]
    assert "TIKAMARKER" not in good.calls[0]
    assert "PYMUPDFMARKER" not in good.calls[0]
    # The extractors were NOT re-run.
    assert [(a["extractor_name"], a.get("extracted_text"))
            for a in record["attempts"]] == attempts_before
    # Validated JSON is now persisted everywhere the tab reads.
    assert record["resume_json"]["full_name"] == "Jane Doe"
    assert record["resume_data"] == record["resume_json"]
    assert record["resume_json_meta"]["validation"] == "valid"
    assert record["llm_status"] == "success"
    # Both real Muse calls are accounted for.
    assert len(record["muse_requests"]) == 2
    assert record["token_usage"]["total_tokens"] == 280
    assert record["pipeline_trace"][-1]["status"] == "PASS"
    muse_stage = next(s for s in record["pipeline_trace"]
                      if s["stage"].startswith("Muse AI — Resume JSON"))
    assert muse_stage["status"] == "PASS"


def test_17_on_demand_not_configured_keeps_existing_json(tmp_path):
    service = FakeMuseService()
    record = _winner_record(tmp_path, "tika", service)
    before = record["resume_json"]
    record, attempt = generate_ai_output_for_record(
        record, service=_UnconfiguredService())
    assert attempt["status"] == "not_configured"
    assert "MUSE_API_KEY" in attempt["error"]
    assert record["ai_last_attempt"]["status"] == "not_configured"
    # No call was made and the stored output is untouched.
    assert record["resume_json"] == before
    assert record["llm_status"] == "success"
    assert len(record["muse_requests"]) == 1


def test_18_on_demand_failure_keeps_existing_json(tmp_path):
    service = FakeMuseService()
    record = _winner_record(tmp_path, "pymupdf", service)
    before = record["resume_json"]
    bad = _InvalidService()
    record, attempt = generate_ai_output_for_record(record, service=bad)
    assert attempt["status"] == "failed"
    assert record["ai_last_attempt"]["error"]
    # The failed regeneration did NOT destroy the stored JSON.
    assert record["resume_json"] == before
    assert record["llm_status"] == "success"
    assert len(record["muse_requests"]) == 2
    assert len(bad.calls) == 1


def test_19_on_demand_endpoint():
    document = pymupdf.open()
    page = document.new_page()
    y = 60
    for line in GOOD_TEXT.splitlines():
        page.insert_text((50, y), line, fontsize=10)
        y += 18
    payload = document.tobytes()
    document.close()
    created = client.post(
        "/api/resumes/process",
        files=[("files", ("ondemand.pdf", payload, "application/pdf"))],
    ).json()["results"][0]
    resume_id = created["resume_id"]
    try:
        # Muse is unconfigured in the test environment: the button
        # endpoint reports that honestly on the SAME record.
        response = client.post(f"/api/resumes/{resume_id}/ai/generate")
        assert response.status_code == 200
        body = response.json()
        assert body["resume_id"] == resume_id
        assert body["ai_last_attempt"]["status"] == "not_configured"
        assert body["resume_json"] is None
        assert body["llm_status"] == "not_configured"

        assert client.post(
            "/api/resumes/does-not-exist-1234/ai/generate"
        ).status_code == 404

        # A record with no selected text has nothing to send → 409.
        bad = client.post(
            "/api/resumes/process",
            files=[("files", ("corrupt.pdf", b"this is not a pdf",
                              "application/pdf"))]).json()["results"][0]
        try:
            assert client.post(
                f"/api/resumes/{bad['resume_id']}/ai/generate"
            ).status_code == 409
        finally:
            client.delete(f"/api/resumes/{bad['resume_id']}")
    finally:
        client.delete(f"/api/resumes/{resume_id}")


def test_20_on_demand_frontend_wiring():
    root = Path(__file__).resolve().parent.parent
    app_js = (root / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    api_js = (root / "frontend" / "js" / "api.js").read_text(encoding="utf-8")
    assert "generateAiOutput" in api_js and "/ai/generate" in api_js
    assert "generateAiBtn" in app_js
    assert "Generate AI Output" in app_js
    assert "Regenerate AI Output" in app_js
    assert "ai_last_attempt" in app_js
