"""SATVIQ — Muse AI Resume Evaluation (the intelligence layer).

Evaluates an already-processed resume record with Muse AI:
career-stage-aware section intelligence, three independent scores
(completeness, quality, per-role suitability) and evidence-based
job-role recommendations. It builds on the existing pipeline —
extraction, normalization and the deterministic Quality Checker are
reused, never repeated or replaced — and stores each analysis on
the resume's record through the project's JSON record store
(``api.store``): ``record["analyses"]`` (full history) and
``record["analysis"]`` (latest success).

Public surface:
- :func:`evaluation.service.analyze_record` — run one evaluation.
- :func:`evaluation.config.load_config` / ``validate_config`` —
  the validated evaluation catalogue (``evaluation/config.yaml``).
- :func:`evaluation.schema.validate_evaluation` — parse + validate
  model output with deterministic post-validation.
"""
