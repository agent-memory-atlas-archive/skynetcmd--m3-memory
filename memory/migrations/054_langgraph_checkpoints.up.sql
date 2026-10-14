-- 054_langgraph_checkpoints.up.sql
--
-- The LangGraph checkpointer's two tables, now owned by migrations (m3 schemas
-- change only through migrations). Before this the
-- integration created them at first use, so an existing install may already have
-- them in the earlier shape: the CREATEs are IF NOT EXISTS and the ALTER brings
-- either case to the same end state. One definition per backend (PostgreSQL pg_063).
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
    checkpoint     BLOB NOT NULL,
    metadata       BLOB NOT NULL,
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
    blob           BLOB NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
ALTER TABLE m3_lg_checkpoints ADD COLUMN deleted_at TEXT;
