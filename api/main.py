"""SATVIQ — Resume Intelligence: FastAPI application.

Serves the vanilla HTML/CSS/JS frontend and the JSON API that runs the
existing extraction + quality-check pipeline. This module contains no
extraction or quality logic — it validates uploads, stores them safely,
delegates to ``api.pipeline`` (which delegates to the existing
``extractor`` and ``quality_checker`` packages), and persists results
via ``api.store``.

Run from the project root:
    uvicorn api.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from extractor.manager import load_manager_config

from . import analytics, extractor_diagnostics, store
from .pipeline import (
    MANUAL_EXTRACTORS,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_MB,
    SUPPORTED_EXTENSIONS,
    ai_generation_in_progress,
    failed_record,
    generate_ai_output_for_record,
    process_resume_file,
    run_manual_extractors,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"

app = FastAPI(title="SATVIQ — Resume Intelligence", version="1.0.0")
store.ensure_dirs()


@app.on_event("startup")
def _check_database() -> None:
    """PostgreSQL is the primary store: verify the connection at boot
    and log the result loudly. A failure here does not block boot —
    the store raises a clear error on the first read/write instead —
    but the console and log show the database state immediately."""
    import logging
    log = logging.getLogger(__name__)
    try:
        from . import db
        status = db.check_connection()
        if status["ok"]:
            log.info("Database check: %s", status["detail"])
            print(f"[SATVIQ] Database: {status['detail']}")
        else:
            log.error("Database check FAILED: %s", status["detail"])
            print(f"[SATVIQ] WARNING — database unavailable: "
                  f"{status['detail']}")
    except Exception:  # noqa: BLE001 - startup check never blocks boot
        log.exception("Database startup check raised")


@app.middleware("http")
async def _frontend_no_cache(request: Request, call_next):
    """Never let browsers heuristically cache the frontend.

    The dashboard's HTML/CSS/JS evolve with the backend's response
    shape (e.g. the per-resume ``pipeline_trace``). A stale cached
    ``app.js`` paired with fresh HTML leaves the trace/flow sections
    empty even though the API data is correct, so frontend responses
    must be revalidated on every load. API responses are unaffected.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitise_filename(raw_name: str | None) -> str:
    """Reduce an uploaded filename to a safe basename.

    Strips any directory components (path traversal), replaces unsafe
    characters, and guarantees a non-empty result.
    """
    name = Path(raw_name or "resume").name  # drops ../ and folders
    name = _UNSAFE_CHARS.sub("_", name).strip("._")
    if not name or name.startswith("."):
        name = f"resume{name}"
    return name[:120]


def _extractor_order() -> list[str]:
    try:
        config = load_manager_config()
        return list(config.get("manager", {}).get("order", []))
    except Exception:
        logger.exception("Could not read extractor config")
        return []


def _quality_gate_config() -> dict:
    try:
        config = load_manager_config()
        return dict(config.get("quality_gate", {}) or {})
    except Exception:
        logger.exception("Could not read quality gate config")
        return {}


# ------------------------------------------------------------------------
# API endpoints
# ------------------------------------------------------------------------

def _ai_status() -> dict:
    """Muse AI status (never includes the API key)."""
    try:
        from muse_ai import load_muse_config

        return load_muse_config().public_status()
    except Exception:
        return {"provider": "Muse AI", "configured": False,
                "error": "Muse configuration could not be loaded."}


def _simple_pipeline_info() -> dict:
    try:
        from pipeline import load_pipeline_config

        cfg = load_pipeline_config()
        return {"extractors": ["tika", "pymupdf", "docling"],
                "min_quality_threshold": cfg.min_quality_threshold}
    except Exception:
        return {"extractors": ["tika", "pymupdf", "docling"],
                "min_quality_threshold": 60.0}


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "SATVIQ — Resume Intelligence",
        "extractor_order": _extractor_order(),
        "quality_filters": ["text_health", "completeness", "reading_order"],
        "quality_gate": _quality_gate_config(),
        "ai": _ai_status(),
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
        "max_upload_mb": MAX_UPLOAD_MB,
        "simple_pipeline": _simple_pipeline_info(),
    }


