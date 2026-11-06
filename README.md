# SATVIQ — Resume Intelligence

A web GUI for the SATVIQ Resume Extraction & Quality Checker pipeline.
Upload resumes in the browser, watch them run through the real
extraction pipeline (**Tika → PyMuPDF → Docling → OCR**, with the
**Quality Checker** — Text Health, Completeness, Reading Order —
gating every attempt), and inspect extracted text, per-filter quality
results, attempt history, and processing history.

**New: per-resume pipeline trace.** Every resume now carries its own
actual execution trace — which extractors were attempted, in what
order, which failed and why, which outputs the Quality Checker
rejected and why, which extractors were skipped, and which extractor
produced the final accepted text. See the dedicated section below.

The GUI and API are a thin integration layer. The existing
`extractor/` and `quality_checker/` packages are used exactly as
provided — their internal logic was not rewritten or modified (the
only change to a pre-existing file is `run_extractor.py` gaining an
optional filename argument; its default behaviour is unchanged).
Per-package documentation remains in `extractor/README.md` and
`quality_checker/README.md`.

## How it works

```
Browser (frontend/ — HTML/CSS/vanilla JS)
        |  HTTP / JSON
FastAPI (api/main.py)
        |
api/pipeline.py — integration layer
        |-- MIME type detection (magic bytes + extension)
        |-- extractor.ExtractionManager, with api.pipeline.QualityGatePolicy
        |     as its acceptance policy:
        |       attempt -> extract -> QualityChecker.check(text)
        |       -> PASS: accept and return the result
        |       -> WARNING: hold the result as a candidate, try the next extractor
        |       -> FAIL: reject, manager tries the next fallback extractor
        |     if nothing is accepted outright, the manager asks the gate
        |     for its best candidate and returns that instead
        |-- records persisted as JSON in results/ (+ originals in uploads/)
```

Overall quality status is derived from the real filter scores by the
integration policy (documented in `api/pipeline.py`), and — since this
update — that status **actually drives fallback decisions**:

- **PASS** — all three filters ≥ 75: accept immediately, stop fallback.
- **WARNING** — any filter < 75 (but Text Health and Reading Order
  ≥ 50): the result is held as a **candidate** and the next extractor
  is tried, when the warning is significant under the configured
  policy (lowest filter score < `warning_continue_below`, default 75 —
  so any WARNING continues; a WARNING at/above the threshold is
  accepted immediately).
- **FAIL** — extraction failed, or Text Health < 50, or Reading
  Order < 50: reject and try the next extractor.

If no extractor is accepted outright, the **best candidate** is
returned instead of a failure: candidates (each keeping its own text
and quality report) are compared by highest weakest-filter score,
then highest average of the three scores, then earliest in the
configured order — never simply the last extractor tried. Thresholds
are configurable under `quality_gate:` in
`extractor/config/extractor.yaml` (`fail_below`, `warn_below`,
`warning_continue_below`) and are exposed at `/api/health`.

