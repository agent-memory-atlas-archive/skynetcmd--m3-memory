-- 011_entity_coalesce.up.sql
--
-- Ports main-lane 043 into the CHATLOG lane: the entity-coalescing columns on
-- `entities` and the candidates / embedding-cache tables. The coalescing pass
-- added them to chatlog stores at runtime, outside this lane's migrations; m3
-- schemas change only through migrations (user decision 2026-10-13). A store that
-- already has them: the runner skips an existing column and the rest is
-- IF NOT EXISTS.

ALTER TABLE entities ADD COLUMN coalesce_state TEXT;   -- 'provisional'|'quarantined'|'clustered'|NULL
ALTER TABLE entities ADD COLUMN cluster_id TEXT;
ALTER TABLE entities ADD COLUMN resolution_run TEXT;
CREATE INDEX IF NOT EXISTS idx_entities_coalesce_state ON entities(coalesce_state);
CREATE INDEX IF NOT EXISTS idx_entities_cluster ON entities(cluster_id);

-- Review queue: candidate coalescing pairs + their band/verdict/review action.
CREATE TABLE IF NOT EXISTS entity_coalesce_candidates (
    uuid            TEXT PRIMARY KEY,
    entity_a        TEXT NOT NULL,
    entity_b        TEXT NOT NULL,
    name_a          TEXT,
    name_b          TEXT,
    cosine          REAL,
    fuzzy           INTEGER,
    band            TEXT,               -- 'merge' | 'needs_llm'
    verdict         TEXT,               -- 'merge' | 'related_not_same' | 'uncertain' | NULL
    resolution_run  TEXT,
    detected_at     TEXT,
    reviewed_at     TEXT,
    review_action   TEXT,               -- 'merge' | 'related' | 'reject' | 'defer' | 'unapplied'
    metadata        TEXT
);
CREATE INDEX IF NOT EXISTS idx_ecc_reviewed ON entity_coalesce_candidates(reviewed_at);

-- Name-hash-keyed embedding cache for the coalescing pass. Module-prefixed name
-- (entity_coalesce_embeddings) so it never collides with core 032's
-- entity_embeddings. embedding is a packed float32 BLOB (embedding_utils.pack);
-- name_hash lets a re-run skip re-embedding an unchanged name.
CREATE TABLE IF NOT EXISTS entity_coalesce_embeddings (
    entity_id   TEXT PRIMARY KEY,
    name_hash   TEXT NOT NULL,
    embedding   BLOB NOT NULL,
    dim         INTEGER NOT NULL,
    model       TEXT,
    created_at  TEXT
);
