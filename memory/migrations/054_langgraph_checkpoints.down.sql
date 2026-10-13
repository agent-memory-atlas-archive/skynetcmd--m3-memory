-- 054_langgraph_checkpoints.down.sql
-- Reverses 054's column only. The tables are NOT dropped: on an existing install
-- they predate 054 (the integration created them), and dropping them would erase
-- users' checkpoints, which only gdpr_forget may do.

ALTER TABLE m3_lg_checkpoints DROP COLUMN deleted_at;
