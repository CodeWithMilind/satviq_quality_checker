-- =====================================================================
-- SATVIQ — candidates schema V2 (fresh CREATE form)
-- For a NEW database (local dev / greenfield): creates the complete
-- V2 structure — the organization's V1 `candidates` columns, the V2
-- additions, and all 7 child tables — in one script.
-- For the organization's EXISTING database, do NOT run this; use the
-- ALTER-based migration instead (V2 additions only, V1 untouched).
-- PostgreSQL 13+. Run: psql -d satviq -f SATVIQ_Schema_V2_Create.sql
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- candidates — V1 columns (exactly as the current org schema)
--             + V2 columns (marked). One table, nothing renamed.
-- ---------------------------------------------------------------------
CREATE TABLE candidates (
    -- V1 (unchanged)
    id                          serial PRIMARY KEY,
    candidate_id                varchar(255),
    email                       varchar(255),
    first_name                  varchar(255),
    last_name                   varchar(255),
    contact_no                  varchar(255),
    gender                      varchar(255),
    dob                         date,
    isfresher                   boolean,
    curr_company                varchar(255),
    curr_position               varchar(255),
    curr_location               varchar(255),
    pref_location               varchar(255),
    "source"                    varchar(255),
    "exp"                       numeric,
    ctc                         numeric,
    exp_ctc                     numeric,
    notice_period               numeric,
    skills                      varchar(255),
    cv_path                     text,
    cv_text                     text,
    status                      varchar(255),
    qualification               varchar(255),
    remarks                     text,
    created_by                  varchar(255),
    createdat                   timestamptz NOT NULL DEFAULT now(),
    updated_by                  varchar(255),
    updatedat                   timestamptz NOT NULL DEFAULT now(),
    relevant_exp                numeric,
    viewed                      jsonb,
    uan_number                  varchar(255),
    documents                   jsonb,
    embedding_indexed           boolean DEFAULT false,
    alternate_contact_number    varchar(255),
    metadata                    jsonb,
    -- SATVIQ pipeline link (which stored resume record produced this row)
    resume_id                   varchar(64) UNIQUE,
    -- V2: profile enrichment (AI extraction)
    headline                    varchar(255),
    summary                     text,
    linkedin_url                varchar(512),
    github_url                  varchar(512),
    portfolio_url               varchar(512),
    other_links                 jsonb,
    total_experience_years      numeric(5,2),
    -- V2: categorized skills (V1 `skills` varchar kept for compatibility)
    technical_skills            text[],
    soft_skills                 text[],
    tools                       text[],
    taxonomy_skills             text[],
    other_skills                text[],
    certifications              text[],
    languages                   text[],
    highest_qualification       varchar(255),
    -- V2: AI evaluation snapshot (latest successful evaluation)
    career_stage                varchar(100),
    professional_domain         varchar(255),
    ai_completeness_score       numeric(5,2),
    ai_quality_score            numeric(5,2),
    ai_suitable_roles           text[],
    ai_top_role                 varchar(255),
    ai_top_role_score           numeric(5,2),
    ai_evaluated_at             timestamptz,
    -- V2: rule-based Quality Checker snapshot (DIFFERENT metric
    -- from the AI scores above — never combine them)
    qc_status                   varchar(20),
    qc_text_health_score        numeric(5,2),
    qc_completeness_score       numeric(5,2),
    qc_reading_order_score      numeric(5,2),
    qc_checked_at               timestamptz,
    -- V2: extraction provenance (final selected extractor)
    extraction_engine           varchar(50),
    extraction_status           varchar(50),
    extraction_duration_ms      integer,
    parsed_at                   timestamptz,
    -- V2: full structured payload + record keeping
    resume_json                 jsonb,
    pipeline_record             jsonb,
    schema_version              varchar(20) NOT NULL DEFAULT 'v2'
);

CREATE INDEX idx_candidates_taxonomy_skills ON candidates USING gin (taxonomy_skills);
CREATE INDEX idx_candidates_technical_skills ON candidates USING gin (technical_skills);
CREATE INDEX idx_candidates_top_role ON candidates (ai_top_role);
CREATE INDEX idx_candidates_ai_completeness ON candidates (ai_completeness_score);
CREATE INDEX idx_candidates_extraction_engine ON candidates (extraction_engine);
CREATE INDEX idx_candidates_email_lower ON candidates (lower(email));

COMMENT ON COLUMN candidates.ai_completeness_score IS
    'Muse AI semantic evaluation (0-100). Different metric from qc_completeness_score — never combine.';
COMMENT ON COLUMN candidates.qc_completeness_score IS
    'Rule-based YAML Quality Checker (0-100): heading/pattern detection only. Different metric from ai_completeness_score — never combine.';
COMMENT ON COLUMN candidates.extraction_engine IS
    'Extractor actually selected by the pipeline (tika/pymupdf/docling/ocr), not merely the configured primary.';
COMMENT ON COLUMN candidates.pipeline_record IS
    'Complete SATVIQ pipeline record (attempts, quality, token usage, normalization, contact recovery) for audit and reproducibility. The normalized columns/tables are the queryable projection of this payload.';

