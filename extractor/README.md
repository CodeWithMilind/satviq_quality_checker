# SATVIQ Modular Document Extractor Engine

A complete, modular Python document extraction engine built for the
SATVIQ Resume Quality Checker project. It extracts text from resumes
and other documents through four extractors behind **one common
interface and one standard result model**, with a configurable
fallback manager on top.

Default extraction order:

```
1. Apache Tika   (primary, general-purpose)
2. PyMuPDF       (fast PDF embedded text)
3. Docling       (structure-preserving parsing)
4. OCR           (final fallback, Tesseract)
```

This package is **standalone**. It does not import, recreate, or modify
the separate `quality_checker` package — it is designed so the Quality
Checker can be plugged in later through the manager's acceptance-policy
extension point (see *Future Quality Checker integration* below).
It contains no LLM, RAG, vector database, embeddings, resume matching,
web API, or dashboard.

## Folder structure

```
extractor/
├── __init__.py          # public API
├── base.py              # BaseExtractor — the common interface
├── manager.py           # ExtractionManager + BasicAcceptancePolicy
├── models.py            # ExtractionResult, AttemptRecord (Pydantic v2)
├── exceptions.py        # ExtractorError hierarchy
├── extractors/
│   ├── __init__.py
│   ├── tika.py          # Apache Tika
│   ├── pymupdf.py       # PyMuPDF
│   ├── docling.py       # Docling
│   └── ocr.py           # Tesseract OCR
├── config/
│   └── extractor.yaml   # order, enabled flags, timeouts, Tika URL, OCR lang/DPI, acceptance
├── tests/
│   ├── __init__.py
│   ├── test_tika.py
│   ├── test_pymupdf.py
│   ├── test_docling.py
│   ├── test_ocr.py
│   └── test_manager.py
├── requirements.txt
└── README.md
```

## Installation

Python 3.11+ is required.

```bash
python -m venv .venv
.venv/bin/pip install -r extractor/requirements.txt
```

External software is **never installed automatically** by this package:

- **Apache Tika** runs as a Java subprocess on the local
  `tika-app-4.2.0.jar` (Java 17+ required). Point `jar_path` in the
  config — or the `TIKA_JAR_PATH` environment variable — at the JAR;
  the Windows default is `C:\Apache\Tika\tika-app-4.2.0.jar`. There is
  no Tika server and no Python tika client.
- **Tesseract OCR** needs the `tesseract` binary on your PATH (or set
  `tesseract_cmd` in the config).
- **Docling** may download its ML models on first use — that is
  Docling's own behaviour, triggered only when the Docling extractor
  actually runs.

Every extractor imports its library lazily. If a library, server, or
binary is missing, that extractor returns `success=False` with a precise
“unavailable / not installed” error instead of breaking the package —
and OCR is **never** reported as available when Tesseract is missing.

## The standard result

Every extractor and the manager return the same Pydantic model:

```python
class ExtractionResult(BaseModel):
    text: str = ""
    extractor_name: str
    success: bool
    file_type: str | None = None        # e.g. "pdf"
    metadata: dict[str, Any] = {}
    page_count: int | None = None
    page_texts: list[str] = []          # per-page text, in page order
    warnings: list[str] = []
    error: str | None = None            # set whenever success is False
    duration_ms: float = 0.0
    attempts: list[AttemptRecord] = []  # populated by the manager
```

Because failures always carry an `error` and `success=False`, an
extraction **failure** is clearly distinguishable from a **successful
extraction of an empty document** (`success=True`, empty `text`).

## The extractors

### Apache Tika — `extractors/tika.py`
Primary extractor: runs the local Apache Tika 4.2.0 app JAR as a Java
subprocess (`java -jar tika-app-4.2.0.jar --text <file>`; JAR path
and timeout configurable, Java/JAR availability validated per
attempt). Supports PDF, DOCX, DOC, PPTX, XLSX, TXT and everything
else Tika supports, and returns plain text plus basic metadata
(JAR path + version). CLI errors, timeouts and empty results are
returned as failures with reasons — an empty Tika result is never a
silent empty success.

### PyMuPDF — `extractors/pymupdf.py`
PDF-only embedded-text extraction. Preserves page boundaries:
`page_texts[i]` is page *i*'s text in reading order. Encrypted PDFs
fail cleanly unless a `password` is configured; malformed PDFs fail
with the parse error. A PDF whose total embedded text is below
`min_meaningful_text_chars` is reported as *no meaningful embedded
text* (likely scanned), so the manager can fall through to Docling/OCR.
No OCR is performed inside this extractor.

### Docling — `extractors/docling.py`
Structure-preserving parsing via Docling for PDF, DOCX, PPTX, XLSX,
HTML, Markdown and images (as supported by the installed Docling
version). Text is exported from the converted document; page
information is included where the conversion exposes it. Options
(`do_ocr`, `do_table_structure`) are explicit in the config and recorded
in the result metadata. Model-loading and conversion failures return
`success=False` with the underlying error.

### OCR — `extractors/ocr.py`
Final fallback, using Tesseract via `pytesseract`. Scanned PDFs are
rendered page-by-page to images with PyMuPDF at the configured `dpi`
(default 300) and OCR'd per page; image files (PNG/JPG/TIFF/BMP/WebP)
are OCR'd directly. Language is configurable (`language`, e.g.
`"eng+hin"`). Missing Tesseract → `success=False` stating OCR is
unavailable. OCR that recognises no text is a failure, not an empty
success.

## Using an individual extractor

