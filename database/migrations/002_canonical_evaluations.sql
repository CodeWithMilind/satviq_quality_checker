-- =====================================================================
-- SATVIQ migration 002 — canonical AI evaluation JSON (Phase 2 B2B)
-- ONE new table. No existing table is altered. Run once:
--   psql -d satviq -f database/migrations/002_canonical_evaluations.sql
-- or via: python scripts/import_to_postgres.py --migrate
-- =====================================================================
CREATE TABLE IF NOT EXISTS candidate_canonical_evaluations (
    id              bigserial PRIMARY KEY,
    candidate_id    integer NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    resume_id       varchar(64) NOT NULL,
    evaluation_id   varchar(64),
    schema_version  varchar(20) NOT NULL,
    canonical       jsonb NOT NULL,
    built_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (resume_id, schema_version)
);
CREATE INDEX IF NOT EXISTS idx_canonical_candidate
    ON candidate_canonical_evaluations (candidate_id);
CREATE INDEX IF NOT EXISTS idx_canonical_profile_gin
    ON candidate_canonical_evaluations USING gin (canonical);
COMMENT ON TABLE candidate_canonical_evaluations IS
    'Canonical ai-evaluation/1.0 documents: deterministic projections of the stored record + latest successful AI evaluation. Versioned (one row per resume per schema version), rebuildable at any time from candidates.pipeline_record — never a source of truth over the evaluation history itself.';
