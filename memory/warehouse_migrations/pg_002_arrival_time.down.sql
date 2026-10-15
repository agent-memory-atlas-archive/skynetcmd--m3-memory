-- pg_002_arrival_time.down.sql
-- Removes the arrival stamp. Pulls on a version that needs it would then fail, so
-- revert m3 on every machine first.

DROP TRIGGER IF EXISTS m3_items_arrival ON memory_items;
DROP TRIGGER IF EXISTS m3_relationships_arrival ON memory_relationships;
DROP TRIGGER IF EXISTS m3_embeddings_arrival ON memory_embeddings;
DROP INDEX IF EXISTS idx_memory_items_arrived;
DROP INDEX IF EXISTS idx_memory_relationships_arrived;
DROP INDEX IF EXISTS idx_memory_embeddings_arrived;
ALTER TABLE memory_items DROP COLUMN IF EXISTS wh_arrived_at;
ALTER TABLE memory_relationships DROP COLUMN IF EXISTS wh_arrived_at;
ALTER TABLE memory_embeddings DROP COLUMN IF EXISTS wh_arrived_at;
DROP FUNCTION IF EXISTS m3_stamp_arrival();
