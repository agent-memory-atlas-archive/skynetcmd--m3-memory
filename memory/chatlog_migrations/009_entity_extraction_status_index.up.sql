-- 009_entity_extraction_status_index.up.sql
--
-- Main-lane 038's index, for the column chatlog 008 adds. Kept apart from 008 so
-- a store whose 008 was rolled back as already-applied still gets it.

CREATE INDEX IF NOT EXISTS idx_eeq_status_done
    ON entity_extraction_queue(memory_id) WHERE status = 'done';
