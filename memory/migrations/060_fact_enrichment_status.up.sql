-- 060_fact_enrichment_status.up.sql
--
-- Adds `status` to fact_enrichment_queue, as 038 did for entity_extraction_queue
-- (PostgreSQL has had it since pg_041). A memory whose enrichment yields ZERO
-- facts leaves no fact child, so without a terminal marker the drain re-sends it
-- to the SLM on every pass. 'done' marks it processed; selection skips it.
--
-- Pure additive: existing rows read NULL (retry-eligible, today's behaviour).
--
--   status  TEXT  — 'done' = processed, no facts; 'failed' = last attempt
--                   errored; NULL = never processed (retry-eligible).

ALTER TABLE fact_enrichment_queue ADD COLUMN status TEXT;

-- Partial index: only 'done' rows are skipped by the selection.
CREATE INDEX IF NOT EXISTS idx_feq_status_done
    ON fact_enrichment_queue(memory_id) WHERE status = 'done';
