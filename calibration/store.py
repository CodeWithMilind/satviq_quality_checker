"""PostgreSQL persistence for the Calibration System.

Uses the same connection machinery as the production mirror
(``api.db.connect`` — psycopg 3, dict rows, SATVIQ_DATABASE_URL) and
the migration-006 ``calibration_*`` tables. Calibration owns these
tables outright; no production code reads or writes them.

Every function raises on database failure — callers (the API layer)
decide how to surface it. Annotation saves are append-only: each
save inserts a new version row, so reviewer history is never
overwritten (spec Part D).
"""

from __future__ import annotations

import uuid
from typing import Any

from psycopg.types.json import Jsonb

from api import db

from .schemas import ANNOTATION_STATUSES


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------
# Datasets and resumes
# ---------------------------------------------------------------

def create_dataset(name: str, description: str = "", source: str = "",
                   metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    dataset_id = _new_id()
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO calibration_datasets"
                " (id, name, description, source, metadata)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING *",
                (dataset_id, name, description, source,
                 Jsonb(metadata or {})))
            row = cur.fetchone()
        conn.commit()
    return row


def get_dataset(dataset_id: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM calibration_datasets WHERE id = %s",
                        (dataset_id,))
            return cur.fetchone()


def list_datasets() -> list[dict[str, Any]]:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM calibration_datasets"
                        " ORDER BY created_at, name")
            return list(cur.fetchall())


def add_resume(dataset_id: str, original_filename: str, file_hash: str,
               file_type: str = "", stored_path: str = "",
               source_record_id: str | None = None,
               ) -> tuple[dict[str, Any], bool]:
    """Add a resume to a dataset. Content-hash duplicates return the
    existing row with ``created=False`` — never a second copy."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO calibration_resumes"
                " (id, dataset_id, original_filename, stored_path,"
                "  file_hash, file_type, source_record_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (dataset_id, file_hash) DO NOTHING"
                " RETURNING *",
                (_new_id(), dataset_id, original_filename, stored_path,
                 file_hash, file_type, source_record_id))
            row = cur.fetchone()
            if row is not None:
                conn.commit()
                return row, True
            cur.execute(
                "SELECT * FROM calibration_resumes"
                " WHERE dataset_id = %s AND file_hash = %s",
                (dataset_id, file_hash))
            return cur.fetchone(), False


def list_resumes(dataset_id: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM calibration_resumes WHERE dataset_id = %s"
                " ORDER BY created_at, original_filename", (dataset_id,))
            return list(cur.fetchall())


# ---------------------------------------------------------------
# Annotations (versioned, append-only)
# ---------------------------------------------------------------

def save_annotation(calibration_resume_id: str,
                    annotation: dict[str, Any], status: str,
                    annotated_by: str = "", notes: str = "",
                    ) -> dict[str, Any]:
    """Append a new annotation version for a resume and return it.
    Prior versions are kept — reviewer history is never lost."""
    if status not in ANNOTATION_STATUSES:
        raise ValueError(f"unknown annotation status {status!r}")
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS next_version"
                " FROM calibration_annotations"
                " WHERE calibration_resume_id = %s",
                (calibration_resume_id,))
            version = cur.fetchone()["next_version"]
            cur.execute(
                "INSERT INTO calibration_annotations"
                " (id, calibration_resume_id, version, annotation,"
                "  status, annotated_by, notes)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (_new_id(), calibration_resume_id, version,
                 Jsonb(annotation), status, annotated_by, notes))
            row = cur.fetchone()
        conn.commit()
    return row


def latest_annotation(calibration_resume_id: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM calibration_annotations"
                " WHERE calibration_resume_id = %s"
                " ORDER BY version DESC LIMIT 1",
                (calibration_resume_id,))
            return cur.fetchone()


def annotation_history(calibration_resume_id: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM calibration_annotations"
                " WHERE calibration_resume_id = %s ORDER BY version",
                (calibration_resume_id,))
            return list(cur.fetchall())


# ---------------------------------------------------------------
# Splits
# ---------------------------------------------------------------

def save_split(dataset_id: str, assignment: dict[str, str], seed: int,
               split_config: dict[str, Any] | None = None) -> int:
    """Replace a dataset's split with a new assignment, atomically.
    Returns the number of resumes assigned."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM calibration_splits"
                        " WHERE dataset_id = %s", (dataset_id,))
            for resume_id, split_name in assignment.items():
                cur.execute(
                    "INSERT INTO calibration_splits"
                    " (id, dataset_id, calibration_resume_id, split_name,"
                    "  seed, split_config)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (_new_id(), dataset_id, resume_id, split_name, seed,
                     Jsonb(split_config or {})))
        conn.commit()
    return len(assignment)


def get_split(dataset_id: str) -> dict[str, str]:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT calibration_resume_id, split_name"
                " FROM calibration_splits WHERE dataset_id = %s",
                (dataset_id,))
            return {str(row["calibration_resume_id"]): row["split_name"]
                    for row in cur.fetchall()}


# ---------------------------------------------------------------
# Overview counts (Calibration dashboard KPIs, later phases)
# ---------------------------------------------------------------

def counts() -> dict[str, int]:
    with db.connect() as conn:
        with conn.cursor() as cur:
            out: dict[str, int] = {}
            for key, table in (
                    ("datasets", "calibration_datasets"),
                    ("resumes", "calibration_resumes"),
                    ("runs", "calibration_runs"),
                    ("results", "calibration_results"),
                    ("errors", "calibration_errors")):
                cur.execute(f"SELECT count(*) AS n FROM {table}")
                out[key] = int(cur.fetchone()["n"])
            cur.execute(
                "SELECT count(*) AS n FROM calibration_annotations a"
                " WHERE a.status = 'completed' AND a.version = ("
                "   SELECT MAX(version) FROM calibration_annotations"
                "   WHERE calibration_resume_id = a.calibration_resume_id)")
            out["annotations_completed"] = int(cur.fetchone()["n"])
            return out
