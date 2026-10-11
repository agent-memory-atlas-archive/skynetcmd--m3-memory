-- 053_memory_access_log.up.sql
--
-- The read-access audit log (GDPR Art. 30: who read what, when). m3 recorded
-- THAT a memory was read (last_accessed_at, access_count) but not by whom.
-- Append-only: rows are written in batches by memory.db's read-log flusher and
-- removed only by retention (maintenance) or gdpr_forget. One definition per
-- backend (PostgreSQL pg_062, where ts is TIMESTAMPTZ like the other tables).

CREATE TABLE IF NOT EXISTS memory_access_log (
    id         TEXT PRIMARY KEY,
    memory_id  TEXT NOT NULL,
    agent_id   TEXT NOT NULL,
    user_id    TEXT,
    op         TEXT NOT NULL,
    ts         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mal_memory_ts ON memory_access_log(memory_id, ts);
CREATE INDEX IF NOT EXISTS idx_mal_agent_ts ON memory_access_log(agent_id, ts);
CREATE INDEX IF NOT EXISTS idx_mal_user ON memory_access_log(user_id);
