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
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from extractor.manager import load_manager_config

from . import store
from .pipeline import (
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_MB,
    SUPPORTED_EXTENSIONS,
    failed_record,
    process_resume_file,
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
    }


@app.get("/api/stats")
def stats() -> dict:
    return store.compute_stats(store.list_records())


@app.post("/api/resumes/process")
async def process_resumes(files: list[UploadFile] = File(...),
                          extraction_timeout: int = Form(20)) -> dict:
    """Process one or more uploaded resumes through the full pipeline.

    Each file gets its own record — a bad file never fails the batch.
    Extraction runs in a threadpool so the server stays responsive.
    ``extraction_timeout`` is the frontend's per-attempt timeout in
    seconds (UI offers 15 or 20); it is enforced on the backend for
    every extraction attempt, including AI stages.
    """
    if not 1 <= extraction_timeout <= 300:
        raise HTTPException(
            400, "extraction_timeout must be between 1 and 300 seconds.")
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
                extraction_timeout_seconds=float(extraction_timeout),
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
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
