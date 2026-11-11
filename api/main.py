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
    return analytics.compute_analytics(
        store.list_records(), days=days, include_manual=include_manual)


# --- YAML Editor AI (independent module: yaml_editor_ai/) ---------------
# Separate analytics + audit endpoints for the self-improving
# completeness YAML agent. Metadata only — no API credentials and no
# resume content are ever exposed here.

def _yaml_editor_agent():
    from yaml_editor_ai import get_default_agent

    return get_default_agent()


@app.get("/api/yaml-editor/analytics")
def yaml_editor_analytics() -> dict:
    """Aggregated YAML Editor AI analytics (own token store)."""
    return _yaml_editor_agent().analytics()


@app.get("/api/yaml-editor/history")
def yaml_editor_history(limit: int = 50) -> dict:
    """Newest-first execution history (metadata only)."""
    agent = _yaml_editor_agent()
    return {"history": agent.tracker.history(limit=limit)}


@app.get("/api/yaml-editor/versions")
def yaml_editor_versions() -> dict:
    """All YAML versions in the store + the current one."""
    from yaml_editor_ai import version_manager

    agent = _yaml_editor_agent()
    agent.store.ensure_seeded()
    numbers = agent.store.version_numbers()
    return {
        "current_version": version_manager.version_label(
            agent.store.current_version()),
        "versions": [
            {"version": version_manager.version_label(n),
             "file": f"completeness_v{n:03d}.yaml"}
            for n in numbers],
    }


@app.get("/api/yaml-editor/versions/{version}")
def yaml_editor_version_content(version: str) -> dict:
    """The stored content of one YAML version (e.g. v002)."""
    import re as _re

    from yaml_editor_ai import version_manager

    match = _re.fullmatch(r"v(\d{1,4})", version.strip().lower())
    if not match:
        raise HTTPException(status_code=404,
                            detail=f"Unknown YAML version {version!r}.")
    agent = _yaml_editor_agent()
    agent.store.ensure_seeded()
    path = version_manager.version_file(agent.store, int(match.group(1)))
    if not path.is_file():
        raise HTTPException(status_code=404,
                            detail=f"YAML version {version} not found.")
    return {"version": version_manager.version_label(int(match.group(1))),
            "content": path.read_text(encoding="utf-8")}


@app.get("/api/yaml-editor/changelog")
def yaml_editor_changelog() -> dict:
    """The full YAML change audit trail."""
    from yaml_editor_ai import version_manager

    agent = _yaml_editor_agent()
    agent.store.ensure_seeded()
    return {"changelog": version_manager.read_changelog(agent.store)}


@app.get("/api/yaml-editor/active")
def yaml_editor_active() -> dict:
    """The ACTIVE completeness YAML, read from the actual backend
    file (never a frontend copy): content, current version, last
    updated timestamp, total keywords, and per-category keyword
    counts for the viewer."""
    import os as _os

    import yaml as _yaml

    agent = _yaml_editor_agent()
    agent.store.ensure_seeded()
    path = agent.store.active_path
    content = path.read_text(encoding="utf-8")
    parsed = _yaml.safe_load(content) or {}
    categories: dict[str, Any] = {}
    total = 0
    for name, section in (parsed.get("sections") or {}).items():
        section = section or {}
        count = len(section.get("aliases") or []) \
            + len(section.get("conditional_aliases") or [])
        categories[name] = {
            "display_name": section.get("display_name") or name,
            "importance": section.get("importance"),
            "keyword_count": count}
        total += count
    from yaml_editor_ai import version_manager

    return {
        "version": version_manager.version_label(
            agent.store.current_version()),
        "content": content,
        "updated_at": _os.path.getmtime(path),
        "total_keywords": total,
        "categories": categories,
    }


@app.get("/api/yaml-editor/compare")
def yaml_editor_compare(from_version: str, to_version: str) -> dict:
    """Before-vs-after comparison of two stored versions: full
    contents of both, plus the keyword-level diff computed from the
    parsed YAML (additions only — the editor is append-only, so
    removed/modified lists are computed honestly and are empty in
    practice; they are never fabricated)."""
    import re as _re

    import yaml as _yaml

    from yaml_editor_ai import version_manager

    def _load(raw: str) -> tuple[str, Path]:
        match = _re.fullmatch(r"v(\d{1,4})", raw.strip().lower())
        if not match:
            raise HTTPException(status_code=404,
                                detail=f"Unknown YAML version {raw!r}.")
        agent = _yaml_editor_agent()
        agent.store.ensure_seeded()
        path = version_manager.version_file(agent.store,
                                            int(match.group(1)))
        if not path.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"YAML version {raw} not found.")
        return version_manager.version_label(int(match.group(1))), path

    from_label, from_path = _load(from_version)
    to_label, to_path = _load(to_version)
    from_text = from_path.read_text(encoding="utf-8")
    to_text = to_path.read_text(encoding="utf-8")

    def _aliases(text: str) -> dict[str, set[str]]:
        parsed = _yaml.safe_load(text) or {}
        out: dict[str, set[str]] = {}
        for name, section in (parsed.get("sections") or {}).items():
            section = section or {}
            out[name] = {str(a) for a in (section.get("aliases") or [])} \
                | {str(a) for a in (section.get("conditional_aliases")
                                   or [])}
        return out

    before, after = _aliases(from_text), _aliases(to_text)
    added: dict[str, list[str]] = {}
    removed: dict[str, list[str]] = {}
    for name in sorted(set(before) | set(after)):
        new = sorted(after.get(name, set()) - before.get(name, set()))
        gone = sorted(before.get(name, set()) - after.get(name, set()))
        if new:
            added[name] = new
        if gone:
            removed[name] = gone
    changelog = version_manager.read_changelog(_yaml_editor_agent().store)
    trigger = next((e for e in reversed(changelog)
                    if e.get("new_version") == to_label
                    and e.get("status") == "success"), None)
    return {
        "from_version": from_label, "to_version": to_label,
        "from_content": from_text, "to_content": to_text,
        "added": added, "removed": removed,
        "modified_sections": sorted(set(added) | set(removed)),
        "trigger": ({"resume_id": trigger.get("resume_id"),
                     "filename": trigger.get("filename"),
                     "timestamp": trigger.get("timestamp")}
                    if trigger else None),
    }


@app.get("/api/resumes/{resume_id}/yaml-editor")
def resume_yaml_editor(resume_id: str) -> dict:
    """One resume's YAML Editor AI activity (stored block, or the
    history-derived block for older records)."""
    record = store.get_record(resume_id)
    if record is None:
        raise HTTPException(status_code=404,
                            detail="Resume record not found.")
    return {"resume_id": resume_id,
            "yaml_editor": store.public(record)["yaml_editor"]}


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
