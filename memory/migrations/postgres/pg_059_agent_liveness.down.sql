-- pg_059_agent_liveness.down.sql
-- Reverses pg_059. Loses host and client-process attribution only; membership
-- falls back to the last-touch window.

ALTER TABLE agents DROP COLUMN IF EXISTS pid_create_time;
ALTER TABLE agents DROP COLUMN IF EXISTS pid;
ALTER TABLE agents DROP COLUMN IF EXISTS hostname;
