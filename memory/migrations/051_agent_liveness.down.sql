-- 051_agent_liveness.down.sql
-- Reverses 051. Requires SQLite >= 3.35 for DROP COLUMN (same floor as 048).
--
-- Loses host and client-process attribution only; membership then falls back to
-- the last-touch window, and every agent re-stamps these on its next touch if
-- the columns return.

ALTER TABLE agents DROP COLUMN pid_create_time;
ALTER TABLE agents DROP COLUMN pid;
ALTER TABLE agents DROP COLUMN hostname;