@app.get("/api/extractors/status")
def extractors_status() -> dict:
    """One-click extractor check (Settings → Extractor Status):
    an installation probe for Tika / PyMuPDF / Docling / OCR —
    Java + JAR, packages, Tesseract binary — with each extractor's
    own precise error when something is missing."""
    return extractor_diagnostics.probe_extractors()


@app.post("/api/extractors/test")
def extractors_test() -> dict:
    """Live extractor test: really extract the bundled
    sample_resume.pdf with every extractor (production code paths)
    and report per-extractor status, characters, duration and the
    real error on failure."""
    path = extractor_diagnostics.sample_path()
    if not path.exists():
        raise HTTPException(
            status_code=409,
            detail=f"{path.name} was not found in the project root; "
                   "the live extractor test needs the bundled "
                   "sample resume.")
    return extractor_diagnostics.run_live_tests(path)


@app.get("/api/stats")
def stats() -> dict:
    return store.compute_stats(store.list_records())


@app.get("/api/analytics/summary")
def analytics_summary(days: int | None = None, include_manual: bool = False) -> dict:
    """Analytics Phase 1 aggregation over stored records (api/analytics.py).

    ``days`` limits to records uploaded within the last N days (omitted =
    all time). ``include_manual`` is accepted for the Phase 2 toggle shape;
    Phase 1 always excludes Manual Comparison runs from pipeline stats.
    """
    records = store.list_records()
    result = analytics.compute_analytics(
        records, days=days, include_manual=include_manual)
    result["ai_costs"] = _ai_costs_block(records)
    return result


def _ai_costs_block(records: list) -> dict:
    """AI expenditure block for Analytics. Prefers SQL aggregates
    over the ai_api_costs table (PostgreSQL); falls back to deriving
    from the stored records when no database is configured."""
    from decimal import Decimal

    from . import db as _db
    from .ai_costs import derive_call_rows, jsonable, summarize_rows

    sql = None
    try:
        sql = _db.ai_cost_aggregates()
    except Exception:  # noqa: BLE001
        sql = None
    if sql is not None:
        by_op = sql["by_operation"]
        total = sql.get("total_cost_usd")
        resumes = sql.get("resumes_with_costs") or 0
        avg = (total / resumes) if (total is not None and resumes)             else None
        return jsonable({
            "source": "postgresql",
            "total_cost_usd": total,
            "total_cost_inr": sql.get("total_cost_inr"),
            "average_cost_per_resume_usd": avg,
            "resumes_with_costs": resumes,
            "json_extraction": by_op.get("json_extraction"),
            "ai_evaluation": by_op.get("ai_evaluation"),
            "most_expensive_resume": sql.get("most_expensive_resume"),
            "monthly": sql.get("monthly") or [],
        })
    all_rows: list = []
    per_resume: dict[str, Decimal] = {}
    filenames: dict[str, str] = {}
    for record in records:
        rows = derive_call_rows(record)
        all_rows.extend(rows)
        subtotal = sum((r["total_cost_usd"] for r in rows
                        if r.get("total_cost_usd") is not None),
                       Decimal("0"))
        if rows:
            per_resume[record["resume_id"]] = subtotal
            filenames[record["resume_id"]] = record.get("filename")
    summary = summarize_rows(all_rows)
    total = summary["totals"]["cost_usd"]
    avg = (total / len(per_resume)) if (total is not None
                                        and per_resume) else None
    top_id = max(per_resume, key=per_resume.get) if per_resume else None
    return jsonable({
        "source": "records",
        "total_cost_usd": total,
        "total_cost_inr": summary["totals"]["cost_inr"],
        "average_cost_per_resume_usd": avg,
        "resumes_with_costs": len(per_resume),
        "json_extraction": summary["json_extraction"],
        "ai_evaluation": summary["ai_evaluation"],
        "most_expensive_resume": (
            {"resume_id": top_id, "filename": filenames.get(top_id),
             "cost_usd": per_resume[top_id]} if top_id else None),
        "monthly": [],
    })


