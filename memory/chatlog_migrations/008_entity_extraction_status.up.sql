-- 008_entity_extraction_status.up.sql
--
-- Ports main-lane 038's column into the CHATLOG lane: `status` on
-- entity_extraction_queue ('done' = processed, entities or empty; 'failed' = last
-- attempt errored; NULL = never processed, retry-eligible).
--
-- WHY THIS EXISTS. m3_entities added the column to chatlog stores at runtime,
-- outside this lane's migrations. m3 schemas change only through migrations (user
-- decision 2026-10-13).
--
-- ONE STATEMENT ON PURPOSE. SQLite has no ADD COLUMN IF NOT EXISTS, and a store
-- the runtime path already touched has the column: the runner then rolls the file
-- back and marks it applied, which is correct only when the ALTER is the whole
-- file. The index is 009 (IF NOT EXISTS), so it is never rolled back with it.

ALTER TABLE entity_extraction_queue ADD COLUMN status TEXT;
