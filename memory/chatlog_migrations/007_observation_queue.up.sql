-- 007_observation_queue.up.sql
--
-- Ports main-lane 025 into the CHATLOG lane: the observer/reflector queues the
-- enrichment drain reads on a chatlog store, and the observation index on its
-- memory_items. Same tables, same indexes.
--
-- WHY THIS EXISTS. The drain used to replay main's 025 file against a chatlog
-- store at runtime, outside this lane's migrations, so the change was recorded
-- nowhere. m3 schemas change only through migrations (user decision 2026-10-13).
-- A store the drain already touched has these objects: every statement is
-- IF NOT EXISTS, so this migration adopts it unchanged.

CREATE TABLE IF NOT EXISTS observation_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    user_id         TEXT,
    enqueued_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    attempts        INTEGER DEFAULT 0,
    last_error      TEXT,
    last_attempt_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_oq_conversation_id ON observation_queue(conversation_id);
CREATE INDEX IF NOT EXISTS idx_oq_attempts ON observation_queue(attempts, enqueued_at);
CREATE TABLE IF NOT EXISTS reflector_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    user_id         TEXT,
    obs_count_at_enqueue INTEGER,
    enqueued_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    attempts        INTEGER DEFAULT 0,
    last_error      TEXT,
    last_attempt_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rq_user_conv ON reflector_queue(user_id, conversation_id);
CREATE INDEX IF NOT EXISTS idx_rq_attempts ON reflector_queue(attempts, enqueued_at);
CREATE INDEX IF NOT EXISTS idx_mi_type_user_obs
  ON memory_items(type, user_id, valid_from)
  WHERE type='observation';