> Behaviour fix in this update: previously the gate accepted any
> non-FAIL result (`status != "FAIL"`), so a WARNING from Tika stopped
> the pipeline and no other extractor was ever tried. The gate now
> returns *continue* for significant WARNINGs, and the
> ExtractionManager gained a generic optional policy hook,
> `best_candidate_result()`, through which the gate nominates the
> best candidate at the end. Policies without that method (e.g. the
> extractor package's `BasicAcceptancePolicy`) behave exactly as
> before, and the configured extractor order is unchanged.

A missing resume section can never by itself cause FAIL (Completeness
is not a FAIL criterion), matching the Quality Checker's own policy
that a missing section is a possible omission, not proof of failure —
a sections-incomplete extraction is a WARNING candidate, not a
rejection.

## Interactive per-resume flowchart (View Processing Flow)

Every resume card (Processing Results), every history row (View
Flow), and the detail view render the record's backend
`pipeline_trace` as an interactive vertical flowchart:

- One node per actual stage — Resume Upload → MIME Detection → for
  each extractor in the configured order, the extractor node plus
  its Quality Checker node → Final Extraction Result — coloured by
  the real status (green PASS, red FAIL, orange WARNING/REJECTED,
  gray SKIPPED, blue RUNNING) with the real duration when recorded
  and a SELECTED marker on the attempt that produced the final text.
- Labelled arrows wherever the quality gate's decision explains the
  route: *accepted — final result*, *candidate held — trying next
  extractor*, *rejected — trying next extractor*, *extraction
  failed — trying next extractor*, *no outright acceptance — best
  candidate selected*, *all extractors failed*.
- Clicking any node expands its real details: for a Quality Checker
  node, the three filter scores, flags, and the Completeness
  filter's detected/missing sections exactly as reported; for a
  failed extractor, the real error; for the final node, the
  selection reason. Nothing is simulated in JavaScript — the
  flowchart only renders the backend trace.

> Why a trace section could appear empty while the attempts table
> had data (audit of this build): the trace data itself was complete
> at every step — generated by the backend, returned by POST/list/
> detail responses, and persisted in the JSON records (verified
> against real stored records) — but the frontend was delivered as
> unversioned `js/app.js` / `css/style.css` with no cache headers,
> so after replacing the project with a newer ZIP a browser could
> keep running a cached older script that never populated the trace
> containers. Fixed by versioned asset URLs (`?v=5` in
> `frontend/index.html` — bump it on future frontend changes), a
> `Cache-Control: no-cache` middleware for frontend responses in
> `api/main.py`, and the history flow now rendering from the record
> data already present in the history list response if the detail
> request fails. If you still see an old UI after updating, hard
> refresh once (Ctrl+F5).

## Per-resume pipeline trace & extractor transparency

Every processed resume's record now includes, in addition to the
existing fields:

- `primary_extractor` — the first extractor attempted (the head of
  the configured order, normally `tika`).
- `extractor_used` — the extractor that produced the final accepted
  text (unchanged field; the history table's *Final Extractor*).
- `attempt_count`, `fallback_occurred`, `fallback_reason` — the first
  non-accepted attempt's real failure/rejection reason when a fallback
  happened (`null` when the first attempt was accepted).
- `attempts[]` — each attempt also carries `quality_status`, the full
  per-attempt `quality` report (the three real filter results) when
  the Quality Checker ran on that attempt, and the gate's decision:
  `decision` (`accept` / `continue` / `reject`), `decision_reason`
  (why fallback stopped, continued, or rejected), and `selected`
  (whether this attempt produced the final result).
- `selection_reason` — why the final extractor was chosen: the
  acceptance reason for an outright PASS, or the best-candidate
  comparison (every candidate's weakest/average scores) when the
  final result was nominated at the end.
- `pipeline_trace[]` — the structured execution trace: `Resume Upload`
  → `MIME Type Detection` → for each extractor in the configured
  order, an extractor stage plus its `Quality Checker` stage (or a
  `SKIPPED` stage when the extractor was never attempted) → `Final
  Extraction Result`. Stage statuses are `PASS` / `WARNING` (the
  gate's real per-attempt quality status — a WARNING Quality Checker
  stage means the result was held as a candidate and fallback
  continued), `FAIL` (extraction failed, with the actual error),
  `REJECTED` (the Quality Checker refused the text, with the actual
  rejection reason and scores), and `SKIPPED`. Stages also carry the
  gate `decision` and a `selected` flag; durations are the attempts'
  recorded `duration_ms`.

The trace is built in `api/pipeline.py` (`build_pipeline_trace` /
`build_record_from_result`) **only** from the ExtractionManager's real
attempt history and the gate's real per-attempt quality reports —
nothing is simulated in the frontend. Records stored by older versions
(which have attempts and the final quality report but no trace) get
their trace derived from that stored data on read
(`ensure_pipeline_trace`, applied in `api/store.py`); per-attempt
quality detail is attached only for the extractor the stored report
actually belongs to. Re-uploading the same file always creates a new,
separate record/run — earlier runs are never overwritten.

In the GUI: each Processing Results card and each Resume History row
shows Primary Extractor, Final Extractor, Attempts, per-attempt
ACCEPTED / REJECTED / FAILED lines with reasons, and a **View
Processing Pipeline** toggle that expands the vertical timeline for
that resume only. The history table additionally has Primary
Extractor, Final Extractor and Attempts columns and a *View Pipeline*
action (expandable row). The global Processing Pipeline section
remains as the configured-order overview and is labelled as such.

### Why different runs of the same file can use different extractors

This is real backend behaviour, visible in the trace — the configured
order never changes. Example (a `.docx` resume): the Tika extractor
runs the local Apache Tika 4.2.0 app JAR via Java subprocess
(`extractor/config/extractor.yaml` → `tika.jar_path`, or the
`TIKA_JAR_PATH` environment variable; Windows default
`C:\Apache\Tika\tika-app-4.2.0.jar`). If Java or the JAR is missing
when a run starts, the Tika attempt FAILs with that precise reason;
PyMuPDF cannot take over for `.docx` (it supports PDF only), so the
run falls back to **Docling**, which supports `.docx`. On a later run
with Tika available, **Tika** succeeds on the first attempt and the
rest are SKIPPED. Both runs can legitimately end `Success / Quality: PASS`
with different final extractors — the per-resume trace shows exactly
which path each run took and why.

## Project structure

```
satviq_resume_quality_checker/
├── extractor/            # existing extraction engine (unchanged)
├── quality_checker/      # existing quality checker (unchanged)
├── frontend/
│   ├── index.html
│   ├── css/style.css
│   ├── js/api.js         # central API layer (all backend calls)
│   ├── js/app.js         # pages: Process / History / Inspector / Settings
│   └── assets/
├── api/
│   ├── main.py           # FastAPI app + endpoints + static frontend
│   ├── pipeline.py       # integration: manager + QualityChecker gate, MIME detection
│   └── store.py          # local JSON persistence for history/statistics
├── tests/
│   ├── test_api.py            # API integration tests (TestClient)
│   ├── test_pipeline_trace.py # per-resume trace / history tests
│   ├── test_quality_gate.py   # quality-gate candidate policy tests
│   ├── test_trace_audit.py    # trace delivery-chain + cache regression tests
│   ├── test_flowchart.py      # attempt-card rendering tests (backend + Node frontend)
│   └── test_production_ui_api.py # 5 MB limit, history paging, prompt settings
├── uploads/              # uploaded originals (kept until their record is deleted)
├── results/              # one JSON record per processed resume
├── run_extractor.py      # original CLI extraction demo (optional file argument)
├── sample_resume.pdf     # sample file from the original project
├── requirements.txt
├── run.bat               # Windows startup script
└── README.md
```

## Installation

Python 3.11+ required.

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux / macOS
pip install -r requirements.txt
```

Optional external tools (never installed automatically): **Java 17+
and the Apache Tika 4.2.0 app JAR** (for the Tika extractor),
**Docling** models, and the **Tesseract** binary (for OCR). Without
them, those extractors report themselves unavailable and the pipeline
falls back — in a minimal install, PDFs are extracted by PyMuPDF.

### Apache Tika 4.2.0 — local JAR via Java subprocess

The Tika extractor does **not** use a Tika server or the Python
`tika` client. Each Tika attempt is one short-lived process:

```
java -jar <tika-app-4.2.0.jar> --text <document>
```

`--text` matters: Tika 4's app CLI defaults to Markdown output.
The JAR path is configurable — `extractors.tika.jar_path` in
`extractor/config/extractor.yaml`, else the `TIKA_JAR_PATH` (or
`TIKA_APP_JAR`) environment variable, else the Windows default
`C:\Apache\Tika\tika-app-4.2.0.jar`. Java and JAR availability are
validated per attempt; a missing JAR or Java runtime fails that
attempt with a precise reason and the pipeline falls back.

**Windows (the reference setup):**

1. Install Java 17+ and confirm `java -version`.
2. Keep the Tika 4.2.0 distribution at `C:\Apache\Tika\`
   (`tika-app-4.2.0.jar` with `lib/` and `plugins/` beside it — the
   4.x app JAR is a launcher and needs its `lib/` folder).
3. Nothing else to configure: the default JAR path already points
   there. Run the app with `run.bat` as usual.

**Ubuntu VPS deployment:**

```bash
sudo apt install openjdk-21-jre-headless python3-venv
# place the same Tika 4.2.0 distribution on the server, e.g. /opt/tika
export TIKA_JAR_PATH=/opt/tika/tika-app-4.2.0.jar
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Note: each Tika attempt starts a JVM (~seconds). That is the cost of
the subprocess design; the configured per-attempt timeout
(`tika.timeout_seconds`, default 120) covers JVM start + parse.

### Muse AI (.env-only configuration), direct image/PDF fallback, timeouts

**Muse AI is the only LLM provider**, and it is configured ONLY
through the backend `.env` file — there is deliberately NO model
configuration in the frontend (the GUI shows a read-only status
line: "AI Provider: Muse AI · Model: … · Configured / Not
configured", fed by `/api/health`, which never exposes the key).

Setup: copy `.env.example` to `.env` in the project root and set
your key:

```env
MUSE_API_KEY=
MUSE_BASE_URL=https://api.meta.ai/v1
MUSE_MODEL=muse-spark-1.3
MUSE_TIMEOUT_SECONDS=20
MUSE_MAX_OUTPUT_TOKENS=
MUSE_TEMPERATURE=
MUSE_VISION_ENABLED=true
```

`.env` is git-ignored and excluded from distribution ZIPs; real
environment variables take precedence over the file. Settings are
validated at startup (`muse_ai/config.py`): a missing key does
not crash the app — extraction keeps working, `/api/health`
reports the configuration error, and any record whose chain fails
without a PASS gets a preserved Muse attempt recording that
error, so the gap is visible rather than silent. The configured
model is always the model used; nothing substitutes another one.

**Protocol** (verified against https://dev.meta.ai/docs/overview,
the Chat Completions, Image understanding and File handling
guides): `POST {MUSE_BASE_URL}/chat/completions` with Bearer
auth; reply text at `choices[0].message.content`; token usage
recorded from `usage`. Optional parameters are sent only when
set: `MUSE_MAX_OUTPUT_TOKENS` → `max_completion_tokens`,
`MUSE_TEMPERATURE` → `temperature`. The client is
`muse_ai/client.py` (stdlib HTTP behind one seam function); the
pipeline never calls the API directly, and the Quality Checker
never calls Muse at all — it stays 100% rule-based.

**Direct image/PDF extraction (final fallback).** When the
Tika → PyMuPDF → Docling chain finishes without a Quality
Checker PASS and vision is enabled, the ORIGINAL file is sent to
Muse: images as `image_url` content parts (base64 data URLs);
PDFs as a `file` content part carrying the original bytes — the
API converts PDFs server-side (documented coverage: text from
the first 100 pages, per-page images for the first 50; a record
notes `pages_beyond_image_limit` when a PDF is longer). The
50 MB inline limit is enforced with a clear error. The returned
Resume JSON is validated against `muse_ai/schema.py`, recorded
as its own preserved attempt (engine `Muse AI`, model, input
type text/image/pdf, duration, configured timeout, JSON
validation status, token usage), and becomes the final result
on success. Set `MUSE_VISION_ENABLED=false` to disable this
stage; the disabled state is recorded the same way.

**Resume JSON generation.** When Muse is configured and the
chain produced accepted text, the final text is converted to the
same schema and shown in the detail view ("Resume JSON" panel).
Failures are recorded in `resume_json_meta` and never fabricate
JSON (verified live: a dummy key produced an honest HTTP 401
`invalid_api_key` from api.meta.ai, recorded — nothing invented).

**Combined sources for the Muse extraction stage.** The direct
image/PDF request now carries THREE sources in one call: (1) the
original document (image part / PDF file part), (2) the **Tika
attempt's extracted text** as supporting context, and (3) the
PDF's **hyperlink annotations**, extracted deterministically by
PyMuPDF (`api/hyperlinks.py`) — clickable URLs live in the PDF
annotation layer and never appear in extracted text, which is
why links were previously missing. The developer instructions
encode the reconciliation priority: visual document first, Tika
text supporting (never copied over clear visual evidence),
annotations authoritative for URLs (preserved exactly; URLs are
never invented). Each Muse attempt records which sources were
actually sent (`visual_provided`, `tika_text_provided`,
`hyperlinks_provided`, `hyperlink_count`), and every record
carries its `hyperlinks` list (page, URL, visible anchor text,
rect) — including records processed before this feature, which
regain their annotations on read from the stored original.
Extraction failures are recorded in `hyperlink_error` and never
stop the pipeline. The hyperlink annotations are also supplied
to the text → JSON generation stage for the same reason.

**Text normalization (selected extractor → Muse).** A
deterministic module (`api/text_normalization.py` — no LLM)
normalizes the text of the extractor the pipeline actually
SELECTED (not always Tika) before it is sent to Muse: line
endings unified, invisible extraction noise (BOM, zero-width
characters, control characters) removed, non-breaking spaces
regularized, space/tab runs collapsed to one space, lines
trimmed, lines containing only decorative separators removed
(table rows and other structured lines are kept), and 3+ blank
lines collapsed to one blank line — meaningful paragraph breaks
are preserved; this stage deliberately does no token compaction.
Lines are never joined and words never merged; headings, bullets,
dates, phone numbers, emails, URLs and punctuation pass through
unchanged, OCR spelling is never "corrected", and the function
is idempotent. The ORIGINAL selected text stays untouched on its
attempt; each record carries a `text_normalization` block
(source extractor, status applied / bypassed / unavailable,
`original_extracted_text`, the normalized text,
`normalization_applied`, `normalization_steps` — the
transformations actually applied — character counts, ESTIMATED
token counts — a ~4 chars/token heuristic, clearly labelled,
never confused with the API's actual usage — and the estimated
reduction %). Muse receives the normalized text in BOTH
text-consuming stages (vision supporting text — the Tika
attempt's own normalized text — and Resume JSON generation from
the selected text); if normalization ever fails, the original
text is sent unchanged and the bypass + error are recorded on
the record and the Muse attempt. The GUI detail view shows
both versions under "Text Normalization" with Original Extracted
Text / Normalized Text (Sent to Muse) tabs, the source
extractor, the applied steps, and the comparison metrics. Older
records gain the current block on read.

**Token usage monitoring.** Every Muse API call records the
usage the API itself returned (input/output/total tokens, cached
input tokens, request ID, duration) — never an estimate. Per
resume, `token_usage` aggregates its calls (totals, call counts
by outcome, models) and `muse_requests` keeps each request's own
entry; the detail view shows a "Resume Token Usage" panel. The
vision request that also produced the final JSON is counted
once. Context monitoring uses the documented 1,048,576-token
window for muse-spark-1.3 (override with
`MUSE_CONTEXT_WINDOW_TOKENS`): status is computed from the
largest single request's INPUT tokens (Normal / Near ≥80% /
Exceeded), with remaining = window − input. When the API returns
no usage, totals show as Unavailable rather than a fabricated
number.

**Timeouts.** Two independent backend-enforced caps: (1) the
per-attempt Extraction Timeout chosen in the Upload card (15s /
20s, default 20) covers every attempt including Muse stages —
an overrun is recorded as TIMEOUT with configured timeout and
actual duration and the flow continues; (2) Muse HTTP calls
additionally obey `MUSE_TIMEOUT_SECONDS` (default 20) from
`.env`. Note: 15s can clip a cold Tika JVM start on a slow
machine — 20s is the safer default.

**What changed from the previous build:** the frontend AI Model
Settings page, the multi-provider configuration store
(`ai_providers/`, `settings/model_configs.json`) and its CRUD API
were REMOVED at the project owner's instruction — Muse via
`.env` replaces them. The standalone `SATVIQ_AI_Model_Module/`
folder is preserved unchanged; it is not wired into the pipeline.

### OCR — Tesseract inside Tika (scanned PDFs and images)

OCR runs **inside the Tika attempt** — there is no separate OCR
attempt and the Quality Checker still judges the resulting text
exactly as before. When OCR is enabled (the default), each Tika run
passes a generated Tika 4 JSON config (`--config=...`) that
registers Tesseract as Tika's text-recognition engine and sets the
PDF page-text strategy to `auto`: pages with a text layer use it,
pages without one are rendered (300 DPI) and OCR'd. Raster images
(PNG/JPG/TIFF…) are OCR'd directly.

**Requirements:** Apache Tika 4.2.0 (as above) and **Tesseract OCR**
(developed/verified against 5.x; Windows: install to
`C:\Program Files\Tesseract-OCR`, language data `eng`). Verify:

```bat
"C:\Program Files\Tesseract-OCR\tesseract.exe" --version
"C:\Program Files\Tesseract-OCR\tesseract.exe" --list-langs
```

**Configuration** (`extractors.tika` in `extractor/config/
extractor.yaml`): `ocr_enabled`, `ocr_language` (`eng`),
`ocr_strategy` (`auto` | `ocr_only` | `ocr_and_text` | `no_ocr`),
`ocr_dpi`, `ocr_timeout_seconds`, `tesseract_path` (the executable
or its folder; else `TESSERACT_PATH` env → PATH → the Windows
default folder), `tessdata_path` (else `TESSDATA_PATH`), and
`tika_config_path` (use your own Tika JSON config as-is — e.g. the
`tika-config.json` of a Tika Server install; a filled example ships
at `extractor/config/tika-config.example.json`).

**Test OCR with a scanned PDF:** take any PDF with no text layer
(select-all in a viewer copies nothing), upload it, and open the
result: the Tika attempt shows the OCR'd text. Standalone images
show **OCR: used** on the attempt; for PDFs the attempt records
that OCR was *available* with the configured strategy — the
`--text` CLI output carries no per-page OCR marker, so per-page
use is honestly reported as not-evidenced rather than claimed.

**Three Tika 4 config traps** (all found by testing against the
real 4.2.0, all handled by the generated config):

1. `tesseractPath` in Tika config is the **directory** containing
   the executable — pointing it at `tesseract.exe` itself fails
   config loading ("doesn't point to an existing directory").
2. Once `tesseractPath` is set, Tesseract looks for language data
   in `<tesseractPath>\tessdata` unless `tessdataPath` is also set
   — a mismatch makes OCR fail hard and takes the whole extraction
   down. The extractor resolves and pairs both paths, and leaves
   `tesseractPath` unset (Tesseract's own defaults) when the data
   directory cannot be located.
3. The config's `text-recognizers` key is required for image OCR,
   and the `parsers` section must keep an (empty) `default-parser`
   entry — a `parsers` section listing only `pdf-parser` silently
   returns empty text for standalone images.

**Troubleshooting OCR:**
- *Attempt warning "Tesseract was not found"* — install Tesseract
  or set `tesseract_path` / `TESSERACT_PATH`; the pipeline continues
  without OCR (structured fallback, no crash).
- *"Error opening data file …\tessdata\eng.traineddata"* — the
  tessdata mismatch of trap 2: set `tessdata_path` to the folder
  containing `eng.traineddata`.
- *Scanned PDF returns no text but images OCR fine* — check the
  strategy is not `no_ocr`, and raise `ocr_dpi` for low-quality
  scans.
- *A Tika Server is also installed* — this project does not call
  it; the same JSON config format applies there (start it with
  `java -jar tika-server-standard-4.2.0.jar -c tika-config.json`
  if you use the server for other tools).

### Every attempt's output is preserved

Each record's `attempts` array stores, per attempt: the extractor's
own `extracted_text` (verbatim, even for WARNING/FAIL attempts),
its full Quality Checker report, error type/message/traceback,
start/end timestamps, duration, extractor version, the gate's
fallback decision + reason, and whether it was finally selected.
Top-level `final_extracted_text` / `final_quality_status` mirror the
selected attempt. In the GUI, the detail view shows one expandable
section per attempt (**View Extracted Text / View Quality Report /
View Error**), and flowchart extractor nodes expose their attempt's
text. Reopening a stored resume shows all of it without re-running
extraction. (Records stored by older builds regain the final text on
their selected attempt on read; older attempts' texts were never
persisted by those builds and stay empty.)

## Starting the application

**Windows:** double-click `run.bat` (it activates `.venv`/`venv` if
present and starts the server), then open **http://127.0.0.1:8000**.

**Any platform:**

```bash
uvicorn api.main:app --host 127.0.0.1 --port 8000
# or: python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Run it from the project root so the `extractor`, `quality_checker`,
`api` and `frontend` packages resolve correctly.

**Muse AI setup (optional, backend only):** copy `.env.example`
to `.env` in the project root and put your Meta Model API key in
`MUSE_API_KEY`. Without it the app still runs — extraction works
and Muse stages report the missing configuration. See the
"Muse AI (.env-only configuration)" section above.

## Using the GUI — production UI (current build)

The UI is a clean, light, four-page application (hash-routed, no
build step). Every value shown comes from the backend API — there is
no sample or hardcoded data anywhere in the frontend.

1. **Process Resume** (`#/process`): drag & drop or browse (multiple
   files supported). Each selected file shows its name, size and type
   and is validated in the browser before upload — maximum **5 MB**
   per file (read from `/api/health`, enforced again on the backend)
   and a supported type. Invalid files are marked with the exact
   reason and cannot be processed. Upload progress is shown; after
   processing, each resume gets a compact result card: filename, file
   size, status, **Quality Score /100**, input/output/total LLM
   tokens, duration and attempt count, with a three-dot menu (view
   texts / AI output / quality report / attempts, download JSON /
   original file / extracted text, delete).
2. **History** (`#/history`): previously processed resumes loaded
   page by page from the backend (server-side pagination, filename
   search and status filter — the full history is never loaded at
   once). Clicking a card opens the inspector; the three-dot menu
   offers the same actions as on result cards.
3. **Resume Inspector** (`#/resume/<id>`): a full page per resume —
   header metrics (quality score, total tokens, duration, attempts,
   pages), three tabs (**Extracted Text** — the original extractor
   text exactly as stored, with copy, length, and an optional
   side-by-side comparison with the normalized text; **Normalized
   Text** — the actual normalized text with its status, source,
   steps and reduction; **AI Output** — the structured Resume JSON
   in a readable layout with a raw JSON view), a **Quality Report**
   (overall score plus the Quality Checker's Text Health /
   Completeness / Reading Order component scores, flags, reasons and
   detected/missing sections), **LLM Token Usage** (aggregate
   input/output/total, provider, model, and per-call rows from
   `muse_requests`), **PDF Hyperlinks** when present, and compact
   **Extraction Attempt** cards (engine, version, status, character
   count, duration, short failure reason) that expand to the full
   text, full error, quality report, configured vs actual duration,
   OCR status and engine metadata.
4. **Settings** (`#/settings`): the LLM provider, model, base URL
   and configuration status, read from the backend (the API key is
   backend-only and never appears in the UI), plus the **system
   prompt editor**: view/edit the prompt used for resume extraction,
   save it (persisted on the backend in `data/prompt_settings.json`
   and applied to new processing requests), reset it to the built-in
   default, or test it — the test runs one real Muse request with
   the edited prompt against a tiny sample and reports the genuine
   validation result, duration and token usage (or reports that the
   backend is not configured; nothing is simulated).

**Quality Score note:** the Quality Checker deliberately reports
component scores only. The headline "Quality Score: XX/100" is a
presentation aggregate — the mean of the component scores the
backend returned for that resume — always shown next to the
components themselves. It is an extraction-quality figure only,
never a candidate-suitability or ranking score. When no component
score exists, the UI shows "N/A" (never 0).

**What the new UI replaced:** the previous single-page dashboard
(statistics cards, pipeline/flowchart visualizations) is retired;
the backend `pipeline_trace` data is unchanged and still stored on
every record. The per-attempt cards in the inspector render the same
real attempt history the flowchart visualized.

## Production deployment

- Backend: run the same FastAPI app under a process manager, e.g.
  `uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers 2`
  (Linux) or the provided `run.bat` for a single Windows host.
  Put a reverse proxy (nginx/Caddy) in front for TLS; the frontend
  is served by the backend itself, same-origin, so no CORS or
  frontend build/deployment is needed.
- Configuration: copy `.env.example` to `.env` on the server and set
  `MUSE_API_KEY` there — secrets live only in the backend
  environment, never in the frontend or the repository.
- Runtime data to persist/back up: `uploads/`, `results/` and
  `data/` (the saved system-prompt override). Everything else is
  source.
- The split-deployment escape hatch: if the UI is ever hosted on a
  different origin, set `window.SATVIQ_API_BASE` before `js/api.js`
  loads; the API layer uses it as the base URL for every call.

## Files changed in the production-UI build

**New**
- `frontend/js/api.js` — central API integration layer.
- `muse_ai/prompt_config.py` (+ identical copy under
  `quality_checker/muse_ai/`) — backend system-prompt persistence.
- `tests/test_production_ui_api.py` — 5 MB limit, history
  search/filter/pagination, original-file download, prompt settings.

**Modified**
- `frontend/index.html`, `frontend/css/style.css`,
  `frontend/js/app.js` — the new four-page UI (assets `?v=14`).
- `api/main.py` — exact 5 MB rejection message; history
  search/filter/pagination on `GET /api/resumes`;
  `GET /api/resumes/{id}/file` (original-file download);
  `GET|PUT /api/settings/system-prompt`,
  `POST /api/settings/system-prompt/reset`,
  `POST /api/settings/system-prompt/test`.
- `api/pipeline.py` — default upload limit is now 5 MB
  (`SATVIQ_MAX_UPLOAD_MB` still overrides).
- `api/store.py` — public records carry `has_original_file` so the
  UI only offers downloads the backend can serve.
- `muse_ai/service.py` (+ identical copy) — Muse stages use the
  saved system prompt when one exists, else the built-in default.
- `.env.example`, `.gitignore`, `README.md`.
- Frontend tests in `tests/test_attempt_outputs.py`,
  `tests/test_flowchart.py`, `tests/test_trace_audit.py`,
  `tests/test_extraction_timeout.py`, `tests/test_token_usage.py`,
  `tests/test_text_normalization.py` — updated to the new UI's
  structure, keeping their original guarantees (real attempt data,
  tokens, texts; no provider/key configuration in the frontend).

## API endpoints

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/health` | Status, extractor order, quality filters, supported extensions, max upload size |
| GET | `/api/stats` | Dashboard statistics + recent activity, computed from stored records |
| POST | `/api/resumes/process` | Multipart upload (`files`, repeatable); processes each file and returns full records |
| GET | `/api/resumes` | History summaries (without full text); optional `q`, `status`, `page`, `page_size` for search/filter/pagination |
| GET | `/api/resumes/{resume_id}` | Full record incl. extracted text and quality report |
| GET | `/api/resumes/{resume_id}/text` | Extracted text as a downloadable `.txt` |
| GET | `/api/resumes/{resume_id}/file` | The originally uploaded file |
| GET | `/api/settings/system-prompt` | Current system prompt, default, custom flag + LLM status (no secrets) |
| PUT | `/api/settings/system-prompt` | Save a custom system prompt (`{"system_prompt": "..."}`) |
| POST | `/api/settings/system-prompt/reset` | Restore the built-in default prompt |
| POST | `/api/settings/system-prompt/test` | One real Muse request with the candidate/saved prompt; reports validation, duration, usage |
| DELETE | `/api/resumes/{resume_id}` | Delete one record and its uploaded file |
| DELETE | `/api/resumes` | Delete all records and uploaded files |

Example response (abridged, real field names):

```json
{
  "results": [{
    "resume_id": "243532c6-…",
    "filename": "sample_resume.pdf",
    "success": true,
    "extractor_used": "pymupdf",
    "primary_extractor": "tika",
    "attempt_count": 2,
    "fallback_occurred": true,
    "fallback_reason": "… (tika attempt's real error)",
    "duration_ms": 165.72,
    "attempts": [
      {"extractor_name": "tika", "success": false, "accepted": false, "error": "…", "quality_status": null, "quality": null},
      {"extractor_name": "pymupdf", "success": true, "accepted": true, "error": null, "quality_status": "PASS", "quality": {"…": "per-attempt report"}}
    ],
    "pipeline_trace": [
      {"stage": "Resume Upload", "type": "upload", "status": "PASS", "duration_ms": null, "message": "…", "extractor": null, "accepted": null, "quality": null},
      {"stage": "MIME Type Detection", "type": "mime", "status": "PASS", "message": "application/pdf", "…": "…"},
      {"stage": "Tika Extractor", "type": "extractor", "extractor": "tika", "status": "FAIL", "message": "… (actual error)", "…": "…"},
      {"stage": "Quality Checker", "type": "quality", "extractor": "tika", "status": "SKIPPED", "…": "…"},
      {"stage": "PyMuPDF Extractor", "type": "extractor", "extractor": "pymupdf", "status": "PASS", "…": "…"},
      {"stage": "Quality Checker", "type": "quality", "extractor": "pymupdf", "status": "PASS", "quality": {"…": "scores"}, "…": "…"},
      {"stage": "Docling Extractor", "type": "extractor", "extractor": "docling", "status": "SKIPPED", "…": "…"},
      {"stage": "OCR Extractor", "type": "extractor", "extractor": "ocr", "status": "SKIPPED", "…": "…"},
      {"stage": "Final Extraction Result", "type": "final", "status": "PASS", "extractor": "pymupdf", "…": "…"}
    ],
    "extracted_text": "…",
    "quality_status": "PASS",
    "quality": {
      "status": "PASS",
      "text_health": {"filter_name": "text_health", "score": 100.0, "flags": [], "reasons": [], "metrics": {}},
      "completeness": {"filter_name": "completeness", "score": 94.0, "detected_sections": ["…"], "missing_sections": ["…"]},
      "reading_order": {"filter_name": "reading_order", "score": 100.0, "flags": [], "reasons": [], "metrics": {}}
    },
    "error": null
  }]
}
```

## Security & reliability

- Extensions validated against the backend-supported set and file
  size capped while streaming (files are never loaded whole into
  memory or executed).
- Filenames are sanitised to a safe basename (path traversal removed);
  stored names are server-generated (UUID + sanitised name).
- Internal storage keys/paths are stripped from every API response;
  errors return a generic message without tracebacks or server paths.
- Empty and corrupt files produce clean failure records (not crashes).
- Processing runs in a threadpool so the UI stays responsive.
- **Cleanup policy:** an uploaded original and its JSON record are
  kept until you delete that record (or Delete All) from the GUI/API.
  Nothing else is deleted automatically.

## Testing

```bash
python -m pytest tests/ -v                      # API + pipeline-trace tests
python -m pytest quality_checker/tests extractor/tests -q   # existing suites (unchanged)
```

Results from the verification run for this delivery (Python 3.12.3,
FastAPI 0.162.0, pydantic 2.14.2, pytest 9.2.0; PyMuPDF 1.28.2 +
Pillow installed; no Tika JAR/Java, Docling, or Tesseract in
that environment):

```
tests/test_api.py:            12 passed
tests/test_pipeline_trace.py: 12 passed
tests/test_quality_gate.py:   12 passed
tests/test_trace_audit.py:     7 passed
tests/test_flowchart.py:       6 passed
tests/test_attempt_outputs.py: 8 passed
tests/test_muse_config.py:     6 passed
tests/test_muse_client.py:    10 passed
tests/test_muse_combined.py:   6 passed
tests/test_hyperlinks.py:      7 passed
tests/test_token_usage.py:     7 passed
tests/test_text_normalization.py: 29 passed
tests/test_vision_fallback.py: 6 passed
tests/test_extraction_timeout.py: 6 passed
tests/test_tika_ocr_integration.py: 4 passed locally with a real
                        Tika 4.2.0 JAR + Tesseract (env-gated: skipped
                        where TIKA_JAR_PATH/Java/Tesseract are absent)
existing suites:              111 passed  (55 quality_checker + 56 extractor)
total:                        245 passed, 4 skipped (integration, env-gated)
```

`tests/test_attempt_outputs.py` verifies that every attempt's own
extracted text, quality report, error and fallback decision survive
the run, the JSON storage round-trip, and the API — and that the
frontend renders a separate expandable section per attempt.
`extractor/tests/test_tika.py` covers the Tika 4.2.0 CLI extractor
with mocked subprocess execution (JAR missing, Java missing, CLI
failure, timeout, empty output, env/config JAR resolution).

`tests/test_quality_gate.py` covers the gate decisions with measured
real-Quality-Checker scores: PASS accepts and stops; WARNING is held
as a candidate and the next extractor is tried, then a PASS wins;
WARNING below the configured significance threshold is accepted
immediately; FAIL is rejected (and never becomes a candidate);
multiple-candidate comparison in both directions (best candidate
first and last) plus a three-candidate case proving weakest-link
ranking over average; candidate text/report preservation; missing
sections alone yielding a WARNING candidate rather than a failure;
and the no-candidate all-fail outcome.

`tests/test_pipeline_trace.py` covers the trace feature end to end:
(1) first extractor accepted immediately (later extractors SKIPPED),
(2) extractor failure → fallback, (3) Quality Checker rejection →
fallback (with the rejected attempt's real scores in the trace),
(4) multiple fallback attempts in real order, (5) the same resume
uploaded twice = two separate runs, (6) two resumes with different
final extractors, (7) history summaries and detail traces matching
the actual accepted extractor, (8) history refresh consistency,
(9) delete-one, (10) delete-all — plus a real-upload trace/attempts
consistency check, the pre-extraction failure record's trace, and
backfill of traces for pre-trace stored records. The deterministic
scenarios drive fake extractors through the real ExtractionManager +
QualityGatePolicy and the same record builder the API uses.

`tests/test_trace_audit.py` guards the trace delivery chain:
process → POST response → JSON on disk → history list → detail all
carry the identical trace; two resumes have different traces; the
same file uploaded twice stays independent (delete-one keeps the
other's trace); legacy records without a stored trace are backfilled
in both list and detail; the real `app.js` renders a non-empty flow
for a real record in Node; asset URLs are versioned and served with
`no-cache`. `tests/test_flowchart.py` covers the flowchart itself:
backend trace completeness for the accept / failure-fallback /
quality-rejection / WARNING-candidate / all-attempted scenarios, and
Node-rendered flowchart assertions (one node per stage, decision
edge labels, real scores and errors in node details, node toggle,
independent flows, result-card integration).

The API tests are environment-independent — they pass whether or not
Tika (JAR + Java) / Docling / Tesseract is installed on the machine
running them. They cover: health, frontend serving, PDF upload with
real quality results and attempt-order/acceptance assertions that
hold whether Tika succeeds or the pipeline falls back, a
deterministic fallback test (fake extractors driven through the real
ExtractionManager + real QualityGatePolicy, covering both an outright
extraction failure and a Quality-Checker FAIL rejection), multiple
uploads, unsupported type, empty file, corrupt PDF, history/detail/
text download/statistics, delete + 404s, path-traversal filename
sanitisation, and no internal fields in responses. The frontend JS
was syntax-checked with Node.

> Follow-up fix: the original upload test asserted Tika's attempt was
> *not* accepted, which only holds when Tika is unavailable.
> On a machine where Tika actually works, Tika correctly succeeds as
> the primary extractor and that assertion failed — the application
> behaviour was right, the test's assumption was wrong. Fixed in the
> current `tests/test_api.py`.

Live smoke test (real uvicorn server, not just TestClient), in the
minimal environment above: health OK, frontend served with the new
trace UI. Uploading `sample_resume.pdf` produced this trace —
Tika **FAIL** ("The 'tika' Python package is not installed…") →
Quality Checker SKIPPED → PyMuPDF **PASS** → Quality Checker **PASS**
(overall quality PASS) → Docling SKIPPED → OCR SKIPPED → Final
Extraction Result PASS, final extractor `pymupdf`, attempts 2, with
the Tika error as `fallback_reason`. Uploading a `.docx` in the same
environment recorded all four attempts FAILing with their real
reasons (Tika package missing, PyMuPDF PDF-only, Docling package
missing, OCR unsupported for docx) and a FAIL final stage — on a
machine with Tika/Docling installed the same file succeeds, as the
stored records in `results/` show. Those two pre-existing records
(same `.docx` uploaded twice, both accepted by Tika) are served by
`/api/resumes` with backfilled traces: primary `tika`, final `tika`,
1 attempt, 8 trace stages each, as two separate runs.

A second live smoke test for this update used a generated
prose-only PDF (no resume sections): Tika FAIL (package missing) →
PyMuPDF extraction with quality **WARNING** (decision `continue`,
held as candidate) → Docling and OCR were actually attempted (both
FAIL in the minimal environment) → with nothing accepted outright,
the PyMuPDF candidate was **selected** as best candidate and the run
ended Success / WARNING with the comparison in `selection_reason` —
the exact behaviour that previously stopped at the first WARNING.
`/api/health` also reported the `quality_gate` thresholds
(50 / 75 / 75).

## Troubleshooting

- **Port 8000 already in use** — start with another port:
  `uvicorn api.main:app --port 8001` and open that URL instead.
- **“Backend unavailable” badge in the GUI** — the API server is not
  running, or the page was opened as a `file://` URL. Always open the
  GUI through the server URL so `/api/*` resolves.
- **Every PDF extracts via PyMuPDF and Tika always “fails”** — Java
  17+ or the Tika JAR is missing. Check `java -version` and
  `extractor/config/extractor.yaml` → `tika.jar_path` (or the
  `TIKA_JAR_PATH` environment variable; Windows default
  `C:\Apache\Tika\tika-app-4.2.0.jar`, with `lib/` beside the JAR).
  This is expected behaviour in a minimal install, not an error.
- **OCR / Docling attempts fail** — Tesseract binary / Docling package
  not installed. Install them separately if you need those fallbacks.
- **Module import errors on startup** — run from the project root and
  make sure the virtual environment is activated and
  `pip install -r requirements.txt` completed.

## Limitations (existing backend + this layer)

- No stage-by-stage progress streaming: the backend processes each
  file synchronously (in a threadpool), so the UI shows a processing
  state during a batch and exact outcomes afterwards.
- History persistence is a simple local JSON store (`results/`), not
  a database — fine for local/single-user use, not for concurrent
  multi-user deployment.
- Extraction/OCR quality is bounded by the underlying tools and by
  which optional tools (Tika JAR + Java, Docling, Tesseract) are installed.
- `.txt`/`.docx` etc. depend primarily on Tika (or Docling); without
  those tools installed, such files will fail cleanly with the
  attempt reasons recorded, while PDFs still work via PyMuPDF.