@app.post("/api/resumes/process")
async def process_resumes(files: list[UploadFile] = File(...)) -> dict:
    """Process one or more uploaded resumes through the full pipeline.

    This is the standard upload flow and it runs the DEFAULT
    pipeline (``pipeline_mode="simple"``): Tika + PyMuPDF + Docling
    concurrently → quality evaluation → best-extractor selection →
    normalization of the selected text only → one Muse AI call →
    schema-validated Resume JSON, persisted on the record.

    Each file gets its own record — a bad file never fails the batch.
    Extraction runs in a threadpool so the server stays responsive.
    """
    results: list[dict] = []
    for upload in files:
        resume_id = str(uuid.uuid4())
        safe_name = _sanitise_filename(upload.filename)
        ext = Path(safe_name).suffix.lower().lstrip(".")
        stored_name = f"{resume_id}_{safe_name}"
        stored_path = store.UPLOADS_DIR / stored_name

        # Stream to disk with a hard size cap; never load whole files
        # into memory and never execute them.
        size = 0
        too_large = False
        with stored_path.open("wb") as out:
            while True:
                chunk = await upload.read(1024 * 256)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    too_large = True
                    break
                out.write(chunk)
        await upload.close()

        if too_large:
            stored_path.unlink(missing_ok=True)
            record = failed_record(
                resume_id=resume_id, original_filename=safe_name,
                file_type=ext or None, mime_type=upload.content_type,
                file_size=size,
                error=(f"File exceeds the {MAX_UPLOAD_MB} MB limit. "
                "Please upload a smaller resume."),
            )
        elif size == 0:
            stored_path.unlink(missing_ok=True)
            record = failed_record(
                resume_id=resume_id, original_filename=safe_name,
                file_type=ext or None, mime_type=upload.content_type,
                file_size=0, error="File is empty.",
            )
        elif ext not in SUPPORTED_EXTENSIONS:
            stored_path.unlink(missing_ok=True)
            record = failed_record(
                resume_id=resume_id, original_filename=safe_name,
                file_type=ext or None, mime_type=upload.content_type,
                file_size=size,
                error=(f"Unsupported file type '.{ext}'. Supported types: "
                       + ", ".join(sorted(SUPPORTED_EXTENSIONS)) + "."),
            )
        else:
            record = await run_in_threadpool(
                process_resume_file, stored_path,
                resume_id=resume_id, original_filename=safe_name,
                pipeline_mode="simple",
            )
            record["_stored_name"] = stored_name

        if "_stored_name" not in record:
            record["_stored_name"] = None
        store.save_record(record)
        results.append(store.public(record))

    return {"results": results}


