# SATVIQ Resume Intelligence — Evaluation Platform Notes

What was built for the "Advanced AI-Powered Resume Intelligence
Platform" spec, how it fits the existing project, where reality
differed from the spec's assumptions, and what remains.

## Architecture (as built)

```
Resume Upload
  → Existing Extraction Pipeline (Tika → PyMuPDF → Docling → OCR,
    quality-gated early stopping)            [unchanged]
  → Text Normalization (selected text only)  [unchanged]
  → Existing Quality Assessment (deterministic Quality Checker)
                                               [unchanged]
  → Muse AI Extraction → Resume JSON         [unchanged]
  → NEW: Muse AI Evaluation (evaluation/ package, on demand)
       POST /api/resumes/{id}/analyze
       · one Muse call via MuseClient.chat_json
       · input: Selected Normalized Text + deterministic Quality
         Checker context + compact Resume JSON digest — never any
         other extractor's text, extraction never re-run
       · output validated against evaluation/schema.py with
         deterministic post-validation (scores clamped 0-100,
         classifications recomputed from configured bands,
         suitable_roles recomputed from the detailed role results)
       · appended to record["analyses"]; latest success promoted
         to record["analysis"]; failures never overwrite a success
  → Frontend: Resume Details "AI Evaluation" section + Analytics
    "AI Evaluation" card                       [extended, not redesigned]
```

New modules (all under `evaluation/`):

| File | Role |
|---|---|
| `config.yaml` | Evaluation catalogue: classification bands, 15 standard sections, 12 good-to-have sections, 6 career stages with per-stage focus, 24-role taxonomy. Human-maintained only. |
| `config.py` | Loader + `validate_config()`, band classification, YAML-version fingerprinting, `SATVIQ_EVALUATION_*` env overrides. |
| `schema.py` | Pydantic evaluation schema + `validate_evaluation()` with deterministic post-validation. |
| `prompt.py` | Instructions (evidence-only, career-stage-aware, three independent scores, section equivalence by meaning) + user-text assembly. |
| `service.py` | `analyze_record()` — per-resume running guard, transient retries (max 2), timeout-free Muse call with `reasoning_effort` + JSON mode, history persistence. |
| `store.py` | Analysis persistence on the record via `api.store`; `yaml_version` stamping. |
| `yaml_versions/` | Frozen baseline snapshot of the production completeness YAML + the candidate → validation → regression → approval → activation procedure and candidate aliases awaiting approval. |

Modified files: `muse_ai/client.py` (one additive method,
`chat_json`, with an HTTP-400-only fallback that drops
`response_format` then `reasoning_effort`), `api/main.py` (4 new
endpoints), `api/analytics.py` (separate `evaluation` block),
`api/store.py` (list summaries carry `analysis_count` instead of the
full history), `frontend/index.html`, `frontend/js/api.js`,
`frontend/js/app.js`, `frontend/css/style.css` (AI Evaluation UI;
asset `?v=` bumped).

## Deviations from the spec (deliberate, evidence-based)

1. **PostgreSQL / SQLAlchemy / Alembic do not exist in this
   project** (spec Parts 1 and 9 assume them). Persistence is
   `api/store.py` — one JSON file per resume under `results/`, no
   database, no ORM, no migrations. Adding a database would have
   been a rewrite, not an enhancement, and would have endangered
   the existing records. Analysis persistence therefore extends the
   existing store: analyses live on the record itself
   (`analyses` history + `analysis` latest-success), which gives
   the spec's required fields (analysis id, version, scores, roles,
   sections, full evaluation JSON, model, YAML version, timestamp,
   status) and multiple analysis versions per resume, with zero new
   dependencies. If SATVIQ later adopts a real database, these
   entries map 1:1 onto an `analyses` table.
2. **No autonomous YAML editing** — per the spec's own Part 7.5 and
   the standing instruction not to recreate the deleted YAML
   Editor AI: configuration improvement is a documented human
   candidate/approval flow (`evaluation/yaml_versions/README.md`),
   and **no production YAML content was modified** —
   `quality_checker/config/completeness.yaml` is byte-identical to
   before this work (a verified byte copy is kept as the baseline
   snapshot). The new section-alias/role/scoring configuration the
   spec asks for lives in the new `evaluation/config.yaml`, which
   configures the evaluation stage only and does not compete with
   the Quality Checker's configuration.
