# SATVIQ Resume Extraction Quality Checker

A modular, testable **Quality Checker** for the SATVIQ resume extraction
pipeline. After an extraction tool (Apache Tika, PyMuPDF, Docling, OCR, …)
produces text, this module evaluates whether that text is usable — using
exactly three independent, deterministic filters. No LLM is used anywhere.

```
Extracted Resume Text
        |
        v
Quality Checker
        |
        +-----------------------+
        |           |           |
        v           v           v
  Text Health  Completeness  Reading Order
    Filter        Filter        Filter
        |           |           |
        +-----------+-----------+
                    |
                    v
          Individual Filter Results
                    |
                    v
             Quality Report
                    |
                    v
        Return report to caller
```

## What this module does — and does not do

**Does:** evaluate extracted text with the three filters below and return a
structured `QualityReport` (Pydantic v2) to the caller.

**Does not:** extract text, select/execute fallbacks, aggregate the three
scores into an overall score, make acceptance/rejection decisions, run
LLM/SLM recovery, do resume–JD matching, RAG, manual-review workflow, or
layout-consistency checking. Those decisions belong to the caller.

Each filter result stays independently accessible on the report —
`report.text_health`, `report.completeness`, `report.reading_order` —
and there is intentionally **no** `overall_score` field.

## The three filters

### 1. Text Health (`filters/text_health.py`)
Is the text technically readable? Checks: empty / nearly empty text,
character & word counts, replacement characters (`�`), non-printable
characters, whitespace ratio, symbol-to-text ratio, and repeated-character
corruption (long runs and single-character repeated lines).
Returns score 0–100, flags, reasons, and raw metrics.

### 2. Completeness (`filters/completeness.py`)
Do expected resume sections appear to be present? Sections, aliases and
importance (`required` / `recommended` / `optional`) are fully configured
in YAML — e.g. `Experience`, `Professional Experience` and
`Employment History` all map to *Work Experience*. Contact Information can
also be detected from an email/phone pattern, since it rarely has a heading.
Returns score 0–100, detected sections, missing sections (split by
importance), flags, reasons, and metrics.

> A missing section is treated as a **possible omission**, not proof that
> extraction failed — the reasons say exactly that. Optional sections cost
> very few points; no single keyword is required in every resume.

### 3. Reading Order (`filters/reading_order.py`)
Does the text preserve a reasonable reading order? Checks: excessive
one-word / very short lines, fragmented lines, line-length variation
(coefficient of variation), possible broken sentences (line ends without
punctuation, next line starts lowercase), repeated adjacent lines, and
long text collapsed into very few lines (a merged-column clue).

> This filter only evaluates clues available in the extracted text. It does
> **not** claim to detect the original visual layout perfectly.

## Usage

```python
from quality_checker import QualityChecker

checker = QualityChecker()  # loads the bundled YAML configs

report = checker.check(
    extracted_text,
    document_id="resume-123",
    source_tool="pymupdf",       # or "tika", "docling", "ocr", ...
    attempt_id="attempt-1",
)

print(report.text_health.score)        # e.g. 100.0
print(report.text_health.flags)        # e.g. []
print(report.completeness.detected_sections)
print(report.completeness.missing_sections)
print(report.reading_order.metrics["short_line_ratio"])
print(report.model_dump_json(indent=2))  # full structured report
```

Or with the convenience function:

```python
from quality_checker import check_quality
report = check_quality(text, document_id="d1", source_tool="tika", attempt_id="a1")
```

Each filter is also usable on its own (independently testable):

```python
from quality_checker.filters import TextHealthFilter, CompletenessFilter, ReadingOrderFilter

result = TextHealthFilter().evaluate(text)
result = CompletenessFilter().evaluate(text)   # -> CompletenessFilterResult
result = ReadingOrderFilter().evaluate(text)
```

Invalid input (e.g. `None`, bytes, a list) is handled safely: every filter
returns a score of `0.0` with an `INVALID_INPUT` flag instead of raising.
The original extracted text is never mutated; normalisation, where needed,
happens on a separate copy.

## Result models (`quality_checker/models.py`)