@app.post("/api/resumes/process-simple")
async def process_resumes_simple(files: list[UploadFile] = File(...)) -> dict:
    """Process uploads through the SIMPLE pipeline (pipeline_mode=simple).

    Kept as an explicit alias: /api/resumes/process now runs this
    same default pipeline. Runs Tika + PyMuPDF + Docling
    concurrently, quality-scores all three, selects the best,
    normalizes the selected text, and calls the LLM once. Returns
    the same {"results": [...]} shape with backward-compatible
    records.
    """
    results: list[dict] = []
    for upload in files:
        resume_id = str(uuid.uuid4())
        safe_name = _sanitise_filename(upload.filename)
        ext = Path(safe_name).suffix.lower().lstrip(".")
        stored_name = f"{resume_id}_{safe_name}"
        stored_path = store.UPLOADS_DIR / stored_name
        size = 0
        too_large = False
        with stored_path.open("wb") as out:
            while True:
                chunk = await upload.read(1024 * 256)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    too_large = True
                    break
                out.write(chunk)
        await upload.close()
        if too_large:
            stored_path.unlink(missing_ok=True)
            record = failed_record(
                resume_id=resume_id, original_filename=safe_name,
                file_type=ext or None, mime_type=upload.content_type,
                file_size=size,
                error=(f"File exceeds the {MAX_UPLOAD_MB} MB limit. "
                       "Please upload a smaller resume."),
            )
        elif size == 0:
            stored_path.unlink(missing_ok=True)
            record = failed_record(
                resume_id=resume_id, original_filename=safe_name,
                file_type=ext or None, mime_type=upload.content_type,
                file_size=0, error="File is empty.",
            )
        elif ext not in SUPPORTED_EXTENSIONS:
            stored_path.unlink(missing_ok=True)
            record = failed_record(
                resume_id=resume_id, original_filename=safe_name,
                file_type=ext or None, mime_type=upload.content_type,
                file_size=size,
                error=(f"Unsupported file type '.{ext}'. Supported types: "
                       + ", ".join(sorted(SUPPORTED_EXTENSIONS)) + "."),
            )
        else:
            record = await run_in_threadpool(
                process_resume_file, stored_path,
                resume_id=resume_id, original_filename=safe_name,
                pipeline_mode="simple",
            )
            record["_stored_name"] = stored_name
        if "_stored_name" not in record:
            record["_stored_name"] = None
        store.save_record(record)
        results.append(store.public(record))
    return {"results": results}


@app.get("/api/resumes")
def list_resumes(q: str | None = None, status: str | None = None,
                 page: int | None = None,
                 page_size: int | None = None) -> dict:
    """Resume history. Without parameters this returns every summary
    (the original behaviour). ``q`` filters by filename (substring,
    case-insensitive), ``status`` by quality status (PASS / WARNING /
    FAIL) or ``failed`` for unsuccessful records, and ``page`` /
    ``page_size`` paginate server-side so the UI never has to load
    the entire history at once."""
    records = store.list_records()
    if q:
        needle = q.strip().lower()
        records = [r for r in records
                   if needle in (r.get("filename") or "").lower()]
    if status:
        wanted = status.strip().upper()
        if wanted == "FAILED":
            records = [r for r in records if not r.get("success")]
        elif wanted in ("PASS", "WARNING", "FAIL"):
            records = [r for r in records
                       if (r.get("final_quality_status")
                           or r.get("quality_status")) == wanted]
    total = len(records)
    if page is None and page_size is None:
        return {"resumes": [store.summary(r) for r in records],
                "total": total, "page": 1,
                "page_size": total, "pages": 1 if total else 0}
    size = min(max(page_size or 10, 1), 100)
    current = max(page or 1, 1)
    pages = (total + size - 1) // size if total else 0
    start = (current - 1) * size
    return {"resumes": [store.summary(r)
                        for r in records[start:start + size]],
            "total": total, "page": current,
            "page_size": size, "pages": pages}


@app.get("/api/resumes/{resume_id}", response_model=None)
def get_resume(resume_id: str) -> JSONResponse:
    record = store.get_record(resume_id)
    if record is None:
        return JSONResponse(status_code=404, content={"detail": "Resume record not found."})
    return JSONResponse(content=store.public(record))


