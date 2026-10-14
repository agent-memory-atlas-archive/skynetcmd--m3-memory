-- pg_066_pgvector_column.up.sql
--
-- Optional pgvector support: a `vector` copy of
-- each embedding beside the portable BYTEA, so an exact search can score inside
-- PostgreSQL (`<=>`) instead of shipping every vector to the client. BYTEA stays the
-- source of truth; m3 writes both, and a batch backfill fills existing rows.
--
-- Conditional by design: on PostgreSQL WITHOUT pgvector (vanilla or locked-down
-- servers) this migration changes nothing and m3 keeps its baseline exact search. On
-- a server where pgvector is available but this role may not create it, it notes
-- that and changes nothing. The body is idempotent, so it can be applied again
-- once a DBA has run `CREATE EXTENSION vector` (the version is already recorded,
-- so a plain `up` will not re-run it).

-- A writer that changes `embedding` without also writing `embedding_vec` (an older
-- client, a hand-run UPDATE) must not leave a stale copy for search to score. The
-- trigger clears the copy instead: the row drops to the backfill's work list and
-- search stays on the baseline path until it is refilled. Created unconditionally
-- (PL/pgSQL resolves NEW.embedding_vec only when it runs); attached only below,
-- where the column exists.
CREATE OR REPLACE FUNCTION m3_embedding_vec_invalidate() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.embedding IS DISTINCT FROM OLD.embedding
       AND NEW.embedding_vec IS NOT DISTINCT FROM OLD.embedding_vec THEN
        NEW.embedding_vec := NULL;
    END IF;
    RETURN NEW;
END $fn$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')
       AND NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        BEGIN
            CREATE EXTENSION IF NOT EXISTS vector;
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE NOTICE 'm3 pg_066: pgvector is available but this role cannot create it; '
                         'search stays on the baseline path. fix: as a superuser, CREATE EXTENSION vector; then m3 doctor --fix';
        END;
    END IF;

    -- The core table, and the chat log's clone where the chat log shares this
    -- database (chat_log_embeddings): a clone that lacked the column would keep
    -- the chat log's searches on the baseline path for good. Each index lists
    -- the rows still lacking a vector: the backfill's work list, and the check
    -- that decides whether search may use the pgvector path.
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS embedding_vec vector;
        CREATE INDEX IF NOT EXISTS idx_memory_embeddings_vec_missing
            ON memory_embeddings (memory_id) WHERE embedding_vec IS NULL;
        DROP TRIGGER IF EXISTS trg_memory_embeddings_vec_invalidate ON memory_embeddings;
        CREATE TRIGGER trg_memory_embeddings_vec_invalidate
            BEFORE UPDATE ON memory_embeddings
            FOR EACH ROW EXECUTE FUNCTION m3_embedding_vec_invalidate();

        IF to_regclass('chat_log_embeddings') IS NOT NULL THEN
            ALTER TABLE chat_log_embeddings ADD COLUMN IF NOT EXISTS embedding_vec vector;
            CREATE INDEX IF NOT EXISTS idx_chat_log_embeddings_vec_missing
                ON chat_log_embeddings (memory_id) WHERE embedding_vec IS NULL;
            DROP TRIGGER IF EXISTS trg_chat_log_embeddings_vec_invalidate ON chat_log_embeddings;
            CREATE TRIGGER trg_chat_log_embeddings_vec_invalidate
                BEFORE UPDATE ON chat_log_embeddings
                FOR EACH ROW EXECUTE FUNCTION m3_embedding_vec_invalidate();
        END IF;
    END IF;
END $$;
