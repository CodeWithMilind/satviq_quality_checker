"""Tests for the interactive per-resume flowchart.

Two layers:

1. Backend: the structured ``pipeline_trace`` returned by the API
   must contain everything the flowchart renders — one stage per real
   step, separated extraction vs Quality Checker outcomes, the gate's
   decision per attempt, per-attempt quality payloads, and the final
   selection — for each required scenario (accept, fallback after
   failure, quality rejection, WARNING candidate flow, all attempted).

2. Frontend: the REAL ``frontend/js/app.js`` (executed in Node)
   renders one flowchart node per backend stage, labels the decision
   edges from the actual decisions, puts the real scores / errors /
   completeness detail into the click-to-expand node panels, and
   toggles node details via ``toggleFlowNode``.

Run from the project root:  python -m pytest tests/test_flowchart.py -v
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from api.pipeline import QualityGatePolicy, build_record_from_result
from extractor import ExtractionManager
from extractor.base import BaseExtractor
from quality_checker import QualityChecker

_PASS_LINES = [
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
PASS_TEXT = "\n".join(_PASS_LINES)
WARN_TEXT = (  # Completeness 58 -> WARNING (measured against the real filters)
    "Priya Nair\n"
    "priya.nair@example.com | +91 98220 12345 | Pune, India\n"
    "Contact Information\n"
    "Email: priya.nair@example.com, Phone: +91 98220 12345, Location: Pune, India.\n"
    "Professional Summary\n"
    "Careful developer who writes Python programs and simple web pages for "
    "local clients. She tests her work, writes clear notes, and fixes "
    "defects methodically over time."
)
CORRUPT_TEXT = "�" * 500

# Technical-failure fixtures (fragmented lines -> Reading Order in
# FAIL territory, Text Health 100, Completeness 97): FRAG_BEST
# 100/97/46.5, FRAG_WORST 100/97/41.7, measured with the real checker.
FRAG_BEST = "\n".join([
    'Jane Doe',
    'jane.doe@example.com |',
    '+91 98765',
    '43210 |',
    'Pune, India',
    'Contact Information',
    'Email: jane.doe@example.com,',
    'Phone: +91',
    '98765 43210,',
    'Location: Pune,',
    'India.',
    'Professional Summary',
    'Python developer',
    'building practical',
    'AI and',
    'backend applications',
    'for real',
    'users.',
    'Focused on',
    'FastAPI services,',
    'data pipelines,',
    'and LLM',
    'powered tools',
    'with tested',
    'code.',
    'Skills',
    'Python, FastAPI,',
    'REST APIs,',
    'SQL, MySQL,',
    'Pandas, NumPy,',
    'LangChain, and',
    'Git are',
    'used daily.',
    'Built backend',
    'services with',
    'authentication, validation,',
    'and structured',
    'logging.',
    'Work Experience',
    'Data Scientist',
    'Intern at',
    'Example AI',
    'Solutions from',
    'May 2025',
    'to November',
    '2025.',
    'Cleaned datasets, trained classification models, and documented experiments.',
    'Education',
    'Master of Computer Applications at Example University, Pune, 2025 to 2027.',
    'Bachelor of Computer Applications completed in 2025 with distinction.',
    'Projects',
    'Data workbench for uploading, analysing, and visualising datasets with FastAPI.',
    'PDF Chat Assistant using RAG, LangChain, FAISS, and FastAPI for document questions.',
    'Certifications',
    'Certified Data Scientist, completed in 2025 with additional ML coursework.',
])
FRAG_WORST = "\n".join([
    'Jane Doe',
    'jane.doe@example.com |',
    '+91 98765',
    '43210 |',
    'Pune, India',
    'Contact Information',
    'Email: jane.doe@example.com,',
    'Phone: +91',
    '98765 43210,',
    'Location: Pune,',
    'India.',
    'Professional Summary',
    'Python developer',
    'building practical',
    'AI and',
    'backend applications',
    'for real',
    'users.',
    'Focused on',
    'FastAPI services,',
    'data pipelines,',
    'and LLM',
    'powered tools',
    'with tested',
    'code.',
    'Skills',
    'Python, FastAPI,',
    'REST APIs,',
    'SQL, MySQL,',
    'Pandas, NumPy,',
    'LangChain, and',
    'Git are',
    'used daily.',
    'Built backend',
    'services with',
    'authentication, validation,',
    'and structured',
    'logging.',
    'Work Experience',
    'Data Scientist',
    'Intern at',
    'Example AI',
    'Solutions from',
    'May 2025',
    'to November',
    '2025.',
    'Cleaned datasets,',
    'trained classification',
    'models, and',
    'documented experiments.',
    'Education',
    'Master of',
    'Computer Applications',
    'at Example',
    'University, Pune,',
    '2025 to',
    '2027.',
    'Bachelor of',
    'Computer Applications',
    'completed in',
    '2025 with',
    'distinction.',
    'Projects',
    'Data workbench',
    'for uploading,',
    'analysing, and',
    'visualising datasets',
    'with FastAPI.',
    'PDF Chat',
    'Assistant using',
    'RAG, LangChain,',
    'FAISS, and',
    'FastAPI for',
    'document questions.',
    'Certifications',
    'Certified Data',
    'Scientist, completed',
    'in 2025',
    'with additional',
    'ML coursework.',
])

FAIL = None


class FakeExtractor(BaseExtractor):
    def __init__(self, name, text):
        super().__init__(config={})
        self.name = name
        self.text = text

    def _extract(self, path):
        if self.text is FAIL:
            return self._failure(path, f"{self.name} fake failure")
        return self._success(path, self.text)


def _record(tmp_path, behaviours, resume_id="flow-0001"):
    names = [n for n, _ in behaviours]
    policy = QualityGatePolicy(QualityChecker(), document_id=resume_id)
    manager = ExtractionManager(
        config={"manager": {"order": names, "max_attempts": 10}},
        extractors={n: FakeExtractor(n, t) for n, t in behaviours},
        acceptance_policy=policy,
    )
    sample = tmp_path / "resume.pdf"
    sample.write_bytes(b"%PDF-fake")
    result = manager.extract(sample)
    return build_record_from_result(
        resume_id=resume_id, original_filename="resume.pdf", path=sample,
        mime_type="application/pdf", file_size=sample.stat().st_size,
        result=result, policy=policy, order=names)


def _stage(record, type_, extractor=None):
    return next(s for s in record["pipeline_trace"]
                if s["type"] == type_ and (extractor is None or s["extractor"] == extractor))


# Backend trace content ---------------------------------------------------------

def test_trace_has_everything_the_flowchart_needs(tmp_path):
    record = _record(tmp_path, [("tika", WARN_TEXT), ("pymupdf", PASS_TEXT)])
    for stage in record["pipeline_trace"]:
        assert stage["stage"] and stage["status"] and stage["type"]
        assert "message" in stage and "duration_ms" in stage
    quality = _stage(record, "quality", "tika")
    assert quality["quality"] is not None
    for key in ("text_health", "completeness", "reading_order"):
        assert isinstance(quality["quality"][key]["score"], (int, float))
    # WARN_TEXT is a completeness-only WARNING: under extraction-only
    # gating it is accepted immediately (fallback stops).
    assert quality["decision"] == "accept"


def test_scenario_a_accepted_first_extractor_rest_skipped(tmp_path):
    record = _record(tmp_path, [("tika", PASS_TEXT), ("pymupdf", PASS_TEXT),
                                ("docling", PASS_TEXT), ("ocr", PASS_TEXT)])
    assert _stage(record, "quality", "tika")["decision"] == "accept"
    for name in ("pymupdf", "docling", "ocr"):
        assert _stage(record, "extractor", name)["status"] == "SKIPPED"
    assert _stage(record, "final")["selected"] is True


def test_scenario_b_failure_then_fallback_success(tmp_path):
    record = _record(tmp_path, [("tika", FAIL), ("pymupdf", PASS_TEXT)])
    tika = _stage(record, "extractor", "tika")
    assert tika["status"] == "FAIL"
    assert "tika fake failure" in tika["message"]
    assert _stage(record, "quality", "tika")["status"] == "SKIPPED"
    assert record["fallback_occurred"] is True
    assert "tika fake failure" in record["fallback_reason"]


def test_scenario_c_extraction_pass_but_quality_rejected(tmp_path):
    record = _record(tmp_path, [("tika", CORRUPT_TEXT), ("pymupdf", PASS_TEXT)])
    assert _stage(record, "extractor", "tika")["status"] == "PASS", \
        "the extractor itself must not be labelled failed"
    quality = _stage(record, "quality", "tika")
    assert quality["status"] == "REJECTED"
    assert quality["decision"] == "reject"
    assert quality["quality"]["text_health"]["score"] < 50


def test_scenario_d_all_attempted_best_candidate_selected(tmp_path):
    """All extractors attempted with technically-failed texts: the
    least-broken candidate is nominated and the trace shows every
    attempt's real gate decision."""
    record = _record(tmp_path, [("tika", FRAG_WORST), ("pymupdf", FRAG_BEST),
                                ("docling", FAIL)])
    assert record["attempt_count"] == 3
    final = _stage(record, "final")
    assert final["selected"] is True
    assert record["extractor_used"] == "pymupdf"
    assert "Best candidate selected" in record["selection_reason"]
    held = _stage(record, "quality", "tika")
    assert held["status"] == "REJECTED" and held["decision"] == "reject"