@app.post("/api/resumes/{resume_id}/extractors/run", response_model=None)
async def run_manual_extractors_endpoint(resume_id: str,
                                         request: Request) -> JSONResponse:
    """Run Tika / PyMuPDF / Docling independently on this resume's
    stored original, for manual comparison in the Inspector.

    Body: {"extractors": ["tika", ...]} or {"extractor": "tika"};
    no body / empty list runs all three. Results are stored under the
    record's ``extractor_results`` only — the pipeline's attempts, the
    selected extractor, the final text and the Resume JSON are never
    touched, and no LLM call is made.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    names = body.get("extractors")
    if not names and body.get("extractor"):
        names = [body["extractor"]]
    if not names:
        names = list(MANUAL_EXTRACTORS)
    if not isinstance(names, list) or \
            any(name not in MANUAL_EXTRACTORS for name in names):
        raise HTTPException(
            400, "extractors must be a list containing only: "
            + ", ".join(MANUAL_EXTRACTORS) + ".")
    names = list(dict.fromkeys(names))
    raw = store.load_raw_record(resume_id)
    if raw is None:
        return JSONResponse(status_code=404,
                            content={"detail": "Resume record not found."})
    stored_name = raw.get("_stored_name")
    original_path = store.UPLOADS_DIR / stored_name if stored_name else None
    if original_path is None or not original_path.is_file():
        return JSONResponse(
            status_code=409,
            content={"detail": "The original uploaded file is not "
                               "available for this record, so manual "
                               "extraction cannot run."})
    await run_in_threadpool(run_manual_extractors, raw, original_path, names)
    store.save_record(raw)
    updated = store.get_record(resume_id)
    return JSONResponse(content={
        "extractor_results": (updated or {}).get("extractor_results", {}),
        "record": store.public(updated) if updated else None,
    })


@app.post("/api/resumes/{resume_id}/reprocess", response_model=None)
async def reprocess_resume(resume_id: str) -> JSONResponse:
    """Reprocess an EXISTING record through the default pipeline.

    For records created by the old (legacy) pipeline — or any record
    whose AI stage never produced Resume JSON — this re-runs the
    stored original file through the current default flow
    (selection → selected-text normalization → Muse AI → validated
    Resume JSON) and UPDATES the same record in place: same
    ``resume_id``, same original upload timestamp, and any Manual
    Comparison results are preserved. No duplicate history entry is
    created. Returns 409 when the original uploaded file is no
    longer available.
    """
    raw = store.load_raw_record(resume_id)
    if raw is None:
        return JSONResponse(status_code=404,
                            content={"detail": "Resume record not found."})
    stored_name = raw.get("_stored_name")
    original_path = store.UPLOADS_DIR / stored_name if stored_name else None
    if original_path is None or not original_path.is_file():
        return JSONResponse(
            status_code=409,
            content={"detail": "The original uploaded file is not "
                               "available for this record, so it "
                               "cannot be reprocessed."})
    record = await run_in_threadpool(
        process_resume_file, original_path,
        resume_id=resume_id,
        original_filename=raw.get("filename") or stored_name,
        pipeline_mode="simple",
    )
    record["_stored_name"] = stored_name
    if raw.get("uploaded_at"):
        record["uploaded_at"] = raw["uploaded_at"]
    if raw.get("extractor_results"):
        record["extractor_results"] = raw["extractor_results"]
    store.save_record(record)
    return JSONResponse(content=store.public(record))


@app.post("/api/resumes/{resume_id}/ai/generate", response_model=None)
async def generate_resume_ai_output(resume_id: str) -> JSONResponse:
    """Generate the AI Output for an EXISTING record, on demand.

    Sends ONLY the record's stored Selected Normalized Text to
    Muse AI — exactly one Muse call; the extractors are NOT
    re-run and no other extractor's text is sent. Used by the AI
    Output tab's Generate button for testing a (new) API key
    without re-uploading or re-extracting. The same record is
    updated in place: on success the validated Resume JSON is
    stored; on failure any previously stored JSON is kept and the
    real error is recorded in ``ai_last_attempt``. Returns 409 when
    the record holds no selected normalized text to send.
    """
    record = store.get_record(resume_id)
    if record is None:
        return JSONResponse(status_code=404,
                            content={"detail": "Resume record not found."})
    if ai_generation_in_progress(resume_id):
        return JSONResponse(
            status_code=409,
            content={"detail": "AI generation is already running "
                               "for this resume. Wait for it to "
                               "finish — a slow Muse response is "
                               "still running, not failed."})
    # Persist a RUNNING status BEFORE the (possibly long) Muse
    # call, so the UI shows the actual state while it processes;
    # the final save below replaces it with the real outcome. The
    # Muse call itself has NO timeout. A client disconnect cannot
    # corrupt the record: the generation's result is written in
    # the single final save, never partially.
    previous_attempt = record.get("ai_last_attempt")
    record["ai_last_attempt"] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "status": "running", "ok": False, "provider": "Muse AI",
        "model": None, "validation": None, "error": None,
        "error_type": None, "duration_ms": None}
    store.save_record(record)
    record, attempt = await run_in_threadpool(
        generate_ai_output_for_record, record)
    if attempt.get("status") == "no_text":
        record["ai_last_attempt"] = previous_attempt
        store.save_record(record)
        return JSONResponse(status_code=409,
                            content={"detail": attempt.get("error")})
    if attempt.get("status") == "already_running":
        return JSONResponse(status_code=409,
                            content={"detail": attempt.get("error")})
    store.save_record(record)
    return JSONResponse(content=store.public(record))


# ------------------------------------------------------------------------
# AI Evaluation (evaluation/ package) — Muse AI judgement of the
# already-processed record: section intelligence, three independent
# scores, role suitability. Extraction is never re-run; exactly one
# evaluation call is made per request (plus transient retries).
# ------------------------------------------------------------------------

@app.post("/api/resumes/{resume_id}/analyze", response_model=None)
async def analyze_resume(resume_id: str) -> JSONResponse:
    """Run the Muse AI evaluation for an EXISTING record.

    The evaluation is based only on the record's stored Selected
    Normalized Text (+ deterministic Quality Checker context + the
    stored Resume JSON digest). The attempt — success or failure —
    is appended to the record's analysis history; a failure never
    overwrites the latest successful analysis, whose scores keep
    being served. Returns 409 when an evaluation for this resume is
    already running or the feature is disabled. Like the AI
    generation endpoint, a Muse failure is surfaced in the
    returned entry (``status: "failed"`` + ``error``), not hidden.
    """
    from evaluation.service import analysis_in_progress, analyze_record

    record = store.get_record(resume_id)
    if record is None:
        return JSONResponse(status_code=404,
                            content={"detail": "Resume record not found."})
    if analysis_in_progress(resume_id):
        return JSONResponse(
            status_code=409,
            content={"detail": "AI evaluation is already running for "
                               "this resume. Wait for it to finish — "
                               "a slow Muse response is still "
                               "running, not failed."})
    entry, error = await run_in_threadpool(analyze_record, record)
    if entry is None:
        detail = ("AI evaluation is disabled "
                  "(SATVIQ_EVALUATION_ENABLED=false)."
                  if error == "evaluation_disabled"
                  else "AI evaluation is already running for this "
                       "resume.")
        return JSONResponse(status_code=409, content={"detail": detail})
    updated = store.get_record(resume_id)
    return JSONResponse(content={
        "analysis": entry,
        "record": store.public(updated) if updated else None,
    })


@app.get("/api/resumes/{resume_id}/analysis", response_model=None)
def get_resume_analysis(resume_id: str) -> JSONResponse:
    """The resume's latest SUCCESSFUL evaluation, or null."""
    from evaluation import store as evaluation_store

    record = store.get_record(resume_id)
    if record is None:
        return JSONResponse(status_code=404,
                            content={"detail": "Resume record not found."})
    return JSONResponse(content={
        "analysis": evaluation_store.latest_success(record)})