-- ---------------------------------------------------------------------
-- candidate_experiences — one row per job (extraction experience[])
-- ---------------------------------------------------------------------
CREATE TABLE candidate_experiences (
    id              bigserial PRIMARY KEY,
    candidate_id    integer NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    title           varchar(255),
    company         varchar(255),
    location        varchar(255),
    start_date      varchar(50),
    end_date        varchar(50),
    duration_months numeric(6,2),
    is_current      boolean,
    highlights      text[],
    raw_dates       jsonb,
    createdat       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_exp_candidate ON candidate_experiences (candidate_id);

-- ---------------------------------------------------------------------
-- candidate_education — one row per qualification
-- ---------------------------------------------------------------------
CREATE TABLE candidate_education (
    id              bigserial PRIMARY KEY,
    candidate_id    integer NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    degree          varchar(255),
    institution     varchar(255),
    location        varchar(255),
    start_date      varchar(50),
    end_date        varchar(50),
    field_of_study  varchar(255),
    score_text      varchar(100),
    score_numeric   numeric(6,2),
    createdat       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_edu_candidate ON candidate_education (candidate_id);

-- ---------------------------------------------------------------------
-- candidate_projects — one row per project (V1 had no home for these)
-- ---------------------------------------------------------------------
CREATE TABLE candidate_projects (
    id              bigserial PRIMARY KEY,
    candidate_id    integer NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    name            varchar(255),
    description     text,
    tech_stack      text[],
    links           text[],
    evidence        text[],
    createdat       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_proj_candidate ON candidate_projects (candidate_id);

-- ---------------------------------------------------------------------
-- candidate_evaluations — one row PER AI evaluation run (history;
-- failed attempts recorded too, never overwriting a success)
-- ---------------------------------------------------------------------
CREATE TABLE candidate_evaluations (
    id                          bigserial PRIMARY KEY,
    candidate_id                integer NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    analysis_version            integer,
    model                       varchar(100),
    yaml_version                varchar(255),
    config_version              varchar(50),
    status                      varchar(20),
    error_category              varchar(50),
    error                       text,
    completeness_score          numeric(5,2),
    completeness_classification varchar(50),
    quality_score               numeric(5,2),
    quality_classification      varchar(50),
    career_stage                varchar(100),
    professional_domain         varchar(255),
    strengths                   text[],
    weaknesses                  text[],
    recommendations             text[],
    evaluation                  jsonb,
    input_tokens                integer,
    output_tokens               integer,
    reasoning_tokens            integer,
    total_tokens                integer,
    duration_ms                 integer,
    createdat                   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_eval_candidate ON candidate_evaluations (candidate_id, analysis_version);

-- ---------------------------------------------------------------------
-- candidate_role_suitability — one row per role per evaluation
-- ---------------------------------------------------------------------
CREATE TABLE candidate_role_suitability (
    id                  bigserial PRIMARY KEY,
    candidate_id        integer NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    evaluation_id       bigint NOT NULL REFERENCES candidate_evaluations(id) ON DELETE CASCADE,
    role                varchar(255),
    score               numeric(5,2),
    matching_skills     text[],
    missing_skills      text[],
    relevant_projects   text[],
    relevant_experience text[],
    evidence            text[],
    explanation         text,
    createdat           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (evaluation_id, role)
);
CREATE INDEX idx_role_candidate ON candidate_role_suitability (candidate_id, score DESC);
CREATE INDEX idx_role_name ON candidate_role_suitability (role, score DESC);

-- ---------------------------------------------------------------------
-- candidate_section_analysis — section findings from BOTH systems.
-- source 'qc' = rule-based YAML checker (detected / not_detected);
-- source 'ai' = Muse AI evaluation (present / partial / missing /
-- insufficient). Disagreements stay visible — never silently merged.
-- ---------------------------------------------------------------------
CREATE TABLE candidate_section_analysis (
    id              bigserial PRIMARY KEY,
    candidate_id    integer NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    evaluation_id   bigint REFERENCES candidate_evaluations(id) ON DELETE CASCADE,
    source          varchar(10) NOT NULL,
    section         varchar(255),
    finding         varchar(30),
    importance      varchar(20),
    reason          text,
    impact          text,
    recommendation  text,
    evidence        text[],
    payload         jsonb,
    createdat       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_section_candidate ON candidate_section_analysis (candidate_id, source);

-- ---------------------------------------------------------------------
-- candidate_extraction_attempts — one row per extractor attempt
-- (quality-gated chain Tika -> PyMuPDF -> Docling -> OCR)
-- ---------------------------------------------------------------------
CREATE TABLE candidate_extraction_attempts (
    id                  bigserial PRIMARY KEY,
    candidate_id        integer NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    extractor           varchar(50),
    attempt_order       integer,
    accepted            boolean,
    selected            boolean,
    skipped             boolean DEFAULT false,
    quality_status      varchar(20),
    text_health_score   numeric(5,2),
    completeness_score  numeric(5,2),
    reading_order_score numeric(5,2),
    char_count          integer,
    duration_ms         integer,
    ocr_used            boolean,
    error               text,
    createdat           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_attempt_candidate ON candidate_extraction_attempts (candidate_id);

COMMIT;

-- =====================================================================
-- Optional (future, NOT executed): semantic search / RAG
--   1. CREATE EXTENSION IF NOT EXISTS vector;
--   2. ALTER TABLE candidates ADD COLUMN cv_embedding vector(N);
--      N depends on the embedding model chosen — decide the model
--      FIRST (changing it later means re-embedding every CV).
--      V1's cv_vector (text) and embedding_indexed columns remain.
--   3. Chunk-level retrieval: candidate_resume_chunks
--      (candidate_id, chunk_type, chunk_text, embedding).
-- =====================================================================
