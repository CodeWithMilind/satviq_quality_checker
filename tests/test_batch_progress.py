"""Batch progress tests: POST /api/resumes/process-async + polling
GET /api/resumes/jobs/{job_id}.

The Process page previously only knew upload progress; once the
bytes reached the server it sat on "processing on the server…"
with no count. The async job flow must report REAL counts as files
finish: processed, remaining, percent, the current file, per-file
statuses, and the results in upload order. An invalid file in the
batch counts as processed (failed) and never blocks the others.
"""

import os
import time

os.environ.setdefault("SATVIQ_TEST_DISK_STORE", "1")

import pytest

pymupdf = pytest.importorskip("pymupdf")
from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402

client = TestClient(app)


def _make_pdf_bytes(marker: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    lines = [
        "Jane Doe",
        "jane.doe@example.com | +91 98765 43210 | Pune, India",
        "Professional Summary",
        f"Python developer {marker} building AI and backend applications.",
        "Skills",
        "Python, FastAPI, REST APIs, SQL, Pandas, LangChain, Git.",
        "Work Experience",
        "Data Scientist Intern at Example AI Solutions, May 2025 to Nov 2025.",
        "Education",
        "Master of Computer Applications, Example University, 2025 to 2027.",
        "Projects",
        "PDF Chat Assistant using RAG, LangChain, FAISS and FastAPI.",
        "Certifications",
        "Certified Data Scientist, completed in 2025.",
    ]
    y = 60
    for line in lines:
        page.insert_text((50, y), line, fontsize=10)
        y += 18
    data = document.tobytes()
    document.close()
    return data


@pytest.fixture(autouse=True, scope="module")
def _clean_state():
    client.delete("/api/resumes")
    yield
    client.delete("/api/resumes")


def _start_job(files):
    response = client.post("/api/resumes/process-async", files=files)
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"]
    assert body["total"] == len(files)
    return body


def _poll_until_done(job_id: str, timeout_s: float = 120.0) -> dict:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        response = client.get(f"/api/resumes/jobs/{job_id}")
        assert response.status_code == 200
        last = response.json()
        # Counts must always be internally consistent, at every poll.
        assert last["processed"] + last["remaining"] == last["total"]
        assert last["processed"] == last["completed"]
        assert last["percent"] == (
            int(last["completed"] * 100 / last["total"]) if last["total"] else 100)
        assert len(last["files"]) == last["total"]
        if last["is_done"]:
            return last
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish: {last}")


def test_async_batch_reports_processed_and_remaining():
    job = _start_job([
        ("files", ("prog_a.pdf", _make_pdf_bytes("alpha"), "application/pdf")),
        ("files", ("prog_b.pdf", _make_pdf_bytes("beta"), "application/pdf")),
    ])
    snap = _poll_until_done(job["job_id"])
    assert snap["status"] == "done"
    assert snap["processed"] == 2
    assert snap["remaining"] == 0
    assert snap["percent"] == 100
    assert snap["current_filename"] is None
    assert [f["status"] for f in snap["files"]] == ["done", "done"]
    assert [f["filename"] for f in snap["files"]] == ["prog_a.pdf", "prog_b.pdf"]
    results = snap["results"]
    assert len(results) == 2
    assert all(r["success"] for r in results)
    # Results land in History too — the job really processed them.
    listing = client.get("/api/resumes").json()["resumes"]
    ids = {r["resume_id"] for r in listing}
    assert {r["resume_id"] for r in results} <= ids


def test_async_batch_invalid_file_counts_as_failed_not_stuck():
    job = _start_job([
        ("files", ("prog_good.pdf", _make_pdf_bytes("gamma"), "application/pdf")),
        ("files", ("prog_bad.exe", b"MZ fake", "application/octet-stream")),
    ])
    snap = _poll_until_done(job["job_id"])
    assert snap["processed"] == 2
    assert snap["remaining"] == 0
    assert snap["succeeded"] == 1
    assert snap["failed"] == 1
    by_name = {f["filename"]: f for f in snap["files"]}
    assert by_name["prog_good.pdf"]["status"] == "done"
    assert by_name["prog_bad.exe"]["status"] == "failed"
    bad = [r for r in snap["results"] if r["filename"] == "prog_bad.exe"]
    assert bad and bad[0]["success"] is False
    assert "Unsupported file type" in bad[0]["error"]


def test_unknown_job_is_404():
    response = client.get("/api/resumes/jobs/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_jobs_unit_counts():
    from api import jobs

    job = jobs.create_job(["one.pdf", "two.pdf", "three.pdf"])
    snap = jobs.snapshot(job["job_id"])
    assert (snap["processed"], snap["remaining"], snap["percent"]) == (0, 3, 0)
    jobs.start_file(job["job_id"], 0)
    snap = jobs.snapshot(job["job_id"])
    assert snap["status"] == "running"
    assert snap["current_index"] == 1
    assert snap["current_filename"] == "one.pdf"
    assert snap["files"][0]["status"] == "processing"
    jobs.finish_file(job["job_id"], 0,
                     {"resume_id": "r1", "filename": "one.pdf", "success": True})
    snap = jobs.snapshot(job["job_id"])
    assert (snap["processed"], snap["remaining"], snap["percent"]) == (1, 2, 33)
    assert snap["is_done"] is False
    jobs.finish_file(job["job_id"], 1,
                     {"resume_id": "r2", "filename": "two.pdf", "success": False})
    jobs.finish_file(job["job_id"], 2,
                     {"resume_id": "r3", "filename": "three.pdf", "success": True})
    snap = jobs.snapshot(job["job_id"])
    assert snap["is_done"] is True
    assert (snap["succeeded"], snap["failed"]) == (2, 1)
    assert [r["resume_id"] for r in snap["results"]] == ["r1", "r2", "r3"]
    assert jobs.snapshot("no-such-job") is None