@app.get("/api/resumes/{resume_id}/analysis/history", response_model=None)
def get_resume_analysis_history(resume_id: str) -> JSONResponse:
    """Every evaluation attempt for the resume (oldest first)."""
    from evaluation import store as evaluation_store

    record = store.get_record(resume_id)
    if record is None:
        return JSONResponse(status_code=404,
                            content={"detail": "Resume record not found."})
    return JSONResponse(content={
        "history": evaluation_store.history(record)})


@app.get("/api/resumes/{resume_id}/canonical-evaluation",
         response_model=None)
def get_canonical_evaluation(resume_id: str) -> JSONResponse:
    """The canonical AI evaluation document (schema ai-evaluation/1.0)
    for one resume: served from PostgreSQL when stored there, built
    on demand from the stored record otherwise. The existing
    evaluation endpoints and shapes are unchanged — this is an
    additional, versioned output."""
    from . import db as _db

    stored = None
    try:
        stored = _db.fetch_canonical(resume_id)
    except Exception:  # noqa: BLE001 - fall back to on-demand build
        stored = None
    if stored is not None:
        return JSONResponse(content=stored)
    record = store.get_record(resume_id)
    if record is None:
        return JSONResponse(status_code=404,
                            content={"detail": "Resume record not found."})
    from candidate_intelligence import build_canonical, check_canonical

    ok, result = check_canonical(build_canonical(record))
    if not ok:
        return JSONResponse(status_code=422, content={
            "detail": "Canonical build failed validation.",
            "errors": result[:10]})
    return JSONResponse(content=result.model_dump(mode="json"))


