-- SATVIQ migration 003 — per-call AI cost tracking.
--
-- One row per actual Muse API call (JSON extraction or AI
-- evaluation), derived from provider-reported usage already stored
-- on the record. Money columns are numeric(20,10): full precision
-- is preserved; rounding happens only at display time.
-- call_ref is deterministic per stored call, and inserts use
-- ON CONFLICT DO NOTHING — historical rows are immutable and a
-- re-sync can never duplicate or rewrite a cost.
-- The candidates master table is NOT modified.

CREATE TABLE IF NOT EXISTS ai_api_costs (
    id                      bigserial PRIMARY KEY,
    call_ref                varchar(160) NOT NULL UNIQUE,
    candidate_id            bigint REFERENCES candidates(id)
                            ON DELETE CASCADE,
    resume_id               varchar(64) NOT NULL,
    operation_type          varchar(32) NOT NULL
                            CHECK (operation_type IN
                                   ('json_extraction', 'ai_evaluation')),
    provider                varchar(64),
    model                   varchar(128),
    prompt_version          varchar(64),
    stage                   varchar(64),
    status                  varchar(32),
    request_id              varchar(128),
    attempt                 integer,
    duration_ms             integer,
    input_tokens            integer NOT NULL,
    cached_input_tokens     integer NOT NULL DEFAULT 0,
    non_cached_input_tokens integer,
    output_tokens           integer NOT NULL,
    total_tokens            integer NOT NULL,
    input_cost_usd          numeric(20, 10),
    cached_input_cost_usd   numeric(20, 10),
    output_cost_usd         numeric(20, 10),
    total_cost_usd          numeric(20, 10),
    usd_inr_rate            numeric(12, 6),
    total_cost_inr          numeric(20, 10),
    currency                varchar(8) NOT NULL DEFAULT 'USD',
    cost_status             varchar(16) NOT NULL DEFAULT 'actual',
    analysis_id             varchar(64),
    created_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_costs_resume
    ON ai_api_costs (resume_id);
CREATE INDEX IF NOT EXISTS idx_ai_costs_operation
    ON ai_api_costs (operation_type);
CREATE INDEX IF NOT EXISTS idx_ai_costs_created
    ON ai_api_costs (created_at);
