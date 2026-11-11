"""Quality-based extraction with early stopping (sequential gate).

Spec scenarios — the pipeline must execute extractors ONE AT A TIME
in the order Tika -> PyMuPDF -> Docling -> OCR and STOP at the first
extractor whose text passes the existing Quality Checker gate
(quality_gate thresholds from extractor/config/extractor.yaml:
fail_below 50 / warn_below 75). Skipped extractors are never invoked
and never counted as attempts; the dashboard record must show the
real attempt count, the skipped stages and the final selected
extractor.

Texts are chosen against the REAL QualityChecker (verified):
- PASS_TEXT: Text Health 100 / Completeness 97 / Reading Order 100
- WARN_TEXT: prose with no resume sections -> Completeness low,
  Text Health / Reading Order high -> WARNING (held as candidate)
- FAIL_TEXT: replacement-char garbage -> Text Health 0 -> FAIL
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.pipeline import process_resume_file_simple
from extractor.models import ExtractionResult
from pipeline import ExtractionPipeline, PipelineConfig

PASS_TEXT = """Jane Doe
jane.doe@example.com | +91 98765 43210 | Pune, India

Professional Summary
Python developer building practical AI and backend applications for real users.
Focused on FastAPI services, data pipelines, and LLM powered tools with tested code.

Skills
Python, FastAPI, REST APIs, SQL, MySQL, Pandas, NumPy, LangChain, and Git are used daily.
Built backend services with authentication, validation, and structured logging.

Work Experience
Data Scientist Intern at Example AI Solutions from May 2025 to November 2025.
Cleaned datasets, trained classification models, and documented experiments.
Developed APIs and wrote tests for data processing pipelines used in production.

Education
Master of Computer Applications at Example University, Pune, 2025 to 2027.
Bachelor of Computer Applications completed in 2025 with distinction.

Projects
Data workbench for uploading, analysing, and visualising datasets with FastAPI.
PDF Chat Assistant using RAG, LangChain, FAISS, and FastAPI for document questions.