```python
from extractor.extractors import PyMuPDFExtractor, TikaExtractor

result = PyMuPDFExtractor().extract("resume.pdf")
if result.success:
    print(result.text)
    print(result.page_texts)   # per-page text
else:
    print("Failed:", result.error)

tika = TikaExtractor(config={"jar_path": r"C:\Apache\Tika\tika-app-4.2.0.jar",
                             "timeout_seconds": 120})
result = tika.extract("resume.docx")
```

All individual extractors accept the same per-extractor config keys as
`config/extractor.yaml` and never raise for file/library failures —
check `result.success` / `result.error`.

## Using the manager

```python
from extractor import ExtractionManager

manager = ExtractionManager()   # loads extractor/config/extractor.yaml
result = manager.extract("resume.pdf")

if result.success:
    print("Extracted by:", result.extractor_name)
    print(result.text)
else:
    print("All extractors failed:", result.error)

# Full audit trail — every attempt and its failure/rejection reason:
for attempt in result.attempts:
    print(attempt.extractor_name, attempt.success, attempt.accepted, attempt.error)
```

The manager:

- runs extractors **sequentially** in the configured order;
- captures exceptions and failure results without crashing, and
  continues to the next extractor (“fallback initiated” is logged);
- returns the first result the **acceptance policy** accepts — fallback
  is not driven by exceptions alone: a successful-but-meaningless
  extraction (e.g. a few characters) is rejected by the policy and the
  next extractor is tried, with the rejection reason recorded;
- records every attempt in `result.attempts`, and if all fail, the
  final `error` summarises **every** failure reason (nothing is
  silently discarded);
- deduplicates the configured order, honours `max_attempts` and
  the configurable extraction safety budget
  (`max_total_timeout_seconds`, a stuck-process guard);
- skips disabled extractors.

### Configuring fallback

Edit `extractor/config/extractor.yaml`, or override programmatically
(overrides are deep-merged over the YAML):

```python
manager = ExtractionManager(config={
    "manager": {"order": ["pymupdf", "ocr"], "max_attempts": 2},
    "extractors": {"tika": {"enabled": False},
                   "ocr": {"language": "eng+hin", "dpi": 200}},
    "acceptance": {"min_text_chars": 100, "min_word_count": 20},
})
```

Or point at your own file: `ExtractionManager(config_path="my.yaml")`.
Custom extractors / test doubles can be injected with
`ExtractionManager(extractors={...})`, and a custom policy with
`acceptance_policy=...`.

## Future Quality Checker integration

The manager's acceptance step is a clean extension point. A policy is
any object with:

```python
def accept(self, result: ExtractionResult) -> bool: ...
def describe_rejection(self, result: ExtractionResult) -> str: ...
```

Today the default `BasicAcceptancePolicy` implements that with
extraction success + meaningful-text thresholds. Later, a
quality-aware policy can implement the same two methods by internally
calling the existing Quality Checker — conceptually:

```python
result = manager.extract(file_path)          # this package

# Future integration (NOT implemented in this package):
#   quality_report = quality_checker.check(result.text, ...)
#   if quality_policy.accept(quality_report):
#       return result
#   # otherwise the manager continues with the next extractor
```

No Quality Checker code lives in, or is imported by, this package.

## Logging

Standard Python `logging` is used throughout (the package adds only a
`NullHandler`; configure handlers in your application). Logged events:
file received, extractor started / completed / failed, fallback
initiated, and the final extraction result. Document contents and
personal information are never logged — only file names, extractor
names, character counts, durations, and error messages.

## Running the tests

```bash
.venv/bin/python -m pytest extractor/tests -v
```

The unit tests do **not** require a Tika server, a Docling
installation, or a Tesseract binary: manager tests use mocked
extractors, Tika/Docling/OCR tests use the extractors' dependency
seams, and PyMuPDF tests generate real PDFs in temporary directories.

### Test results

Run before packaging (Python 3.12.3, pydantic 2.14.2, pytest 9.2.0,
with PyMuPDF and Pillow installed; Tika, Docling and Tesseract
deliberately absent):

```
45 passed in 0.61s
```

Coverage includes: successful extraction (all four extractors),
empty extraction, invalid file path, unsupported file type, extractor
exceptions, fallback execution (after failure, after exception, and
after policy rejection), correct extractor order (default from YAML
and custom), configuration overrides, extraction result structure,
all extractors failing (with every reason preserved), duplicate
attempts prevented, max-attempts and total-timeout limits, disabled
extractors skipped, encrypted / malformed / scanned PDFs, OCR
unavailable-when-Tesseract-missing, and the Quality Checker policy
extension point.

End-to-end sanity check through the real manager (PyMuPDF only):
a generated text PDF was accepted (`success=True`, extractor
`pymupdf`); a blank scanned-style PDF correctly failed with all
attempt reasons recorded; a missing file failed cleanly.

## Limitations

- Extraction quality is bounded by the underlying tools: Tika needs
  its server running, Docling's output depends on the installed
  Docling version and its models, and OCR accuracy depends on scan
  quality, DPI, and language data installed for Tesseract.
- PyMuPDF's “reading order” is the PDF's embedded text order; complex
  multi-column layouts may need Docling for better structure.
- The basic acceptance policy checks only success + text quantity. It
  is not a quality judgement — that is the future Quality Checker's
  role, via the policy extension point described above.
- Per-extractor `timeout_seconds` is enforced where the underlying
  library supports a timeout (e.g. Tika requests); for in-process
  libraries (PyMuPDF, Docling, OCR) the manager's *total* timeout
  budget is the hard bound between attempts.
- Encrypted documents are only opened when a password is configured
  (PyMuPDF). Tika/Docling behaviour with encrypted files depends on
  those tools.
