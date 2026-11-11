# SATVIQ Cleanup Report — 2026-11-11 (Gating + YAML Editor Transparency build)

Recommendations only. Nothing in this list was deleted automatically.

## Safe to delete
- `__pycache__/` directories anywhere in the project (Python bytecode caches; regenerated automatically).
- `.pytest_cache/` (pytest run cache; regenerated on the next test run).
- `yaml_editor_ai/token_usage.json` copies created inside test/sandbox runs (the module recreates the file on the first real execution; the copy inside any delivered ZIP from a test environment contains only test entries). Do NOT delete the one in your live project folder — it holds your real YAML Editor execution history.
- `results/` and `uploads/` copies inside throwaway test folders only (never in your live project folder — those are your stored resume records and originals).

## Needs manual verification before deletion
- `tests/test_extraction_timeout.py` — tests the per-extractor timeout feature that was deliberately removed in an earlier round; 5 of its tests fail for that reason (pre-existing baseline failures, unchanged by this build). Deleting the file removes those 5 known failures from the suite; keep it if you want the removal documented by tests.
- The trailing block in `requirements.txt` (`openai>=1.30,<2` plus duplicate pydantic/Pillow/pytest lines left over from the old standalone AI-model module): nothing in api/, pipeline/, extractor/, muse_ai/, quality_checker/, yaml_editor_ai/ or tests/ imports `openai`, and no `ai_model` package exists in the current build. Safe to remove after you confirm no external script of yours imports it.

## Required — do not delete
- `extractor/` and `pipeline/` — the legacy chain and the default simple pipeline share these implementations; both are live code paths.
- `api/text_normalization.py` and `pipeline/normalization.py` — different jobs (record-level normalization block vs pipeline-stage normalization); both imported by live code and tests.
- `quality_checker/` — the whole package; the deterministic checker every gate decision depends on.
- `yaml files/` — the versioned YAML store (active + versions + changelog); the single source of truth for completeness keywords since the YAML Editor AI build.
- `yaml_editor_ai/` — the independent YAML Editor AI module, including its token history in your live folder.
- `sample_resume.pdf` — POST /api/extractors/test returns 409 without it.
- `data/prompt_settings.json` — read at runtime by muse_ai/prompt_config.py; holds the configured classification prompt.
- `.env` / `.env.example` — Muse configuration (key stays only in .env, never in code or ZIPs).
- `results/` and `uploads/` in your live project folder — your stored records and original resume files.