```python
class FilterResult(BaseModel):
    filter_name: str
    score: float            # 0–100, clamped and rounded
    flags: list[str]        # machine-readable, e.g. "EXCESSIVE_REPLACEMENT_CHARS"
    reasons: list[str]      # human-readable explanations
    metrics: dict[str, Any] # raw metrics behind the score

class CompletenessFilterResult(FilterResult):
    detected_sections: list[str]
    missing_sections: list[str]
    missing_required: list[str]
    missing_recommended: list[str]
    missing_optional: list[str]

class QualityReport(BaseModel):
    document_id: str
    source_tool: str
    attempt_id: str
    text_health: FilterResult
    completeness: CompletenessFilterResult
    reading_order: FilterResult
```

## Configuration

All thresholds, penalties, section aliases and importance levels live in
YAML — nothing important is hardcoded in the filter classes:

| File | Controls |
|---|---|
| `quality_checker/config/text_health.yaml` | min/good char & word counts, warn/max ratios for replacement chars, non-printables, whitespace, symbols, repeated-char runs, and the max penalty per check |
| `quality_checker/config/completeness.yaml` | section definitions: display name, importance, aliases, optional regex `patterns`, and penalties by importance (required 25 / recommended 12 / optional 3) |
| `quality_checker/config/reading_order.yaml` | short-line, fragmentation, line-length-variation, broken-sentence and repeated-line thresholds and penalties |

Penalties scale linearly between a *warn* threshold (0 points) and a *max*
threshold (full penalty), so borderline text is graded, not cliff-edged.

Custom configuration can be injected per filter or per checker, which is
how the tests exercise configurability:

```python
checker = QualityChecker(completeness_config=my_dict)
# or: QualityChecker(completeness_config_path="path/to/completeness.yaml")
```

## Project structure

```
quality_checker/
├── __init__.py            # public API: QualityChecker, check_quality, models
├── checker.py             # orchestrator — runs the 3 filters, builds the report
├── models.py              # FilterResult, CompletenessFilterResult, QualityReport
├── config/
│   ├── text_health.yaml
│   ├── completeness.yaml
│   └── reading_order.yaml
├── filters/
│   ├── __init__.py
│   ├── _config.py         # YAML loading + scaled-penalty helper
│   ├── text_health.py
│   ├── completeness.py
│   └── reading_order.py
└── tests/
    ├── conftest.py        # synthetic test data (no external files / network)
    ├── test_text_health.py
    ├── test_completeness.py
    ├── test_reading_order.py
    └── test_checker.py
requirements.txt
README.md
```

## Setup & running tests

Requires Python 3.11+ (developed and tested on Python 3.12).

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest quality_checker/tests -v
```

## Test results

Test suite run before packaging (Python 3.12.3, pydantic 2.14.2, pytest 9.2.0):

```
55 passed in 0.41s
```

Coverage of the required cases:

- **Text Health (17 tests):** empty text, whitespace-only text, normal clean
  resume (score ≥ 80, no flags), corrupted text (`�` × 120), excessive
  symbols, excessive whitespace, non-printable characters, repeated
  characters, very short text, invalid input (`None`/int/float/list/dict/bytes),
  model structure, original text not mutated.
- **Completeness (14 tests):** empty text, common section headings detected,
  some sections missing (score > 0, reasons state it is not proof of failure),
  alternative names (`Technical Skills`, `Professional Experience`,
  `Academic Background`, `Employment History`), contact detected from an
  email without a heading, optional sections costing less than required,
  custom configuration, invalid input, model structure.
- **Reading Order (13 tests):** empty text, normal resume, fragmented
  one-word lines, repeated adjacent lines, broken-sentence clues,
  non-adjacent repeats not penalised, invalid input, model structure.
- **Checker (13 tests):** report structure & metadata, each filter
  independently accessible, **no overall score produced**, normal resume
  scoring high on all three filters, empty text, invalid input producing a
  report (not an exception), original text not mutated, JSON round-trip,
  convenience function, filters operating independently.

Sanity check on the synthetic normal resume used in the tests:
Text Health **100.0**, Completeness **100.0** (7/7 sections detected),
Reading Order **100.0**.

## Logging & error handling

Each module logs through the standard `logging` package (the package itself
adds only a `NullHandler`). The checker logs start/finish with the document
and attempt IDs and the three scores. Missing or invalid YAML raises with a
logged error at construction time; invalid document input never raises —
it returns zero-score `INVALID_INPUT` results.
