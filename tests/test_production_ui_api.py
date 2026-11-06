"""Tests for the production-UI backend additions:

- 5 MB upload limit (backend-enforced, exact user-facing message).
- History search / status filter / server-side pagination.
- Original-file download endpoint.
- System-prompt settings: default, save, reset, validation, and the
  guarantee that a saved prompt is what the Muse service sends.

Run from the project root:  python -m pytest tests/test_production_ui_api.py -v
"""

from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")
from fastapi.testclient import TestClient  # noqa: E402

from api import pipeline  # noqa: E402
from api.main import app  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True, scope="module")
def _clean_state():
    client.delete("/api/resumes")
    yield
    client.delete("/api/resumes")


@pytest.fixture()
def prompt_file(tmp_path, monkeypatch):
    path = tmp_path / "prompt_settings.json"
    monkeypatch.setenv("SATVIQ_PROMPT_SETTINGS_FILE", str(path))
    yield path
    from muse_ai.prompt_config import reset_prompt

    reset_prompt()


def _make_pdf_bytes() -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    y = 60
    for line in [
        "Jane Doe",
        "jane.doe@example.com | +91 98765 43210 | Pune, India",
        "Professional Summary",
        "Python developer building practical AI and backend applications.",
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
    ]:
        page.insert_text((50, y), line, fontsize=10)
        y += 18
    data = document.tobytes()
    document.close()
    return data


# ---------------------------------------------------------------- 5 MB

def test_upload_limit_is_five_mb():
    assert pipeline.MAX_UPLOAD_MB == 5
    health = client.get("/api/health").json()
    assert health["max_upload_mb"] == 5


def test_oversized_file_rejected_with_exact_message():
    data = b"%PDF-" + b"x" * (pipeline.MAX_UPLOAD_BYTES + 1)
    response = client.post(
        "/api/resumes/process",
        files=[("files", ("big_resume.pdf", data, "application/pdf"))])
    assert response.status_code == 200
    (record,) = response.json()["results"]
    assert record["success"] is False
    assert record["error"] == (
        "File exceeds the 5 MB limit. Please upload a smaller resume.")
    # The rejected file must not enter the extraction pipeline.
    assert record["attempts"] == []


# ------------------------------------------------------- history paging

def test_history_search_filter_and_pagination():
    client.post("/api/resumes/process", files=[(
        "files", ("alpha_resume.pdf", _make_pdf_bytes(), "application/pdf"))])
    client.post("/api/resumes/process", files=[(
        "files", ("beta_resume.pdf", _make_pdf_bytes(), "application/pdf"))])

    body = client.get("/api/resumes").json()
    assert body["total"] >= 2
    assert len(body["resumes"]) == body["total"]  # default: all

    searched = client.get("/api/resumes", params={"q": "ALPHA"}).json()
    assert searched["total"] == 1
    assert searched["resumes"][0]["filename"] == "alpha_resume.pdf"

    page1 = client.get(
        "/api/resumes", params={"page": 1, "page_size": 1}).json()
    assert page1["page_size"] == 1 and len(page1["resumes"]) == 1
    assert page1["pages"] >= 2
    page2 = client.get(
        "/api/resumes", params={"page": 2, "page_size": 1}).json()
    assert page2["resumes"][0]["resume_id"] != page1["resumes"][0]["resume_id"]

    filtered = client.get(
        "/api/resumes", params={"status": "failed"}).json()
    # The oversized record above is a failed record.
    assert filtered["total"] >= 1
    assert all(not r["success"] for r in filtered["resumes"])


def test_download_original_file():
    response = client.post("/api/resumes/process", files=[(
        "files", ("download_me.pdf", _make_pdf_bytes(), "application/pdf"))])
    (record,) = response.json()["results"]
    downloaded = client.get(f"/api/resumes/{record['resume_id']}/file")
    assert downloaded.status_code == 200
    assert downloaded.content.startswith(b"%PDF")
    missing = client.get("/api/resumes/00000000-0000-0000-0000-000000000000/file")
    assert missing.status_code == 404


# ------------------------------------------------------- system prompt

def test_system_prompt_default_save_reset(prompt_file):
    from muse_ai.prompt_config import default_prompt, get_effective_prompt

    body = client.get("/api/settings/system-prompt").json()
    assert body["is_custom"] is False
    assert body["system_prompt"] == default_prompt()
    assert body["ai"]["provider"] == "Muse AI"
    assert "api_key" not in body["ai"]

    saved = client.put("/api/settings/system-prompt",
                       json={"system_prompt": "Custom prompt for tests."})
    assert saved.status_code == 200
    assert saved.json()["is_custom"] is True
    assert get_effective_prompt() == "Custom prompt for tests."
    assert prompt_file.is_file()

    empty = client.put("/api/settings/system-prompt",
                       json={"system_prompt": "   "})
    assert empty.status_code == 400

    reset = client.post("/api/settings/system-prompt/reset")
    assert reset.json()["is_custom"] is False
    assert get_effective_prompt() == default_prompt()


def test_saved_prompt_is_what_the_service_sends(prompt_file, monkeypatch):
    from muse_ai.config import MuseConfig
    from muse_ai.service import MuseService

    client.put("/api/settings/system-prompt",
               json={"system_prompt": "SENTINEL CUSTOM PROMPT"})
    captured = {}

    class FakeClient:
        def chat_text(self, instructions, user_text):
            captured["instructions"] = instructions
            return {"text": "{}", "usage": {}, "request_id": None,
                    "duration_ms": 1.0, "model": "fake"}

    service = MuseService(
        MuseConfig(api_key="test-key"), client=FakeClient())
    service.generate_resume_json("Some resume text")
    assert captured["instructions"] == "SENTINEL CUSTOM PROMPT"


def test_system_prompt_test_endpoint_reports_unconfigured(
        prompt_file, monkeypatch, tmp_path):
    monkeypatch.delenv("MUSE_API_KEY", raising=False)
    monkeypatch.setenv("SATVIQ_ENV_FILE", str(tmp_path / "no-such.env"))
    body = client.post("/api/settings/system-prompt/test", json={}).json()
    assert body["ok"] is False
    assert body["configured"] is False
    assert "MUSE_API_KEY" in body["error"]