@app.get("/api/evaluation/config")
def get_evaluation_config() -> dict:
    """Evaluation configuration metadata: versions, classification
    bands, section and role catalogues. Metadata only — no
    credentials, no prompt text."""
    from evaluation.config import public_metadata

    return public_metadata()


@app.get("/api/resumes/{resume_id}/text", response_model=None)
def get_resume_text(resume_id: str) -> PlainTextResponse | JSONResponse:
    record = store.get_record(resume_id)
    if record is None:
        return JSONResponse(status_code=404, content={"detail": "Resume record not found."})
    text = record.get("extracted_text") or ""
    base = Path(record.get("filename", "resume")).stem or "resume"
    return PlainTextResponse(
        content=text, media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{base}_extracted.txt"'},
    )


@app.get("/api/resumes/{resume_id}/file", response_model=None)
def download_original_file(resume_id: str):
    """Download the originally uploaded file for a stored record."""
    record = store.get_record(resume_id)
    if record is None:
        return JSONResponse(status_code=404,
                            content={"detail": "Resume record not found."})
    stored_name = record.get("_stored_name")
    if not stored_name:
        return JSONResponse(
            status_code=404,
            content={"detail": "Original file is not available for this record."})
    candidate = (store.UPLOADS_DIR / stored_name).resolve()
    if candidate.parent != store.UPLOADS_DIR.resolve() \
            or not candidate.is_file():
        return JSONResponse(
            status_code=404,
            content={"detail": "Original file is not available for this record."})
    return FileResponse(
        path=str(candidate),
        filename=record.get("filename") or candidate.name,
        media_type=record.get("mime_type") or "application/octet-stream")


@app.delete("/api/resumes/{resume_id}", response_model=None)
def delete_resume(resume_id: str) -> JSONResponse:
    if not store.delete_record(resume_id):
        return JSONResponse(status_code=404, content={"detail": "Resume record not found."})
    return JSONResponse(content={"deleted": True, "resume_id": resume_id})


# ------------------------------------------------------------------------
# Settings — LLM system prompt (provider/model come from /api/health;
# the API key is backend-only and is never exposed here)
# ------------------------------------------------------------------------

@app.get("/api/settings/system-prompt")
def get_system_prompt() -> dict:
    from muse_ai.prompt_config import prompt_status

    status = prompt_status()
    status["ai"] = _ai_status()
    return status


@app.put("/api/settings/system-prompt")
async def save_system_prompt(request: Request) -> JSONResponse:
    from muse_ai.prompt_config import save_prompt

    payload = await request.json()
    try:
        status = save_prompt(payload.get("system_prompt"))
    except (ValueError, TypeError) as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    status["ai"] = _ai_status()
    return JSONResponse(content=status)


@app.post("/api/settings/system-prompt/reset")
def reset_system_prompt() -> dict:
    from muse_ai.prompt_config import reset_prompt

    status = reset_prompt()
    status["ai"] = _ai_status()
    return status


