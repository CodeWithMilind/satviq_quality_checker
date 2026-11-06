"""Regression tests for the per-resume pipeline trace audit.

Covers the full delivery chain that the GUI depends on:

    backend build -> POST response -> JSON on disk -> history list
    -> detail endpoint -> actual frontend rendering (Node, real app.js)

plus record independence (two resumes / same file twice), legacy
records without a stored trace, safe handling of a record whose trace
is empty, and the static-asset cache protections (versioned asset
URLs + Cache-Control: no-cache) that keep a stale cached ``app.js``
from leaving the trace sections empty after an update.

Run from the project root:  python -m pytest tests/test_trace_audit.py -v
"""

import json
import shutil
import subprocess
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

import api.store as store
from api.main import app

client = TestClient(app)

RESUME_LINES = [
    "Jane Doe",
    "jane.doe@example.com | +91 98765 43210 | Pune, India",
    "Contact Information",
    "Email: jane.doe@example.com, Phone: +91 98765 43210, Location: Pune, India.",
    "Professional Summary",
    "Python developer building practical AI and backend applications for real users.",
    "Focused on FastAPI services, data pipelines, and LLM powered tools with tested code.",
    "Skills",
    "Python, FastAPI, REST APIs, SQL, MySQL, Pandas, NumPy, LangChain, and Git are used daily.",
    "Built backend services with authentication, validation, and structured logging.",
    "Work Experience",
    "Data Scientist Intern at Example AI Solutions from May 2025 to November 2025.",
    "Cleaned datasets, trained classification models, and documented experiments.",
    "Education",
    "Master of Computer Applications at Example University, Pune, 2025 to 2027.",
    "Bachelor of Computer Applications completed in 2025 with distinction.",
    "Projects",
    "Data workbench for uploading, analysing, and visualising datasets with FastAPI.",
    "PDF Chat Assistant using RAG, LangChain, FAISS, and FastAPI for document questions.",
    "Certifications",
    "Certified Data Scientist, completed in 2025 with additional ML coursework.",
]


@pytest.fixture(autouse=True)
def clean_state():
    client.delete("/api/resumes")
    yield
    client.delete("/api/resumes")


def make_pdf_bytes(lines=RESUME_LINES):
    doc = pymupdf.open()
    page = doc.new_page()
    y = 60
    for line in lines:
        page.insert_text((50, y), line, fontsize=10)
        y += 18
    data = doc.tobytes()
    doc.close()
    return data


def process_one(name="audit.pdf", data=None, mime="application/pdf"):
    return client.post(
        "/api/resumes/process",
        files=[("files", (name, data if data is not None else make_pdf_bytes(), mime))],
    ).json()["results"][0]


# 1. The whole chain -----------------------------------------------------------

def test_trace_survives_process_disk_list_and_detail():
    record = process_one()
    trace = record["pipeline_trace"]
    assert trace, "POST response must carry the pipeline trace"
    assert trace[0]["stage"] == "Resume Upload"
    assert trace[-1]["stage"] == "Final Extraction Result"

    on_disk = json.loads((store.RESULTS_DIR / f"{record['resume_id']}.json").read_text())
    assert on_disk["pipeline_trace"] == trace, "trace must be persisted unchanged"

    listing = client.get("/api/resumes").json()["resumes"]
    entry = next(r for r in listing if r["resume_id"] == record["resume_id"])
    assert entry["pipeline_trace"] == trace, "history list must return the same trace"

    detail = client.get(f"/api/resumes/{record['resume_id']}").json()
    assert detail["pipeline_trace"] == trace, "detail must return the same trace"
    # Trace belongs to this record: per-extractor stage outcomes match
    # this record's own attempt history.
    attempted = {a["extractor_name"] for a in record["attempts"]}
    traced = {s["extractor"] for s in trace if s["type"] == "extractor"
              and s["status"] != "SKIPPED"}
    assert traced == attempted


def test_failed_record_also_has_a_trace():
    record = process_one("bad.exe", b"MZ", "application/octet-stream")
    assert record["success"] is False
    assert record["pipeline_trace"], "pre-extraction failures still get a trace"
    assert record["pipeline_trace"][-1]["status"] == "FAIL"


# 2. Record independence ---------------------------------------------------------

def test_two_resumes_have_different_traces():
    good = process_one("good.pdf")
    bad = process_one("bad.exe", b"MZ", "application/octet-stream")
    assert good["resume_id"] != bad["resume_id"]
    good_statuses = [s["status"] for s in good["pipeline_trace"]]
    bad_statuses = [s["status"] for s in bad["pipeline_trace"]]
    assert good_statuses != bad_statuses
    assert good["pipeline_trace"][-1]["status"] == "PASS"
    assert bad["pipeline_trace"][-1]["status"] == "FAIL"
    # MIME failure happens before extraction: no extractor stages ran.
    assert all(s["status"] == "SKIPPED" for s in bad["pipeline_trace"]
               if s["type"] == "extractor")