Certifications
Certified Data Scientist, completed in 2025 with additional ML coursework.
- Python programming
- FastAPI development
- Machine learning
"""

WARN_TEXT = (
    "John Smith is a software professional with experience in many "
    "technologies and tools used across different companies over the "
    "years including python and data work and web development and "
    "testing and databases and cloud platforms and many other things "
    "in various teams and projects of different sizes and domains."
    * 3)

FAIL_TEXT = "\ufffd" * 500

# Mixed good lines + fragmented lines: Text Health 100 / Reading
# Order 60 against the real QualityChecker — a genuine TECHNICAL
# warning (Reading Order between fail_below 50 and
# warning_continue_below 75), unlike WARN_TEXT whose only problem is
# unfamiliar sections. The gate must CONTINUE on this, not accept.
_TECH_WORDS = ("python developer building practical backend systems "
               "with fastapi sql docker and data pipelines for real "
               "product teams across india").split()
_TECH_GOOD_LINE = ("Jane Doe builds practical backend systems with "
                   "fastapi and sql, designs data pipelines, writes "
                   "tests and documents her work.")
TECH_WARN_TEXT = "\n".join(
    [_TECH_GOOD_LINE] * 4
    + [" ".join(_TECH_WORDS[i:i + 3]) for i in range(0, 45, 3)])

VALID_JSON_STR = json.dumps({
    "full_name": "Jane Doe",
    "email": "jane.doe@example.com",
    "phone": "+91 98765 43210",
    "location": "Pune, India",
    "summary": "Python developer",
    "skills": ["Python", "FastAPI"],
    "experience": [],
    "education": [],
    "certifications": [],
    "languages": [],
    "links": [],
})


class RecordingExtractor:
    """Fake extractor that records every invocation."""

    def __init__(self, name, text="", success=True, error=None,
                 log: list | None = None):
        self.name = name
        self._text = text
        self._success = success
        self._error = error
        self.log = log if log is not None else []

    def extract(self, path):
        self.log.append(self.name)
        if not self._success:
            return ExtractionResult(
                extractor_name=self.name, success=False, text="",
                error=self._error or "fake extractor failure",
                error_type="ExtractionFailedError", duration_ms=5.0,
                page_count=None, page_texts=[], metadata={})
        return ExtractionResult(
            extractor_name=self.name, success=True, text=self._text,
            error=None, duration_ms=5.0, page_count=1,
            page_texts=[self._text] if self._text else [], metadata={})


def _valid_llm(text):
    return VALID_JSON_STR


@pytest.fixture()
def dummy_pdf(tmp_path):
    p = tmp_path / "resume.pdf"
    p.write_bytes(b"%PDF-fake-content")
    return p


def _run(extractors, dummy_pdf):
    pipe = ExtractionPipeline(config=PipelineConfig(),
                              extractors=extractors,
                              llm_callable=_valid_llm)
    return pipe.run(dummy_pdf, include_extractor_texts=True)


# --- Spec scenario 1: Tika PASS -> Tika only -------------------------
def test_tika_pass_runs_tika_only(dummy_pdf):
    log: list = []
    result = _run({
        "tika": RecordingExtractor("tika", PASS_TEXT, log=log),
        "pymupdf": RecordingExtractor("pymupdf", PASS_TEXT, log=log),
        "docling": RecordingExtractor("docling", PASS_TEXT, log=log),
        "ocr": RecordingExtractor("ocr", PASS_TEXT, log=log),
    }, dummy_pdf)
    assert log == ["tika"]
    assert result["selected_extractor"] == "tika"
    assert result["gate_decisions"]["tika"]["status"] == "PASS"
    assert result["gate_decisions"]["tika"]["decision"] == "accept"
    assert result["skipped_extractors"] == ["ocr", "pymupdf", "docling"]
    assert [r["extractor"] for r in result["extractor_results"]] == ["tika"]
    assert result["status"] == "success"


# --- Spec scenario 2: Tika FAIL, PyMuPDF PASS ------------------------
def test_tika_fail_pymupdf_pass(dummy_pdf):
    # Order tika -> ocr -> pymupdf -> docling: after Tika fails,
    # OCR is next and passes, so PyMuPDF/Docling never run.
    log: list = []
    result = _run({
        "tika": RecordingExtractor("tika", "", success=False,
                                   error="tika boom", log=log),
        "pymupdf": RecordingExtractor("pymupdf", PASS_TEXT, log=log),
        "docling": RecordingExtractor("docling", PASS_TEXT, log=log),
        "ocr": RecordingExtractor("ocr", PASS_TEXT, log=log),
    }, dummy_pdf)
    assert log == ["tika", "ocr"]
    assert result["selected_extractor"] == "ocr"
    assert result["skipped_extractors"] == ["pymupdf", "docling"]


# --- Spec scenario 3: Tika PASS regardless of Docling availability ---
def test_tika_pass_with_broken_docling_still_tika_only(dummy_pdf):
    log: list = []

    class ExplodingExtractor:
        name = "docling"

        def extract(self, path):  # pragma: no cover - must never run
            raise AssertionError("docling must never be invoked")

    result = _run({
        "tika": RecordingExtractor("tika", PASS_TEXT, log=log),
        "pymupdf": RecordingExtractor("pymupdf", PASS_TEXT, log=log),
        "docling": ExplodingExtractor(),
        "ocr": RecordingExtractor("ocr", PASS_TEXT, log=log),
    }, dummy_pdf)
    assert log == ["tika"]
    assert result["selected_extractor"] == "tika"


# --- Spec scenario 4: everything before Docling fails, Docling PASS --
def test_tika_and_pymupdf_fail_docling_pass(dummy_pdf):
    log: list = []
    result = _run({
        "tika": RecordingExtractor("tika", FAIL_TEXT, log=log),
        "pymupdf": RecordingExtractor("pymupdf", "", success=False,
                                      error="pymupdf boom", log=log),
        "docling": RecordingExtractor("docling", PASS_TEXT, log=log),
        "ocr": RecordingExtractor("ocr", "", success=False,
                                   error="ocr boom", log=log),
    }, dummy_pdf)
    assert log == ["tika", "ocr", "pymupdf", "docling"]
    assert result["selected_extractor"] == "docling"
    assert result["gate_decisions"]["tika"]["status"] == "FAIL"
    assert result["skipped_extractors"] == []


# --- Spec scenario 5: OCR (second) rescues a scanned document -------
def test_all_three_fail_then_ocr(dummy_pdf):
    log: list = []
    result = _run({
        "tika": RecordingExtractor("tika", FAIL_TEXT, log=log),
        "pymupdf": RecordingExtractor("pymupdf", FAIL_TEXT, log=log),
        "docling": RecordingExtractor("docling", "", success=False,
                                      error="docling boom", log=log),
        "ocr": RecordingExtractor("ocr", PASS_TEXT, log=log),
    }, dummy_pdf)
    assert log == ["tika", "ocr"]
    assert result["selected_extractor"] == "ocr"
    assert result["skipped_extractors"] == ["pymupdf", "docling"]


# --- Spec scenario 6: every extractor fails -> FAILED/REJECTED -------
def test_all_extractors_fail_is_rejected(dummy_pdf):
    log: list = []
    result = _run({
        "tika": RecordingExtractor("tika", "", success=False, log=log),
        "pymupdf": RecordingExtractor("pymupdf", "", success=False, log=log),
        "docling": RecordingExtractor("docling", "", success=False, log=log),
        "ocr": RecordingExtractor("ocr", "", success=False, log=log),
    }, dummy_pdf)
    assert log == ["tika", "ocr", "pymupdf", "docling"]
    assert result["selected_extractor"] is None
    assert result["status"] in ("extraction_failed", "below_threshold")
    assert result["llm"]["status"] == "skipped"


# --- WARNING continues (revised 2026-11-12) ---------------------------
# This test previously asserted the 2026-11-11 Part A behaviour: a
# completeness-only WARNING from Tika was ACCEPTED immediately and
# PyMuPDF/Docling never ran. That rule caused the download_11_.pdf
# incident (Tika WARNING fragment accepted, everything skipped), and
# Milind's 2026-11-12 spec reversed it: a WARNING is held as a
# candidate and the next extractor is tried. The Part A scenario is
# still protected — by the convergence stop (see
# test_convergence_stops_when_extractors_agree), not by accepting
# the first WARNING.
def test_warning_continues_to_next_extractor(dummy_pdf):
    log: list = []
    result = _run({
        "tika": RecordingExtractor("tika", WARN_TEXT, log=log),
        "pymupdf": RecordingExtractor("pymupdf", PASS_TEXT, log=log),
        "docling": RecordingExtractor("docling", PASS_TEXT, log=log),
    }, dummy_pdf)
    assert log == ["tika", "pymupdf"]
    assert result["gate_decisions"]["tika"]["status"] == "WARNING"
    assert result["gate_decisions"]["tika"]["decision"] == "continue"
    assert result["selected_extractor"] == "pymupdf"
    assert result["skipped_extractors"] == ["docling"]


def test_convergence_stops_when_extractors_agree(dummy_pdf):
    # Two extractors return substantially the same WARNING text:
    # the content is genuinely what the document holds, so the
    # chain stops and Docling never runs. The texts are long
    # (~2,800 chars), so OCR is necessity-skipped before PyMuPDF.
    log: list = []
    long_warn = WARN_TEXT * 3
    result = _run({
        "tika": RecordingExtractor("tika", long_warn, log=log),
        "pymupdf": RecordingExtractor("pymupdf", long_warn, log=log),
        "docling": RecordingExtractor("docling", PASS_TEXT, log=log),
        "ocr": RecordingExtractor("ocr", PASS_TEXT, log=log),
    }, dummy_pdf)
    assert log == ["tika", "pymupdf"]
    assert result["gate_decisions"]["ocr"]["decision"] == "skip"
    assert result["skipped_extractors"] == ["ocr", "docling"]
    assert result["selected_extractor"] in ("tika", "pymupdf")
    reason = result["gate_decisions"][result["selected_extractor"]]["reason"]
    assert "convergence" in reason


# --- download_11_.pdf regression --------------------------------------
# Real file (tests/fixtures/download_11_.pdf): a one-page,
# image-composed graphic-designer resume with NO text layer.
# Measured on the real file: PyMuPDF extracts 0 characters. On
# Milind's machine Tika returned a 771-character WARNING fragment
# detecting only Contact Information, and the old gate accepted it
# and skipped every other extractor. TIKA_FRAGMENT below reproduces
# that fragment's QC profile against the real checker (748 chars,
# Text Health 100 / Completeness 52 / Reading Order 65.7, detected:
# Contact Information only -> WARNING).
TIKA_FRAGMENT = """Yogesh M
Graphic Designer
8939785374
Yogeshraj006@gmail.com
Portfolio - click here
I am an aspiring graphic designer who is always curious to learn new things, both within and beyond the field of graphic design. I have worked with design teams on branding, print layouts, brochures, magazines, motion graphics and image editing projects for clients in retail, interiors and agriculture businesses over the last four years, handling everything from concept sketches to final print ready files, coordinating with printers and clients, revising artwork based on feedback, and delivering creatives for social media campaigns, product catalogues and exhibition displays on tight deadlines while keeping a consistent visual style across all deliverables."""

DOCLING_FRAGMENT = """Yogesh M Graphic Designer 8939785374 Yogeshraj006@gmail.com
Portfolio click here About I am an aspiring graphic designer who is
always curious to learn new things both within and beyond the field
of graphic design. Education Bsc MultiMedia Image Creative Education
2021-2024 Graphic Designing 2019-2020 Higher Secondary 2017-2018
Experience Sameera groups Jun 2024 to Present Mebel Interiors
Dosindia Richtech Agrofarms Languages Tamil English"""

OCR_FULL = """Yogesh M
Graphic Designer
8939785374
Yogeshraj006@gmail.com
Portfolio - click here

