-- pg_041_queue_status_column.up.sql
--
-- Add the `status` marker column ('done' | 'failed') to the extraction/enrichment
-- queues. SQLite gets it from its own migrations (main 038, chatlog 008); m3
-- schemas change only through migrations.
--
-- ADD COLUMN IF NOT EXISTS is idempotent; one implicit transaction (migrate_pg
-- wraps the file), no explicit BEGIN/COMMIT.

ALTER TABLE entity_extraction_queue ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE fact_enrichment_queue   ADD COLUMN IF NOT EXISTS status TEXT;

CREATE INDEX IF NOT EXISTS idx_eeq_status ON entity_extraction_queue(status);
CREATE INDEX IF NOT EXISTS idx_feq_status ON fact_enrichment_queue(status);