@app.post("/api/settings/system-prompt/test")
async def test_system_prompt(request: Request) -> dict:
    """Run ONE real Muse request with the candidate (or saved)
    prompt against a tiny sample text and report the genuine result:
    JSON validation, duration and provider-reported token usage.
    When Muse is not configured, that is reported instead — no
    request is made and nothing is simulated."""
    from muse_ai import MuseService, load_muse_config
    from muse_ai.prompt_config import validate_prompt

    payload = await request.json() if await request.body() else {}
    candidate = payload.get("system_prompt")
    if candidate is not None:
        try:
            validate_prompt(candidate)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc)}
    config = load_muse_config()
    if not config.configured:
        return {"ok": False, "configured": False,
                "provider": "Muse AI",
                "model": config.model,
                "error": config.configuration_error}
    service = MuseService(config)
    if candidate is not None:
        # Test the candidate text without saving it: temporarily run
        # the same chat call the text stage makes.
        from muse_ai.service import _TEXT_PROMPT

        try:
            result = service.client.chat_text(
                candidate,
                _TEXT_PROMPT + "\n\nRESUME TEXT:\nTest Candidate\n"
                "test.candidate@example.com\nPython developer.")
            from muse_ai.schema import validate_resume_json

            parsed, error = validate_resume_json(result["text"])
            return {"ok": parsed is not None, "configured": True,
                    "provider": "Muse AI", "model": result.get("model") or config.model,
                    "validation": "valid" if parsed is not None else "invalid",
                    "error": error, "duration_ms": result.get("duration_ms"),
                    "usage": result.get("usage") or {}}
        except Exception as exc:  # MuseError and transport errors
            return {"ok": False, "configured": True,
                    "provider": "Muse AI", "model": config.model,
                    "error": str(exc)}
    outcome = service.generate_resume_json(
        "Test Candidate\ntest.candidate@example.com\nPython developer.")
    return {"ok": bool(outcome.get("ok")), "configured": True,
            "provider": outcome.get("provider"), "model": outcome.get("model"),
            "validation": outcome.get("validation"),
            "error": outcome.get("error"),
            "duration_ms": outcome.get("duration_ms"),
            "usage": outcome.get("usage") or {}}


@app.delete("/api/resumes")
def delete_all_resumes() -> dict:
    return {"deleted_count": store.clear_records()}


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    # Never leak tracebacks or server paths to the frontend.
    logger.exception("Unhandled error while handling %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


# Frontend (mounted last so /api/* routes take precedence).
@app.get("/api/resumes/{resume_id}/costs", response_model=None)
def get_resume_costs(resume_id: str) -> JSONResponse:
    """Per-resume AI cost breakdown: one row per actual Muse API
    call (JSON extraction and AI evaluation separately), priced
    from provider-reported usage. Stored PostgreSQL rows are
    preferred (immutable history, incl. the FX rate stamped at sync
    time); derivation from the stored record is the fallback."""
    from . import db as _db
    from .ai_costs import derive_call_rows, jsonable, summarize_rows
    from muse_ai import pricing as _pricing

    record = store.get_record(resume_id)
    if record is None:
        return JSONResponse(status_code=404,
                            content={"detail": "Resume record not found."})
    rows = None
    try:
        rows = _db.fetch_resume_costs(resume_id)
    except Exception:  # noqa: BLE001
        rows = None
    if not rows:
        rows = derive_call_rows(record)
    summary = summarize_rows(rows)
    models = sorted({r.get("model") for r in rows if r.get("model")})
    return JSONResponse(content=jsonable({
        "resume_id": resume_id,
        "filename": record.get("filename"),
        "calls": rows,
        "summary": summary,
        "pricing": {
            "provider": _pricing.PROVIDER,
            "currency": _pricing.CURRENCY,
            "rates_per_mtok_usd": {
                m: _pricing.pricing_for(m) for m in models},
            "usd_inr_rate_configured": _pricing.usd_inr_rate(),
        },
    }))


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
