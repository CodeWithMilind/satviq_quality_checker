"""SATVIQ AI Evaluation Calibration System (development/testing only).

Measures how accurately the production evaluation engine
(``evaluation/``) agrees with HUMAN-VERIFIED ground truth: section
detection precision/recall/F1, missing-section detection, score
agreement and role-suitability accuracy.

Production isolation rules (spec Part L):
- Nothing in this package is imported by the production pipeline.
- Calibration never modifies production prompts, YAML or behaviour.
- AI output is never used as ground truth.
- Metrics are computed only from persisted calibration records;
  insufficient data yields None (rendered "N/A"), never estimates.

Phase 1: migration 006 tables, annotation schemas, ground-truth
import, deterministic splitting, the metrics engine and the
PostgreSQL store. Runs, lifecycle and the frontend follow in later
phases.
"""

CALIBRATION_VERSION = "calibration/0.1"
