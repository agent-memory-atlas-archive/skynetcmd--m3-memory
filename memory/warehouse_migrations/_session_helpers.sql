-- _session_helpers.sql
--
-- Loaded by migrate_pg.run_warehouse_migrations once per session, before any
-- pending warehouse migration, so every pg_NNN file can use these helpers (a
-- warehouse already at v1 runs only the later files). Not a migration: the
-- runner's pg_*.sql discovery does not pick it up.

-- ── Change only what is missing ─────────────────────────────────────────────
-- PostgreSQL checks table ownership BEFORE it honours ADD COLUMN IF NOT EXISTS or
-- CREATE INDEX IF NOT EXISTS, so a warehouse whose tables another role created
-- (an admin, or postgres) refused even a no-op as "must be owner of table". These
-- helpers look in the catalog first and change only what is missing, so an
-- up-to-date warehouse needs no ownership. A change that IS needed on a table this
-- role does not own fails naming the table, its owner and the fix. pg_temp: they
-- vanish with the session and need no privilege beyond TEMP.
CREATE OR REPLACE FUNCTION pg_temp.m3_add_column(tbl text, col text, def text) RETURNS void
LANGUAGE plpgsql AS $fn$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = current_schema() AND table_name = tbl AND column_name = col
  ) THEN
    EXECUTE format('ALTER TABLE %I ADD COLUMN %I %s', tbl, col, def);
  END IF;
EXCEPTION WHEN insufficient_privilege THEN
  RAISE EXCEPTION 'warehouse table %.% needs column %, and role % does not own the table (owner: %). fix: ALTER TABLE %.% OWNER TO %;',
    current_schema(), tbl, col, current_user,
    (SELECT tableowner FROM pg_tables WHERE schemaname = current_schema() AND tablename = tbl),
    current_schema(), tbl, current_user
    USING ERRCODE = 'insufficient_privilege';
END $fn$;

CREATE OR REPLACE FUNCTION pg_temp.m3_create_index(idx text, tbl text, ddl text) RETURNS void
LANGUAGE plpgsql AS $fn$
BEGIN
  IF to_regclass(format('%I.%I', current_schema(), idx)) IS NULL THEN
    EXECUTE ddl;
  END IF;
EXCEPTION WHEN insufficient_privilege THEN
  RAISE EXCEPTION 'warehouse table %.% needs index %, and role % does not own the table (owner: %). fix: ALTER TABLE %.% OWNER TO %;',
    current_schema(), tbl, idx, current_user,
    (SELECT tableowner FROM pg_tables WHERE schemaname = current_schema() AND tablename = tbl),
    current_schema(), tbl, current_user
    USING ERRCODE = 'insufficient_privilege';
END $fn$;

-- pg_setup's auxiliary tables live in PUBLIC, where PostgreSQL 15+ lets only the
-- schema owner create, and the CREATE privilege is checked before IF NOT EXISTS.
-- Sync never touches them, so a missing one that this role cannot create is
-- skipped with a notice rather than blocking the warehouse migration.
CREATE OR REPLACE FUNCTION pg_temp.m3_create_aux(tbl text, ddl text) RETURNS void
LANGUAGE plpgsql AS $fn$
BEGIN
  IF to_regclass(format('public.%I', tbl)) IS NULL THEN
    EXECUTE ddl;
  END IF;
EXCEPTION WHEN insufficient_privilege THEN
  RAISE NOTICE 'skipped public.%: role % may not create in schema public (pg_setup auxiliary table; sync does not use it)', tbl, current_user;
END $fn$;

-- A trigger is created only when missing, for the same reason: CREATE TRIGGER needs
-- ownership even when the trigger already exists.
CREATE OR REPLACE FUNCTION pg_temp.m3_create_trigger(trg text, tbl text, ddl text) RETURNS void
LANGUAGE plpgsql AS $fn$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = current_schema() AND c.relname = tbl AND t.tgname = trg
  ) THEN
    EXECUTE ddl;
  END IF;
EXCEPTION WHEN insufficient_privilege THEN
  RAISE EXCEPTION 'warehouse table %.% needs trigger %, and role % does not own the table (owner: %). fix: ALTER TABLE %.% OWNER TO %;',
    current_schema(), tbl, trg, current_user,
    (SELECT tableowner FROM pg_tables WHERE schemaname = current_schema() AND tablename = tbl),
    current_schema(), tbl, current_user
    USING ERRCODE = 'insufficient_privilege';
END $fn$;
