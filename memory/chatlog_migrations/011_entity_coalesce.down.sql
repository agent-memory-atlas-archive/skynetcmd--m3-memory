-- 011_entity_coalesce.down.sql
-- Reverses 011, as main-lane 043's down does.

DROP TABLE IF EXISTS entity_coalesce_embeddings;
DROP INDEX IF EXISTS idx_ecc_reviewed;
DROP TABLE IF EXISTS entity_coalesce_candidates;
DROP INDEX IF EXISTS idx_entities_cluster;
DROP INDEX IF EXISTS idx_entities_coalesce_state;
ALTER TABLE entities DROP COLUMN resolution_run;
ALTER TABLE entities DROP COLUMN cluster_id;
ALTER TABLE entities DROP COLUMN coalesce_state;