3. **Role taxonomy has 24 roles**: the 23 roles listed in spec
   Part 6 plus Site Reliability Engineer, added so the taxonomy
   count matches the spec's stated 24 and because SRE is a standard
   adjacent role for the DevOps/Linux evidence the taxonomy already
   covers.
4. **Evaluation is on-demand, not automatic on upload.** The spec's
   workflow diagram implies evaluation for every resume; running it
   automatically would spend a Muse call on every upload —
   including reprocesses and test uploads — against the spec's own
   "avoid unnecessary Muse API calls". The Analyze / Re-analyze
   button + endpoint implement the same workflow one explicit step
   later. Wiring it into processing later is a one-line call to
   `evaluation.service.analyze_record` if that policy changes.
5. **Dataset calibration (Part 8) is delivered as framework +
   documentation, not a 700-1000-resume run.** The sandbox has no
   Muse API key and no authorized bulk dataset; fabricating
   accuracy figures would violate the spec's own rules ("never
   claim accuracy based only on the number of processed resumes",
   "do not fabricate test results"). What exists: the calibration
   resumes generated earlier for this project, the candidate-alias
   evidence they produced (in `yaml_versions/README.md`), mocked
   end-to-end tests, and the Analytics evaluation block (averages,
   most-recommended roles, most-missed sections, token usage,
   invalid-JSON failure count) that makes a real calibration run
   measurable when a dataset and a key are available. Train /
   validation / holdout separation remains a procedure to execute
   with real data — it is documented, not performed.

## Design decisions worth knowing

- **Reasoning effort is explicit.** Muse Spark is a reasoning
  model; reasoning tokens count toward `max_completion_tokens` and
  the API default (high) effort was observed on this project
  consuming a 2000-token budget entirely on reasoning, returning
  empty content. The evaluation call sends
  `reasoning_effort: low` (env-overridable) and a 4000-token budget,
  plus JSON mode; optional parameters are dropped one at a time on
  HTTP 400 only (see `MuseClient.chat_json`).
- **Deterministic post-validation.** The model proposes; code
  disposes: scores are clamped, classifications recomputed from the
  configured bands, and `suitable_roles` is recomputed from the
  detailed role list (score ≥ 50, best first) — so the stored
  evaluation can never contain a classification that disagrees with
  its score or a suitable-roles array that disagrees with its
  details.
- **The Quality Checker stays a cross-check, not a crutch.** Its
  scores and detected/missing sections are sent to the model as
  clearly-labelled deterministic context, with an explicit warning
  that it can miss nonstandard headings; the model must verify
  against the text. The evaluation's completeness score is the
  model's stage-aware judgement — independent of, and not averaged
  with, the checker's completeness filter.
- **Token accounting is actuals-only and separated.** Evaluation
  entries store the API-reported prompt/completion tokens (None
  when unreported, never estimated) under `usage_type:
  "evaluation"`, and Analytics reports them in their own block,
  never merged into extraction token totals.
- **Failures are data.** Unconfigured Muse, API errors and
  invalid-JSON responses all become persisted `failed` history
  entries with the real error — mirroring how the AI generation
  endpoint surfaces failures — and never destroy a prior success.

## Limitations

- No live Muse evaluation has been run from this environment
  (no API key in the sandbox); all behaviour is verified with a
  mocked client in `tests/test_evaluation.py`.
- Evaluation quality (score agreement with human reviewers, role
  precision/recall) is unmeasured until a labelled dataset is run —
  see deviation 5.
- The per-resume running guard is in-process state (same as the
  existing AI-generation guard): a multi-process deployment would
  need it in shared storage.
- Analysis history grows on the record JSON file; heavy re-analysis
  use would eventually argue for the database the spec assumed.
- The frontend version selector shows older successful analyses
  read-only; failed attempts are listed in the history count and
  the latest failure is shown as an error banner, but full failure
  browsing lives in the API history endpoint.
