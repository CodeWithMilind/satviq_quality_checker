-- SATVIQ migration 004 — persistent analytics architecture.
--
-- 1. resume_processing_runs: one row per PROCESSING RUN of a
--    resume. Reprocessing appends a new run; re-saving the same
--    state (e.g. after an evaluation) does not — run_key is a
--    fingerprint of the run's attempt set, and inserts are
--    ON CONFLICT DO NOTHING. History is never overwritten.
-- 2. candidate_extraction_attempts gains the run link plus the
--    per-attempt fields analytics needs (success, headline quality
--    score, start/end timestamps). Attempts become append-only:
--    the sync no longer deletes them between runs.
-- 3. ai_api_costs gains the JSON-correction operation type (split
--    from ai_evaluation), reasoning tokens, error text and the
--    pricing configuration version.
-- The candidates master table is NOT modified.

CREATE TABLE IF NOT EXISTS resume_processing_runs (
    id                    bigserial PRIMARY KEY,
    candidate_id          bigint REFERENCES candidates(id)
                          ON DELETE CASCADE,
    resume_id             varchar(64) NOT NULL,
    run_key               varchar(80) NOT NULL UNIQUE,
    filename              text,
    uploaded_at           timestamptz,
    processed_at          timestamptz,
    duration_ms           integer,
    status                varchar(32),
    success               boolean,
    file_type             varchar(64),
    file_size_bytes       bigint,
    page_count            integer,
    final_extractor       varchar(64),
    attempt_count         integer,
    successful_attempts   integer,
    failed_attempts       integer,
    skipped_attempts      integer,
    selected_quality_score numeric(6, 2),
    quality_status        varchar(20),
    qc_text_health_score  numeric(6, 2),
    qc_completeness_score numeric(6, 2),
    qc_reading_order_score numeric(6, 2),
    fallback_occurred     boolean,
    fallback_reason       text,
    error                 text,
    created_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_runs_resume
    ON resume_processing_runs (resume_id);
CREATE INDEX IF NOT EXISTS idx_runs_processed
    ON resume_processing_runs (processed_at);

ALTER TABLE candidate_extraction_attempts
    ADD COLUMN IF NOT EXISTS run_id bigint
        REFERENCES resume_processing_runs(id) ON DELETE CASCADE;
ALTER TABLE candidate_extraction_attempts
    ADD COLUMN IF NOT EXISTS success boolean;
ALTER TABLE candidate_extraction_attempts
    ADD COLUMN IF NOT EXISTS quality_score numeric(6, 2);
ALTER TABLE candidate_extraction_attempts
    ADD COLUMN IF NOT EXISTS started_at timestamptz;
ALTER TABLE candidate_extraction_attempts
    ADD COLUMN IF NOT EXISTS completed_at timestamptz;
CREATE INDEX IF NOT EXISTS idx_attempts_run
    ON candidate_extraction_attempts (run_id);
CREATE INDEX IF NOT EXISTS idx_attempts_extractor
    ON candidate_extraction_attempts (extractor);

ALTER TABLE ai_api_costs
    DROP CONSTRAINT IF EXISTS ai_api_costs_operation_type_check;
ALTER TABLE ai_api_costs
    ADD CONSTRAINT ai_api_costs_operation_type_check
    CHECK (operation_type IN
           ('json_extraction', 'ai_evaluation', 'json_correction'));
ALTER TABLE ai_api_costs
    ADD COLUMN IF NOT EXISTS reasoning_tokens integer;
ALTER TABLE ai_api_costs
    ADD COLUMN IF NOT EXISTS error text;
ALTER TABLE ai_api_costs
    ADD COLUMN IF NOT EXISTS pricing_version varchar(32);
