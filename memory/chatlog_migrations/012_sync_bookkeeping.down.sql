-- 012_sync_bookkeeping.down.sql
-- No-op: both tables may predate 012 (pg_sync created them at runtime) and hold
-- this machine's sync position.

SELECT 1;