About
I am an aspiring graphic designer who is always curious to learn new things, both within and beyond the field of graphic design.

Education
Bsc MultiMedia, Image Creative Education, 2021-2024
Graphic Designing, Image Creative Education, 2019-2020
Higher Secondary, Rcm Hr Sec School, 2017-2018

Software Skills
Photoshop, Premiere Pro, Illustrator, After Effects, Lightroom, InDesign

Experience
Sameera groups, Jun 2024 to Present
Mebel Interiors, Jun to May 2023-24
Dosindia, Apr to May 2022-23
Richtech Agrofarms india limited, Aug to Apr 2021-22

Technical Skills
Image Editing and Manipulation, Motion Graphics, Print Design and Production, Print Layouts, Brochures, Magazines

Languages
Tamil, English
"""


def test_download11_warning_fragment_never_final(dummy_pdf):
    log: list = []
    result = _run({
        "tika": RecordingExtractor("tika", TIKA_FRAGMENT, log=log),
        # The real file has no text layer: PyMuPDF genuinely fails.
        "pymupdf": RecordingExtractor("pymupdf", "", success=False,
                                      log=log),
        "docling": RecordingExtractor("docling", DOCLING_FRAGMENT,
                                      log=log),
        "ocr": RecordingExtractor("ocr", OCR_FULL, log=log),
    }, dummy_pdf)
    # OCR is second in the chain now: it runs immediately after
    # the Tika WARNING (image-based signals: 1 section, <800 chars)
    # and its complete extraction PASSes, so PyMuPDF and Docling
    # are never needed. The Tika fragment is never final.
    assert log == ["tika", "ocr"]
    assert result["skipped_extractors"] == ["pymupdf", "docling"]
    assert result["gate_decisions"]["tika"]["status"] == "WARNING"
    assert result["gate_decisions"]["tika"]["decision"] == "continue"
    sig = result["gate_decisions"]["tika"]["signals"]
    assert sig["detected_sections"] == 1
    assert sig["chars"] < 800
    # The complete OCR extraction wins — not the Tika fragment.
    assert result["selected_extractor"] == "ocr"


def test_ocr_skipped_when_text_candidate_adequate(dummy_pdf):
    log: list = []
    long_warning = DOCLING_FRAGMENT * 5  # ~2,215 chars, WARNING
    result = _run({
        "tika": RecordingExtractor("tika", long_warning, log=log),
        "pymupdf": RecordingExtractor("pymupdf", FAIL_TEXT, log=log),
        "docling": RecordingExtractor("docling", WARN_TEXT, log=log),
        "ocr": RecordingExtractor("ocr", PASS_TEXT, log=log),
    }, dummy_pdf)
    assert "ocr" not in log
    assert result["gate_decisions"]["ocr"]["decision"] == "skip"
    assert "OCR skipped" in result["gate_decisions"]["ocr"]["reason"]
    assert result["skipped_extractors"] == ["ocr"]


def test_real_pymupdf_extracts_nothing_from_download11():
    # Anchor against the real file and the real extractor: the PDF
    # has no text layer, so a text extractor MUST come back empty —
    # the pipeline can never rely on it for this document.
    from pipeline.extractors import (default_sequential_extractors,
                                     run_one_extractor)
    fixture = Path(__file__).parent / "fixtures" / "download_11_.pdf"
    if not fixture.exists():
        pytest.skip("download_11_.pdf fixture not present")
    extractors = default_sequential_extractors()
    if "pymupdf" not in extractors:
        pytest.skip("pymupdf extractor unavailable")
    res = run_one_extractor("pymupdf", extractors["pymupdf"], fixture)
    assert len((res.get("extracted_text") or "").strip()) == 0


def test_warning_candidate_selected_when_nothing_passes(dummy_pdf):
    # A TECHNICAL warning (garbled order -> low technical scores)
    # still continues the chain and is held as a candidate; when
    # nothing passes outright the selector may pick it as best.
    log: list = []
    result = _run({
        "tika": RecordingExtractor("tika", TECH_WARN_TEXT, log=log),
        "pymupdf": RecordingExtractor("pymupdf", "", success=False,
                                      error="pymupdf boom", log=log),
        "docling": RecordingExtractor("docling", FAIL_TEXT, log=log),
    }, dummy_pdf)
    assert log == ["tika", "pymupdf", "docling"]
    assert result["gate_decisions"]["tika"]["decision"] == "continue"
    # The fragmented candidate's deterministic score is below the
    # selector threshold too, so nothing is selected — the point of
    # this scenario is the CONTINUE decision, not the final pick.
    assert result["selected_extractor"] is None


# --- Dashboard/record accuracy via the API record builder ------------
def test_record_shows_skipped_and_real_attempt_count(dummy_pdf):
    log: list = []
    pipe = ExtractionPipeline(config=PipelineConfig(), extractors={
        "tika": RecordingExtractor("tika", PASS_TEXT, log=log),
        "pymupdf": RecordingExtractor("pymupdf", PASS_TEXT, log=log),
        "docling": RecordingExtractor("docling", PASS_TEXT, log=log),
        "ocr": RecordingExtractor("ocr", PASS_TEXT, log=log),
    }, llm_callable=_valid_llm)
    record = process_resume_file_simple(
        dummy_pdf, resume_id="early-stop-1",
        original_filename="resume.pdf", pipeline=pipe)
    assert log == ["tika"]
    # Attempts count only extractors that actually executed.
    assert record["attempt_count"] == 1
    assert [a["extractor_name"] for a in record["attempts"]] == ["tika"]
    assert record["extractor_used"] == "tika"
    assert record["final_extractor"] == "tika"
    assert record["attempts"][0]["is_final_selected"] is True
    assert record["attempts"][0]["quality_status"] == "PASS"
    assert record["quality_status"] == "PASS"
    assert record["fallback_occurred"] is False
    assert record["skipped_extractors"] == ["ocr", "pymupdf", "docling"]
    # Trace shows every non-executed extractor as SKIPPED, in order.
    stages = [(s["stage"], s["status"]) for s in record["pipeline_trace"]]
    extractor_stages = [(n, st) for n, st in stages
                        if n.endswith("Extractor")]
    assert extractor_stages == [
        ("Tika Extractor", "PASS"),
        ("OCR Extractor", "SKIPPED"),
        ("PyMuPDF Extractor", "SKIPPED"),
        ("Docling Extractor", "SKIPPED"),
    ]
    # Only the selected extractor's text was sent onward.
    assert record["text_normalization"]["source"] == "tika"
    assert record["resume_json"] is not None
