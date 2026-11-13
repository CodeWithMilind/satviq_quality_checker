-- SATVIQ migration 006 — AI Evaluation Calibration System tables.
-- A separate development/testing module (spec Parts A-L): datasets of
-- resumes with HUMAN-VERIFIED ground truth, deterministic splits,
-- evaluation runs against prompt/YAML candidates, persisted metrics
-- and error records. Additive only — no production table is altered,
-- and nothing in the production pipeline reads these tables.
-- Idempotent: the --migrate runner executes every migration file on
-- every run, so every statement is safe to re-apply.

CREATE TABLE IF NOT EXISTS calibration_datasets (
    id           uuid PRIMARY KEY,
    name         varchar(255) NOT NULL UNIQUE,
    description  text NOT NULL DEFAULT '',
    source       varchar(100) NOT NULL DEFAULT '',
    status       varchar(32)  NOT NULL DEFAULT 'active',
    metadata     jsonb        NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz  NOT NULL DEFAULT now(),
    updated_at   timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS calibration_resumes (
    id                uuid PRIMARY KEY,
    dataset_id        uuid NOT NULL REFERENCES calibration_datasets(id)
                          ON DELETE CASCADE,
    original_filename varchar(500) NOT NULL,
    stored_path       text NOT NULL DEFAULT '',
    file_hash         varchar(64)  NOT NULL,
    file_type         varchar(20)  NOT NULL DEFAULT '',
    source_record_id  varchar(255),
    created_at        timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (dataset_id, file_hash)
);

CREATE TABLE IF NOT EXISTS calibration_annotations (
    id                   uuid PRIMARY KEY,
    calibration_resume_id uuid NOT NULL REFERENCES calibration_resumes(id)
                              ON DELETE CASCADE,
    version              integer NOT NULL,
    annotation           jsonb   NOT NULL,
    status               varchar(32) NOT NULL DEFAULT 'pending',
    annotated_by         varchar(255) NOT NULL DEFAULT '',
    notes                text NOT NULL DEFAULT '',
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (calibration_resume_id, version)
);

CREATE TABLE IF NOT EXISTS calibration_splits (
    id                   uuid PRIMARY KEY,
    dataset_id           uuid NOT NULL REFERENCES calibration_datasets(id)
                              ON DELETE CASCADE,
    calibration_resume_id uuid NOT NULL REFERENCES calibration_resumes(id)
                              ON DELETE CASCADE,
    split_name           varchar(32) NOT NULL,
    seed                 integer NOT NULL,
    split_config         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (dataset_id, calibration_resume_id)
);

CREATE TABLE IF NOT EXISTS calibration_config_versions (
    id             uuid PRIMARY KEY,
    version_label  varchar(255) NOT NULL UNIQUE,
    config_type    varchar(32)  NOT NULL,
    file_path      text NOT NULL DEFAULT '',
    content_hash   varchar(64)  NOT NULL DEFAULT '',
    status         varchar(32)  NOT NULL DEFAULT 'candidate',
    parent_version_id uuid REFERENCES calibration_config_versions(id),
    notes          text NOT NULL DEFAULT '',
    created_by     varchar(255) NOT NULL DEFAULT '',
    created_at     timestamptz  NOT NULL DEFAULT now(),
    activated_at   timestamptz
);

CREATE TABLE IF NOT EXISTS calibration_runs (
    id                       uuid PRIMARY KEY,
    name                     varchar(255) NOT NULL DEFAULT '',
    dataset_id               uuid REFERENCES calibration_datasets(id),
    split_name               varchar(32) NOT NULL DEFAULT '',
    prompt_version           varchar(100) NOT NULL DEFAULT '',
    yaml_version_id          uuid REFERENCES calibration_config_versions(id),
    yaml_version_label       varchar(255) NOT NULL DEFAULT '',
    evaluation_config_version varchar(100) NOT NULL DEFAULT '',
    model                    varchar(255) NOT NULL DEFAULT '',
    status                   varchar(32) NOT NULL DEFAULT 'queued',
    total_resumes            integer NOT NULL DEFAULT 0,
    completed_resumes        integer NOT NULL DEFAULT 0,
    failed_resumes           integer NOT NULL DEFAULT 0,
    run_config               jsonb NOT NULL DEFAULT '{}'::jsonb,
    notes                    text NOT NULL DEFAULT '',
    created_by               varchar(255) NOT NULL DEFAULT '',
    started_at               timestamptz,
    completed_at             timestamptz,
    created_at               timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS calibration_results (
    id                   uuid PRIMARY KEY,
    run_id               uuid NOT NULL REFERENCES calibration_runs(id)
                              ON DELETE CASCADE,
    calibration_resume_id uuid NOT NULL REFERENCES calibration_resumes(id)
                              ON DELETE CASCADE,
    status               varchar(32) NOT NULL DEFAULT 'pending',
    analysis             jsonb,
    prompt_version       varchar(100) NOT NULL DEFAULT '',
    yaml_version         varchar(255) NOT NULL DEFAULT '',
    tokens               jsonb,
    duration_ms          integer,
    error                text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, calibration_resume_id)
);

CREATE TABLE IF NOT EXISTS calibration_metrics (
    id          uuid PRIMARY KEY,
    run_id      uuid NOT NULL REFERENCES calibration_runs(id)
                     ON DELETE CASCADE,
    scope       varchar(32) NOT NULL DEFAULT 'overall',
    scope_key   varchar(255) NOT NULL DEFAULT '',
    metrics     jsonb NOT NULL DEFAULT '{}'::jsonb,
    sample_size integer NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, scope, scope_key)
);

CREATE TABLE IF NOT EXISTS calibration_errors (
    id                   uuid PRIMARY KEY,
    run_id               uuid NOT NULL REFERENCES calibration_runs(id)
                              ON DELETE CASCADE,
    calibration_resume_id uuid NOT NULL REFERENCES calibration_resumes(id)
                              ON DELETE CASCADE,
    error_type           varchar(64) NOT NULL,
    section_name         varchar(255) NOT NULL DEFAULT '',
    expected_value       text,
    predicted_value      text,
    severity             varchar(16) NOT NULL DEFAULT 'medium',
    details              jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cal_resumes_dataset
    ON calibration_resumes (dataset_id);
CREATE INDEX IF NOT EXISTS idx_cal_annotations_resume
    ON calibration_annotations (calibration_resume_id);
CREATE INDEX IF NOT EXISTS idx_cal_splits_dataset
    ON calibration_splits (dataset_id);
CREATE INDEX IF NOT EXISTS idx_cal_runs_dataset
    ON calibration_runs (dataset_id);
CREATE INDEX IF NOT EXISTS idx_cal_runs_status
    ON calibration_runs (status);
CREATE INDEX IF NOT EXISTS idx_cal_results_run
    ON calibration_results (run_id);
CREATE INDEX IF NOT EXISTS idx_cal_metrics_run
    ON calibration_metrics (run_id);
CREATE INDEX IF NOT EXISTS idx_cal_errors_run
    ON calibration_errors (run_id);
CREATE INDEX IF NOT EXISTS idx_cal_errors_type
    ON calibration_errors (error_type);
