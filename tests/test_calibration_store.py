"""Calibration Phase 1: live-PostgreSQL store tests (migration 006).

Runs only when SATVIQ_TEST_DATABASE_URL points at a scratch
database. Applies the migration twice (the --migrate runner
re-executes every file, so idempotency is part of the contract),
then exercises dataset / resume / annotation / split persistence.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("SATVIQ_TEST_DATABASE_URL"),
    reason="SATVIQ_TEST_DATABASE_URL not set")

MIGRATION = (Path(__file__).resolve().parent.parent
             / "database" / "migrations" / "006_calibration.sql")


@pytest.fixture()
def store():
    os.environ["SATVIQ_DATABASE_URL"] = \
        os.environ["SATVIQ_TEST_DATABASE_URL"]
    from api import db
    sql = MIGRATION.read_text(encoding="utf-8")
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(sql)  # second pass must be a no-op success
        conn.commit()
    from calibration import store as calibration_store
    return calibration_store


@pytest.fixture()
def dataset(store):
    row = store.create_dataset("phase1-test-dataset",
                               description="gated test", source="pytest")
    yield row
    from api import db
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM calibration_datasets WHERE id = %s",
                        (row["id"],))
        conn.commit()


def test_dataset_roundtrip(store, dataset):
    assert store.get_dataset(dataset["id"])["name"] == \
        "phase1-test-dataset"
    assert any(d["id"] == dataset["id"] for d in store.list_datasets())


def test_resume_hash_dedupe(store, dataset):
    row, created = store.add_resume(dataset["id"], "a.pdf", "hash-1",
                                    file_type="pdf")
    assert created
    again, created_again = store.add_resume(
        dataset["id"], "a-copy.pdf", "hash-1", file_type="pdf")
    assert not created_again and again["id"] == row["id"]
    assert len(store.list_resumes(dataset["id"])) == 1


def test_annotation_versions_append_only(store, dataset):
    resume, _ = store.add_resume(dataset["id"], "a.pdf", "hash-1")
    first = store.save_annotation(
        resume["id"], {"career_stage": "Student"}, "in_progress",
        annotated_by="one")
    second = store.save_annotation(
        resume["id"], {"career_stage": "Student"}, "completed",
        annotated_by="two")
    assert (first["version"], second["version"]) == (1, 2)
    assert store.latest_annotation(resume["id"])["id"] == second["id"]
    assert len(store.annotation_history(resume["id"])) == 2


def test_annotation_status_validated(store, dataset):
    resume, _ = store.add_resume(dataset["id"], "a.pdf", "hash-1")
    with pytest.raises(ValueError):
        store.save_annotation(resume["id"], {}, "bogus-status")


def test_split_roundtrip(store, dataset):
    first, _ = store.add_resume(dataset["id"], "a.pdf", "hash-1")
    second, _ = store.add_resume(dataset["id"], "b.pdf", "hash-2")
    assignment = {str(first["id"]): "development",
                  str(second["id"]): "holdout"}
    assert store.save_split(dataset["id"], assignment, seed=42) == 2
    assert store.get_split(dataset["id"]) == assignment


def test_counts(store, dataset):
    resume, _ = store.add_resume(dataset["id"], "a.pdf", "hash-1")
    store.save_annotation(resume["id"], {}, "completed")
    counts = store.counts()
    assert counts["datasets"] >= 1 and counts["resumes"] >= 1
    assert counts["annotations_completed"] >= 1