# Frontend rendering (Node, real app.js) -----------------------------------------

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

  const renderAttempts = (rec) => (rec.attempts || []).map((a, i) => attemptCardHtml(a, i)).join("");
  for (const key of ["warnThenPass", "failThenPass", "accepted"]) {
    const rec = __D__[key];
    const html = renderAttempts(rec);
    assert(html.length > 0, key + ": attempts rendered empty");
    for (const a of rec.attempts) {
      assert(html.includes(engineLabel(a.extractor_name)), key + ": engine missing");
      const text = a.extracted_text || a.text || "";
      if (text) assert(html.includes(text.slice(0, 30)), key + ": attempt text missing");
      if (a.error_message) assert(html.includes(a.error_message.slice(0, 30)), key + ": error missing");
    }
    const selected = rec.attempts.find((a) => a.is_final_selected || a.selected);
    if (selected) assert(html.includes("Accepted"), key + ": selected attempt not marked Accepted");
  }
  assert(renderAttempts(__D__.warnThenPass) !== renderAttempts(__D__.failThenPass),
    "different runs must render different attempt lists");
  assert(overallScore({}) === null, "missing quality must give no score (UI shows N/A)");
  console.log("ATTEMPTS FRONTEND OK");
})();
`;
vm.runInContext(code, ctx);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_frontend_attempt_cards_render_real_records(tmp_path):
    """Production UI (v14) supersedes the flowchart with compact
    extraction-attempt cards; the guarantee kept from the flowchart
    tests is that every real attempt (engine, text, error, selection)
    renders from the record's actual data."""
    records = {
        "warnThenPass": _record(tmp_path, [("tika", WARN_TEXT), ("pymupdf", PASS_TEXT)], "flow-a"),
        "failThenPass": _record(tmp_path, [("tika", FAIL), ("pymupdf", PASS_TEXT)], "flow-b"),
        "accepted": _record(tmp_path, [("tika", PASS_TEXT), ("pymupdf", PASS_TEXT),
                                       ("docling", PASS_TEXT)], "flow-c"),
    }
    data_path = tmp_path / "records.json"
    data_path.write_text(json.dumps(records))
    driver_path = tmp_path / "driver.js"
    driver_path.write_text(_NODE_DRIVER)
    app_js = Path(__file__).resolve().parent.parent / "frontend" / "js" / "app.js"
    result = subprocess.run(
        ["node", str(driver_path), str(app_js), str(data_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "ATTEMPTS FRONTEND OK" in result.stdout
