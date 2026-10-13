-- pg_063_langgraph_checkpoints.down.sql
-- Reverses pg_063's column only. The tables are NOT dropped: on an existing
-- install they predate pg_063 (the integration created them), and dropping them
-- would erase users' checkpoints, which only gdpr_forget may do.

ALTER TABLE m3_lg_checkpoints DROP COLUMN IF EXISTS deleted_at;
