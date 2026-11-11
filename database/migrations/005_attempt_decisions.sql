-- SATVIQ migration 005 — per-attempt gate decisions in PostgreSQL.
-- candidate_extraction_attempts gains the technical extraction
-- status (SUCCESS/FAILED/TIMEOUT/ERROR, independent of quality),
-- the gate decision (accept/continue/reject) and its reason, so
-- fallback and selection decisions are queryable per attempt.
-- Additive only; historical rows keep NULLs (unknown, not zero).

ALTER TABLE candidate_extraction_attempts
    ADD COLUMN IF NOT EXISTS technical_status varchar(32);
ALTER TABLE candidate_extraction_attempts
    ADD COLUMN IF NOT EXISTS decision varchar(20);
ALTER TABLE candidate_extraction_attempts
    ADD COLUMN IF NOT EXISTS decision_reason text;
