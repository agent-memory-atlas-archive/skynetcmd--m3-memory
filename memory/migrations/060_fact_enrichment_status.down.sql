-- 060_fact_enrichment_status.down.sql
-- Reverts 060_fact_enrichment_status.up.sql.

DROP INDEX IF EXISTS idx_feq_status_done;

ALTER TABLE fact_enrichment_queue DROP COLUMN status;