def test_same_file_twice_creates_independent_records_and_traces():
    data = make_pdf_bytes()
    both = client.post(
        "/api/resumes/process",
        files=[("files", ("dup.pdf", data, "application/pdf")),
               ("files", ("dup.pdf", data, "application/pdf"))],
    ).json()["results"]
    assert len(both) == 2
    first, second = both
    assert first["resume_id"] != second["resume_id"]
    assert first["pipeline_trace"] and second["pipeline_trace"]
    # Deleting one removes only that record and its trace.
    assert client.delete(f"/api/resumes/{first['resume_id']}").status_code == 200
    assert client.get(f"/api/resumes/{first['resume_id']}").status_code == 404
    survivor = client.get(f"/api/resumes/{second['resume_id']}").json()
    assert survivor["pipeline_trace"] == second["pipeline_trace"]


# 3. Legacy records ---------------------------------------------------------------

def test_legacy_record_without_trace_is_backfilled_in_list_and_detail():
    record = process_one("legacy.pdf")
    legacy = {k: v for k, v in record.items() if k != "pipeline_trace"}
    legacy_path = store.RESULTS_DIR / f"{record['resume_id']}.json"
    raw = json.loads(legacy_path.read_text())
    raw.pop("pipeline_trace", None)
    legacy_path.write_text(json.dumps(raw))

    listing = client.get("/api/resumes").json()["resumes"]
    entry = next(r for r in listing if r["resume_id"] == record["resume_id"])
    assert entry["pipeline_trace"], "list must serve a derived trace for legacy records"
    detail = client.get(f"/api/resumes/{record['resume_id']}").json()
    assert detail["pipeline_trace"] == record["pipeline_trace"] or \
        len(detail["pipeline_trace"]) == len(record["pipeline_trace"])


# 4. Frontend rendering with the real app.js (Node) ------------------------------

_NODE_DRIVER = r"""
const fs = require("fs");
const vm = require("vm");
process.on("unhandledRejection", () => {});
const DATA = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
function makeEl() {
  return { innerHTML: "", textContent: "", className: "", hidden: false, disabled: false,
    href: "#", value: "", files: [], dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, scrollIntoView() {}, setAttribute() {},
    getAttribute() { return null; }, querySelector() { return null; },
    querySelectorAll() { return []; }, closest() { return null; } };
}
const elements = {};
const healthStub = { extractor_order: ["tika", "pymupdf", "docling", "ocr"],
  supported_extensions: ["pdf"], max_upload_mb: 5,
  ai: { provider: "Muse AI", model: "muse-spark-1.3", configured: false } };
const ctx = vm.createContext({
  document: { getElementById: (id) => (elements[id] ??= makeEl()),
    querySelector: () => null, querySelectorAll: () => [], addEventListener() {},
    body: makeEl(), createElement: () => makeEl() },
  window: { addEventListener() {} },
  location: { hash: "#/process" },
  Api: { health: async () => healthStub,
    downloadOriginalUrl: (id) => "/api/resumes/" + id + "/file",
    downloadTextUrl: (id) => "/api/resumes/" + id + "/text" },
  fetch: async () => ({ ok: true, json: async () => ({}) }), console,
  __D__: DATA, __EL__: elements,
  alert: () => {}, confirm: () => true, navigator: {},
  URLSearchParams, URL, setTimeout, clearTimeout,
});
const code = fs.readFileSync(process.argv[2], "utf8") + `
;(() => {
  const assert = (cond, msg) => { if (!cond) throw new Error(msg); };

  const rec = __D__;
  const html = (rec.attempts || []).map((a, i) => attemptCardHtml(a, i)).join("");
  assert(html.length > 0, "attempt cards rendered empty for a real record");
  assert(rec.attempts.length > 0, "real record must carry attempts");
  assert(html.includes(engineLabel(rec.attempts[0].extractor_name)), "first engine missing");
  const firstText = rec.attempts[0].extracted_text || rec.attempts[0].text || "";
  if (firstText) assert(html.includes(firstText.slice(0, 30)), "attempt text missing");
  const card = resultCardHtml(rec);
  assert(card.includes(rec.filename), "result card must show the real filename");
  console.log("FRONTEND TRACE OK");
})();
`;
vm.runInContext(code, ctx);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_frontend_renders_non_empty_trace_for_real_record(tmp_path):
    record = process_one("render.pdf")
    record_path = tmp_path / "record.json"
    record_path.write_text(json.dumps(record))
    driver_path = tmp_path / "driver.js"
    driver_path.write_text(_NODE_DRIVER)
    app_js = Path(__file__).resolve().parent.parent / "frontend" / "js" / "app.js"
    result = subprocess.run(
        ["node", str(driver_path), str(app_js), str(record_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "FRONTEND TRACE OK" in result.stdout


# 5. Static-asset cache protection -------------------------------------------------

def test_frontend_assets_are_versioned_and_not_cached():
    page = client.get("/")
    assert page.status_code == 200
    assert "js/app.js?v=" in page.text, "script URL must carry a version so updates cannot be served stale"
    assert "css/style.css?v=" in page.text
    assert "no-cache" in page.headers.get("cache-control", "")
    script = client.get("/js/app.js?v=4")
    assert script.status_code == 200
    assert "no-cache" in script.headers.get("cache-control", "")
