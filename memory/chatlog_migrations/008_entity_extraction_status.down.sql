-- 008_entity_extraction_status.down.sql
-- Reverses 008.

ALTER TABLE entity_extraction_queue DROP COLUMN status;
