-- pg_066_pgvector_column.down.sql
-- The vector column is a derived copy of the BYTEA embedding (the source of truth), so
-- dropping it loses no data. The extension itself is left installed: other databases
-- or tools on the server may use it.

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['memory_embeddings', 'chat_log_embeddings'] LOOP
        CONTINUE WHEN to_regclass(t) IS NULL;
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', 'trg_' || t || '_vec_invalidate', t);
        EXECUTE format('DROP INDEX IF EXISTS %I', 'idx_' || t || '_vec_missing');
        EXECUTE format('ALTER TABLE %I DROP COLUMN IF EXISTS embedding_vec', t);
    END LOOP;
END $$;

DROP FUNCTION IF EXISTS m3_embedding_vec_invalidate();
