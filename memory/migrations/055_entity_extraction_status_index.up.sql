-- 055_entity_extraction_status_index.up.sql
--
-- Re-creates 038's index where it was lost. 038 is an ALTER plus this index; on a
-- store where the runtime fallback had already added `status`, the ALTER hit
-- "duplicate column", the runner rolled the whole file back and marked 038
-- applied, so the index was never created. IF NOT EXISTS: a no-op where 038
-- applied whole. SQLite only (PostgreSQL's pg_041 creates its own index).

CREATE INDEX IF NOT EXISTS idx_eeq_status_done
    ON entity_extraction_queue(memory_id) WHERE status = 'done';
