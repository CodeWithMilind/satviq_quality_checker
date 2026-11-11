"""PostgreSQL persistence for SATVIQ candidate records (schema V2).

This module is the ONLY place that talks to PostgreSQL. It projects
a SATVIQ pipeline record (the same dict the JSON store keeps) into
the V2 relational schema:

    candidates                    (V1 columns + V2 additions)
    candidate_experiences         (resume_json.experience[])
    candidate_education           (resume_json.education[])
    candidate_projects            (projects parsed from resume_json)
    candidate_evaluations         (record.analyses[] — full history)
    candidate_role_suitability    (evaluation.role_suitability[])
    candidate_section_analysis    (QC findings + AI findings, kept
                                   separate via the `source` column)
    candidate_extraction_attempts (record.attempts[] + skipped list)

The complete record is ALSO stored verbatim in
candidates.pipeline_record (jsonb), so the projection is fully
rebuildable and nothing the pipeline produced is ever lost.

Configuration (environment, consistent with the project's .env
convention):
    SATVIQ_DATABASE_URL   e.g. postgresql://user:pass@host:5432/db
    SATVIQ_DB_ENABLED     'false' disables mirroring entirely.

Design: mirroring is best-effort. A database outage must never break
resume processing — sync failures are logged with the exception type
and the JSON store remains the operational record until the mirror
catches up (re-running the import or the next save re-syncs).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except Exception:  # pragma: no cover - dependency missing
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    Jsonb = None  # type: ignore[assignment]


# ----------------------------------------------------------------------
# Configuration / connection
# ----------------------------------------------------------------------

def database_url(env: dict[str, str] | None = None) -> str:
    env = env if env is not None else os.environ
    return (env.get("SATVIQ_DATABASE_URL") or "").strip()


def db_enabled(env: dict[str, str] | None = None) -> bool:
    env = env if env is not None else os.environ
    if (env.get("SATVIQ_DB_ENABLED") or "").strip().lower() == "false":
        return False
    return bool(database_url(env))


def connect(env: dict[str, str] | None = None):
    """Open a psycopg connection (raises on failure — callers in the
    sync path catch; the CLI surfaces the error)."""
    if psycopg is None:
        raise RuntimeError(
            "psycopg is not installed — add 'psycopg[binary]' to "
            "requirements and install it to use PostgreSQL mirroring.")
    url = database_url(env)
    if not url:
        raise RuntimeError(
            "SATVIQ_DATABASE_URL is not set — PostgreSQL mirroring "
            "has nowhere to connect.")
    return psycopg.connect(url, row_factory=dict_row)


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------

def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def _str_list(value: Any) -> list[str] | None:
    if not value:
        return None
    if isinstance(value, str):
        return [value] if value.strip() else None
    items = [str(v).strip() for v in value if v is not None and str(v).strip()]
    return items or None


def _split_name(full_name: str | None) -> tuple[str | None, str | None]:
    if not full_name:
        return None, None
    parts = str(full_name).strip().split()
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


_URL_KINDS = (
    ("linkedin", "linkedin_url"),
    ("github", "github_url"),
    ("portfolio", "portfolio_url"),
    ("duckdns", "portfolio_url"),
    ("github.io", "portfolio_url"),
)


def _classify_links(links: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"other_links": []}
    for link in _str_list(links) or []:
        low = link.lower()
        placed = False
        for needle, column in _URL_KINDS:
            if needle in low and column not in out:
                out[column] = link
                placed = True
                break
        if not placed:
            out["other_links"].append(link)
    if out["other_links"]:
        # jsonb column — must be a Jsonb wrapper, never a raw list:
        # psycopg adapts a plain dict as a record, not as JSON.
        out["other_links"] = Jsonb(out["other_links"])
    else:
        out.pop("other_links")
    return out


_MONTHS = {name: i + 1 for i, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}


def _parse_month_year(text: Any) -> tuple[int, int] | None:
    if not text:
        return None
    t = str(text).lower()
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
                  r"[a-z]*\s+(\d{4})", t)
    if m:
        return int(m.group(2)), _MONTHS[m.group(1)]
    m = re.search(r"(\d{4})", t)
    if m:
        return int(m.group(1)), 1
    return None


def _duration_months(start: Any, end: Any, is_current: bool | None) -> float | None:
    s = _parse_month_year(start)
    if not s:
        return None
    if is_current or (end and "present" in str(end).lower()):
        return None  # ongoing — duration depends on "today"; leave NULL
    e = _parse_month_year(end)
    if not e:
        return None
    months = (e[0] - s[0]) * 12 + (e[1] - s[1])
    return float(months) if months >= 0 else None


def _total_experience_years(experience: list[dict]) -> float | None:
    total = 0.0
    found = False
    for item in experience:
        months = _duration_months(item.get("start_date"),
                                  item.get("end_date"),
                                  item.get("is_current"))
        if months is not None:
            total += months
            found = True
    return round(total / 12.0, 2) if found else None


# ----------------------------------------------------------------------
# Projection: SATVIQ record -> relational rows
# ----------------------------------------------------------------------

def _quality_block(record: dict[str, Any]) -> dict[str, Any]:
    """The record-level Quality Checker report (selected attempt)."""
    quality = record.get("quality") or {}
    if isinstance(quality, dict) and quality:
        return quality
    # Fall back to the selected attempt's own quality payload.
    for attempt in record.get("attempts") or []:
        if attempt.get("is_final_selected") or attempt.get("selected"):
            return attempt.get("quality") or attempt.get(
                "quality_checker_report") or {}
    return {}


def _filter_score(block: dict[str, Any], name: str) -> float | None:
    sub = block.get(name) or {}
    return _num(sub.get("score")) if isinstance(sub, dict) else None


def _candidate_values(record: dict[str, Any]) -> dict[str, Any]:
    rj = record.get("resume_json") or record.get("resume_data") or {}
    if not isinstance(rj, dict):
        rj = {}
    skills = rj.get("skills") or {}
    if isinstance(skills, list):  # legacy flat form
        skills = {"technical_skills": skills}
    if not isinstance(skills, dict):
        skills = {}
    experience = [e for e in (rj.get("experience") or [])
                  if isinstance(e, dict)]
    current = next((e for e in experience if e.get("is_current")),
                   experience[0] if experience else {})
    first, last = _split_name(rj.get("full_name"))
    links = _classify_links(rj.get("links"))
    quality = _quality_block(record)
    analysis = record.get("analysis") or {}
    if not (isinstance(analysis, dict)
            and analysis.get("status") == "success"):
        analysis = {}
    ev = analysis.get("evaluation") or {}
    values: dict[str, Any] = {
        "resume_id": record.get("resume_id"),
        "candidate_id": record.get("resume_id"),
        "email": _clean(rj.get("email")),
        "first_name": first,
        "last_name": last,
        "contact_no": _clean(rj.get("phone")),
        "curr_company": _clean(current.get("company")),
        "curr_position": _clean(current.get("title")),
        "curr_location": _clean(rj.get("location")),
        "skills": ", ".join(_str_list(
            skills.get("technical_skills")) or [])[:255] or None,
        "cv_text": record.get("extracted_text")
        or record.get("final_extracted_text"),
        "status": _clean(record.get("quality_status"))
        or _clean(record.get("final_quality_status")),
        "headline": _clean((ev.get("candidate") or {}).get("headline"))
        or _clean(current.get("title")),
        "summary": _clean(rj.get("summary")),
        "total_experience_years": _total_experience_years(experience),
        "exp": _total_experience_years(experience),
        "technical_skills": _str_list(skills.get("technical_skills")),
        "soft_skills": _str_list(skills.get("soft_skills")),
        "tools": _str_list(skills.get("tools")),
        "taxonomy_skills": _str_list(skills.get("taxonomy_skills")),
        "other_skills": _str_list(skills.get("other_skills")),
        "certifications": _str_list(rj.get("certifications")),
        "languages": _str_list(rj.get("languages")),
        "extraction_engine": _clean(record.get("selected_extractor"))
        or _clean(record.get("extractor_used"))
        or _clean(record.get("final_extractor")),
        "extraction_status": _clean(record.get("llm_status"))
        or ("success" if record.get("success") else None),
        "extraction_duration_ms": _int(record.get("duration_ms")),
        "resume_json": Jsonb(rj) if rj else None,
        "pipeline_record": Jsonb(record),
        "metadata": Jsonb({
            "filename": record.get("filename"),
            "file_type": record.get("file_type"),
            "page_count": record.get("page_count"),
            "pipeline_mode": record.get("pipeline_mode"),
            "selection_reason": record.get("selection_reason"),
            "uploaded_at": record.get("uploaded_at"),
        }),
    }
    values.update({k: v for k, v in links.items()})
    # QC snapshot (rule-based — separate from AI scores, always).
    if quality:
        values["qc_status"] = _clean(quality.get("status"))
        values["qc_text_health_score"] = _filter_score(quality, "text_health")
        values["qc_completeness_score"] = _filter_score(quality, "completeness")
        values["qc_reading_order_score"] = _filter_score(quality, "reading_order")
    # AI snapshot (latest successful evaluation only).
    if ev:
        comp = ev.get("completeness") or {}
        qual = ev.get("quality") or {}
        roles = ev.get("role_suitability") or []
        top = max(roles, key=lambda r: _num(r.get("score")) or -1,
                  default=None)
        values["career_stage"] = _clean(ev.get("career_stage"))
        values["professional_domain"] = _clean(ev.get("professional_domain"))
        values["ai_completeness_score"] = _num(comp.get("score"))
        values["ai_quality_score"] = _num(qual.get("score"))
        values["ai_suitable_roles"] = _str_list(ev.get("suitable_roles"))
        if top:
            values["ai_top_role"] = _clean(top.get("role"))
            values["ai_top_role_score"] = _num(top.get("score"))
        values["ai_evaluated_at"] = analysis.get("created_at")
    return values


_CANDIDATE_COLUMNS = [
    "resume_id", "candidate_id", "email", "first_name", "last_name",
    "contact_no", "curr_company", "curr_position", "curr_location",
    "skills", "cv_text", "status", "headline", "summary",
    "total_experience_years", "exp", "technical_skills", "soft_skills",
    "tools", "taxonomy_skills", "other_skills", "certifications",
    "languages", "linkedin_url", "github_url", "portfolio_url",
    "other_links", "qc_status", "qc_text_health_score",
    "qc_completeness_score", "qc_reading_order_score", "career_stage",
    "professional_domain", "ai_completeness_score", "ai_quality_score",
    "ai_suitable_roles", "ai_top_role", "ai_top_role_score",
    "ai_evaluated_at", "extraction_engine", "extraction_status",
    "extraction_duration_ms", "resume_json", "pipeline_record",
    "metadata",
]


def _delete_children(cur, candidate_pk: int) -> None:
    for table in ("candidate_role_suitability",
                  "candidate_section_analysis",
                  "candidate_evaluations",
                  "candidate_extraction_attempts",
                  "candidate_experiences",
                  "candidate_education",
                  "candidate_projects"):
        cur.execute(f"DELETE FROM {table} WHERE candidate_id = %s",
                    (candidate_pk,))


def _insert_experiences(cur, pk, record):
    rj = record.get("resume_json") or record.get("resume_data") or {}
    for item in rj.get("experience") or []:
        if not isinstance(item, dict):
            continue
        is_current = item.get("is_current")
        if isinstance(is_current, str):
            is_current = is_current.strip().lower() in ("true", "yes", "1")
        cur.execute(
            "INSERT INTO candidate_experiences (candidate_id, title,"
            " company, location, start_date, end_date, duration_months,"
            " is_current, highlights, raw_dates) VALUES (%s,%s,%s,%s,"
            "%s,%s,%s,%s,%s,%s)",
            (pk, _clean(item.get("title")), _clean(item.get("company")),
             _clean(item.get("location")), _clean(item.get("start_date")),
             _clean(item.get("end_date")),
             _duration_months(item.get("start_date"),
                              item.get("end_date"), is_current),
             bool(is_current) if is_current is not None else None,
             _str_list(item.get("highlights")),
             Jsonb({"start_date": item.get("start_date"),
                    "end_date": item.get("end_date")})))


def _insert_education(cur, pk, record):
    rj = record.get("resume_json") or record.get("resume_data") or {}
    for item in rj.get("education") or []:
        if not isinstance(item, dict):
            continue
        score_text = _clean(item.get("score")) or _clean(item.get("grade"))
        cur.execute(
            "INSERT INTO candidate_education (candidate_id, degree,"
            " institution, location, start_date, end_date,"
            " field_of_study, score_text, score_numeric) VALUES"
            " (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (pk, _clean(item.get("degree")),
             _clean(item.get("institution")), _clean(item.get("location")),
             _clean(item.get("start_date")), _clean(item.get("end_date")),
             _clean(item.get("field_of_study")), score_text,
             _num(item.get("score_numeric"))))


def _insert_projects(cur, pk, record):
    rj = record.get("resume_json") or record.get("resume_data") or {}
    projects = []
    raw = rj.get("projects")
    if isinstance(raw, list):
        projects = [p for p in raw if isinstance(p, dict)]
    if not projects:
        # Derive from experience highlights groups if present as
        # structured entries; otherwise projects stay empty — the
        # full payload remains in candidates.pipeline_record.
        projects = []
    for item in projects:
        cur.execute(
            "INSERT INTO candidate_projects (candidate_id, name,"
            " description, tech_stack, links, evidence) VALUES"
            " (%s,%s,%s,%s,%s,%s)",
            (pk, _clean(item.get("name") or item.get("title")),
             _clean(item.get("description")),
             _str_list(item.get("tech_stack") or item.get("technologies")),
             _str_list(item.get("links")), _str_list(item.get("evidence"))))


def _ocr_used(attempt: dict[str, Any]) -> bool | None:
    ocr = attempt.get("ocr") or {}
    if isinstance(ocr, dict) and ocr.get("ocr_used") is not None:
        return bool(ocr["ocr_used"])
    meta = attempt.get("metadata") or {}
    if isinstance(meta, dict) and meta.get("ocr_used") is not None:
        return bool(meta["ocr_used"])
    return None  # unknown — the schema stores NULL, by design


def _insert_attempts(cur, pk, record):
    skipped = {str(s).lower() for s in
               (record.get("skipped_extractors") or [])}
    seen: set[str] = set()
    order = 0
    for attempt in record.get("attempts") or []:
        name = _clean(attempt.get("extractor_name"))
        if not name or name.lower() == "muse_ai":
            continue  # the LLM stage is not an extraction attempt
        order += 1
        seen.add(name.lower())
        quality = attempt.get("quality") or attempt.get(
            "quality_checker_report") or {}
        text = attempt.get("extracted_text") or attempt.get("text") or ""
        cur.execute(
            "INSERT INTO candidate_extraction_attempts (candidate_id,"
            " extractor, attempt_order, accepted, selected, skipped,"
            " quality_status, text_health_score, completeness_score,"
            " reading_order_score, char_count, duration_ms, ocr_used,"
            " error) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (pk, name, order, bool(attempt.get("accepted")),
             bool(attempt.get("is_final_selected")
                  or attempt.get("selected")), False,
             _clean(attempt.get("quality_status"))
             or _clean(quality.get("status")),
             _filter_score(quality, "text_health"),
             _filter_score(quality, "completeness"),
             _filter_score(quality, "reading_order"),
             len(text), _int(attempt.get("duration_ms")),
             _ocr_used(attempt),
             _clean(attempt.get("error_message"))
             or _clean(attempt.get("error"))))
    for name in sorted(skipped - seen):
        order += 1
        cur.execute(
            "INSERT INTO candidate_extraction_attempts (candidate_id,"
            " extractor, attempt_order, accepted, selected, skipped)"
            " VALUES (%s,%s,%s,false,false,true)", (pk, name, order))


def _insert_evaluations(cur, pk, record):
    for entry in record.get("analyses") or []:
        if not isinstance(entry, dict):
            continue
        ev = entry.get("evaluation") or {}
        comp, qual = ev.get("completeness") or {}, ev.get("quality") or {}
        tokens = entry.get("token_usage") or {}
        cur.execute(
            "INSERT INTO candidate_evaluations (candidate_id,"
            " analysis_version, model, yaml_version, config_version,"
            " status, error_category, error, completeness_score,"
            " completeness_classification, quality_score,"
            " quality_classification, career_stage, professional_domain,"
            " strengths, weaknesses, recommendations, evaluation,"
            " input_tokens, output_tokens, reasoning_tokens,"
            " total_tokens, duration_ms) VALUES (%s,%s,%s,%s,%s,%s,%s,"
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " RETURNING id",
            (pk, _int(entry.get("analysis_version")),
             _clean(entry.get("model")), _clean(entry.get("yaml_version")),
             _clean(entry.get("config_version")),
             _clean(entry.get("status")),
             _clean(entry.get("error_category")),
             _clean(entry.get("error")),
             _num(comp.get("score")), _clean(comp.get("classification")),
             _num(qual.get("score")), _clean(qual.get("classification")),
             _clean(ev.get("career_stage")),
             _clean(ev.get("professional_domain")),
             _str_list(ev.get("strengths")), _str_list(ev.get("weaknesses")),
             _str_list(ev.get("recommendations")),
             Jsonb(entry), _int(tokens.get("input_tokens")),
             _int(tokens.get("output_tokens")),
             _int(tokens.get("reasoning_tokens")),
             _int(tokens.get("total_tokens")),
             _int(entry.get("duration_ms"))))
        evaluation_pk = cur.fetchone()["id"]
        if entry.get("status") != "success":
            continue
        for role in ev.get("role_suitability") or []:
            if not isinstance(role, dict):
                continue
            cur.execute(
                "INSERT INTO candidate_role_suitability (candidate_id,"
                " evaluation_id, role, score, matching_skills,"
                " missing_skills, relevant_projects,"
                " relevant_experience, evidence, explanation) VALUES"
                " (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT"
                " (evaluation_id, role) DO NOTHING",
                (pk, evaluation_pk, _clean(role.get("role")),
                 _num(role.get("score")),
                 _str_list(role.get("matching_skills")),
                 _str_list(role.get("missing_skills")),
                 _str_list(role.get("relevant_projects")),
                 _str_list(role.get("relevant_experience")),
                 _str_list(role.get("evidence")),
                 _clean(role.get("explanation"))))
        _insert_ai_sections(cur, pk, evaluation_pk, ev)


def _insert_ai_sections(cur, pk, evaluation_pk, ev):
    for section in ev.get("matching_sections") or []:
        if not isinstance(section, dict):
            continue
        finding = (_clean(section.get("completeness_status"))
                   or _clean(section.get("presence_status")) or "present")
        cur.execute(
            "INSERT INTO candidate_section_analysis (candidate_id,"
            " evaluation_id, source, section, finding, evidence,"
            " payload) VALUES (%s,%s,'ai',%s,%s,%s,%s)",
            (pk, evaluation_pk, _clean(section.get("section")), finding,
             _str_list(section.get("evidence")), Jsonb(section)))
    for section in ev.get("missing_sections") or []:
        if not isinstance(section, dict):
            continue
        cur.execute(
            "INSERT INTO candidate_section_analysis (candidate_id,"
            " evaluation_id, source, section, finding, importance,"
            " reason, impact, recommendation, evidence, payload)"
            " VALUES (%s,%s,'ai',%s,%s,%s,%s,%s,%s,%s,%s)",
            (pk, evaluation_pk, _clean(section.get("section")),
             _clean(section.get("status")) or "missing",
             _clean(section.get("importance")),
             _clean(section.get("reason")), _clean(section.get("impact")),
             _clean(section.get("recommendation")),
             _str_list(section.get("evidence")), Jsonb(section)))


def _insert_qc_sections(cur, pk, record):
    quality = _quality_block(record)
    completeness = quality.get("completeness") or {}
    if not isinstance(completeness, dict):
        return
    metrics = completeness.get("metrics") or {}
    section_status = metrics.get("section_status") or {}
    evidence_map = metrics.get("section_evidence") or {}
    for section, finding in section_status.items():
        evidence = evidence_map.get(section) or {}
        cur.execute(
            "INSERT INTO candidate_section_analysis (candidate_id,"
            " evaluation_id, source, section, finding, evidence,"
            " payload) VALUES (%s,NULL,'qc',%s,%s,%s,%s)",
            (pk, section,
             "detected" if finding == "detected" else "not_detected",
             [evidence["evidence"]] if evidence.get("evidence") else None,
             Jsonb(evidence) if evidence else None))


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

def sync_record(record: dict[str, Any], conn=None) -> int | None:
    """Upsert one SATVIQ record into PostgreSQL. Returns the
    candidates.id primary key, or None when mirroring is disabled.
    Raises on database errors (the safe wrapper below catches)."""
    if not db_enabled() and conn is None:
        return None
    own = conn is None
    if own:
        conn = connect()
    try:
        values = _candidate_values(record)
        params = [values.get(c) for c in _CANDIDATE_COLUMNS]
        updates = ", ".join(
            f"{c} = EXCLUDED.{c}" for c in _CANDIDATE_COLUMNS
            if c != "resume_id")
        cols = ", ".join(_CANDIDATE_COLUMNS)
        marks = ", ".join(["%s"] * len(_CANDIDATE_COLUMNS))
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO candidates ({cols}, updatedat) VALUES"
                f" ({marks}, now()) ON CONFLICT (resume_id) DO UPDATE"
                f" SET {updates}, updatedat = now() RETURNING id",
                params)
            pk = cur.fetchone()["id"]
            _delete_children(cur, pk)
            _insert_experiences(cur, pk, record)
            _insert_education(cur, pk, record)
            _insert_projects(cur, pk, record)
            _insert_evaluations(cur, pk, record)
            _insert_qc_sections(cur, pk, record)
            _insert_attempts(cur, pk, record)
        conn.commit()
        return pk
    except Exception:
        conn.rollback()
        raise
    finally:
        if own:
            conn.close()


def sync_record_safe(record: dict[str, Any]) -> int | None:
    """Best-effort mirror used by the live save path: a database
    problem is logged, never raised into resume processing."""
    if not db_enabled():
        return None
    try:
        return sync_record(record)
    except Exception as exc:  # noqa: BLE001 - boundary guard
        logger.warning("PostgreSQL mirror failed for resume %s: %s",
                       record.get("resume_id"), type(exc).__name__)
        return None


def delete_candidate(resume_id: str) -> bool:
    if not db_enabled():
        return False
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM candidates WHERE resume_id = %s",
                        (resume_id,))
            return cur.rowcount > 0


def import_all(results_dir) -> dict[str, Any]:
    """Bulk-import every stored JSON record (used by the CLI and the
    initial backfill). Each record syncs independently; failures are
    reported per record, never hidden."""
    from pathlib import Path
    imported, failed = 0, []
    for path in sorted(Path(results_dir).glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            sync_record(record)
            imported += 1
        except Exception as exc:  # noqa: BLE001 - report per record
            failed.append({"file": path.name,
                           "error": f"{type(exc).__name__}: {exc}"})
    return {"imported": imported, "failed": failed}


# ----------------------------------------------------------------------
# Primary-store read layer (PostgreSQL is the system of record)
# ----------------------------------------------------------------------

_ENV_LOADED = False


def _ensure_env_loaded() -> None:
    """Load SATVIQ_DATABASE_URL from the project .env on first use
    (the loader never overrides variables already set)."""
    global _ENV_LOADED
    if _ENV_LOADED or os.environ.get("SATVIQ_DATABASE_URL"):
        _ENV_LOADED = True
        return
    _ENV_LOADED = True
    try:
        from muse_ai.config import load_env_file
        load_env_file()
    except Exception:  # noqa: BLE001 - env loading is best-effort
        pass


def check_connection() -> dict[str, Any]:
    """Startup health check for the PostgreSQL primary store.

    Returns {"ok": True, "detail": "..."} when the database is
    reachable AND the V2 schema is present; otherwise ok=False with a
    plain-language detail. Never raises — the caller decides whether
    a down database is fatal (the SATVIQ store does: DB-primary mode
    must fail loudly rather than silently fall back to JSON files).
    """
    _ensure_env_loaded()
    if not db_enabled():
        return {"ok": False,
                "detail": "SATVIQ_DATABASE_URL is not set in .env — "
                          "PostgreSQL is now the primary store, so "
                          "the URL is required."}
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS n FROM information_schema.tables"
                    " WHERE table_schema = 'public' AND table_name IN"
                    " ('candidates','candidate_experiences',"
                    "'candidate_education','candidate_projects',"
                    "'candidate_evaluations',"
                    "'candidate_role_suitability',"
                    "'candidate_section_analysis',"
                    "'candidate_extraction_attempts')")
                tables = cur.fetchone()["n"]
                if tables < 8:
                    return {"ok": False,
                            "detail": "Connected, but the V2 schema is"
                                      f" incomplete ({tables}/8 tables)"
                                      " — run: python scripts/"
                                      "import_to_postgres.py"
                                      " --create-schema"}
                cur.execute("SELECT count(*) AS n FROM candidates")
                count = cur.fetchone()["n"]
        return {"ok": True,
                "detail": f"PostgreSQL primary store connected; "
                          f"{count} candidate record(s) present."}
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return {"ok": False,
                "detail": f"PostgreSQL connection failed "
                          f"({type(exc).__name__}) — is the PostgreSQL"
                          " service running, and does the password in"
                          " SATVIQ_DATABASE_URL match the postgres"
                          " user?"}


def fetch_record(resume_id: str) -> dict[str, Any] | None:
    """The full stored record for one resume, exactly as saved."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pipeline_record FROM candidates"
                " WHERE resume_id = %s", (resume_id,))
            row = cur.fetchone()
    return row["pipeline_record"] if row else None


def fetch_all_records() -> list[dict[str, Any]]:
    """Every stored record, newest first (same ordering the file
    store produced: uploaded_at descending)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pipeline_record FROM candidates"
                " ORDER BY COALESCE(pipeline_record->>'uploaded_at',"
                " '') DESC")
            return [row["pipeline_record"] for row in cur.fetchall()]


def delete_candidate_by_resume(resume_id: str) -> bool:
    """Delete one candidate (children cascade)."""
    return delete_candidate(resume_id)
