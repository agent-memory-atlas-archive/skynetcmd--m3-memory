-- 012_sync_bookkeeping.up.sql
--
-- The CHATLOG lane's sync bookkeeping, as main-lane 005 (sync_watermarks) and
-- 044 (sync_locks) give the main store. pg_sync created both on chatlog stores at
-- runtime, because no chatlog migration did; m3 schemas change only through
-- migrations. Per-machine state: never replicated to
-- the warehouse. IF NOT EXISTS adopts a store the runtime path already touched.

CREATE TABLE IF NOT EXISTS sync_watermarks (
    direction      TEXT PRIMARY KEY,   -- 'pg_push', 'pg_pull'
    last_synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_locks (
    lock_name   TEXT PRIMARY KEY,
    holder      TEXT,
    acquired_at TEXT
);
