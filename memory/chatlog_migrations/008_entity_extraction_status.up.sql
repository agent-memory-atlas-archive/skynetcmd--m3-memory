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
-- SQLite has no ADD COLUMN IF NOT EXISTS, and a store the runtime path already
-- touched has the column: the runner skips that one statement and applies the
-- rest. The index is still kept in 009, for runners from before that fix.

ALTER TABLE entity_extraction_queue ADD COLUMN status TEXT;
