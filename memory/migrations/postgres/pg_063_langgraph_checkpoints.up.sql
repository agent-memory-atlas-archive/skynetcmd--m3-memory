-- pg_063_langgraph_checkpoints.up.sql
--
-- PostgreSQL counterpart of 054_langgraph_checkpoints.up.sql. The LangGraph
-- checkpointer's two tables, now owned by migrations (m3 schemas change only
-- through migrations, user decision 2026-10-13). An existing install may already
-- have them from the integration's first use, so the CREATEs are IF NOT EXISTS
-- and the column is ADD ... IF NOT EXISTS.
--
-- deleted_at: delete_thread soft-deletes (hard delete is GDPR-only).

CREATE TABLE IF NOT EXISTS m3_lg_checkpoints (
    thread_id      TEXT NOT NULL,
    checkpoint_ns  TEXT NOT NULL DEFAULT '',
    checkpoint_id  TEXT NOT NULL,
    parent_id      TEXT,
    user_id        TEXT NOT NULL DEFAULT '',
    type           TEXT,
    metadata_type  TEXT,
    checkpoint     BYTEA NOT NULL,
    metadata       BYTEA NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
CREATE INDEX IF NOT EXISTS ix_m3_lg_ckpt_thread
    ON m3_lg_checkpoints (thread_id, checkpoint_ns, created_at DESC);
CREATE TABLE IF NOT EXISTS m3_lg_writes (
    thread_id      TEXT NOT NULL,
    checkpoint_ns  TEXT NOT NULL DEFAULT '',
    checkpoint_id  TEXT NOT NULL,
    task_id        TEXT NOT NULL,
    idx            INTEGER NOT NULL,
    channel        TEXT NOT NULL,
    type           TEXT,
    blob           BYTEA NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
ALTER TABLE m3_lg_checkpoints ADD COLUMN IF NOT EXISTS deleted_at TEXT;
