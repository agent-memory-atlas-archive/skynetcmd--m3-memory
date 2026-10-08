-- pg_059_agent_liveness.up.sql
--
-- PostgreSQL counterpart of 051_agent_liveness.up.sql (full rationale there).
-- Record the agent's host and client process so team membership can be derived:
-- a member has touched within the window OR its client process is alive on its
-- node. pid_create_time pairs with pid so a reused PID is not mistaken for the
-- agent. Not backfilled: NULL means "has not touched since the column existed".

ALTER TABLE agents ADD COLUMN IF NOT EXISTS hostname TEXT DEFAULT NULL;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS pid BIGINT DEFAULT NULL;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS pid_create_time DOUBLE PRECISION DEFAULT NULL;
