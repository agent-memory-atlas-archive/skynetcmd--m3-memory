-- pg_001_baseline.up.sql
--
-- The warehouse schema as one baseline: a migration chain applied by
-- pg_sync, which migrates an older warehouse in place. It unifies the three
-- earlier sources: bin/pg_setup.py's core tables, pg_warehouse_chatlog_v1.sql, and
-- the DDL pg_sync used to issue on every run.
--
-- UNQUALIFIED: the runner sets the warehouse's schema as search_path (m3_warehouse,
-- or public on an older layout), so this file serves both.
--
-- ADOPTS ANY EXISTING WAREHOUSE: every table is CREATE ... IF NOT EXISTS in the
-- current shape, then every column an older shape may lack is added if missing
-- (pg_temp.m3_add_column; see 'Change only what is missing' below). Column TYPES are the current ones (pg_warehouse_chatlog_v1); an existing
-- column keeps its type (additive only: e.g. valid_from stays TEXT where pg_sync
-- once added it as TEXT). Constraints are not retrofitted onto existing tables.
--
-- Later warehouse changes are pg_002..., additive only, so a machine still on an
-- older m3 keeps syncing against a newer warehouse.

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

CREATE TABLE IF NOT EXISTS schema_versions (
  version    BIGINT PRIMARY KEY,
  filename   TEXT NOT NULL,
  applied_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── memory_items ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS memory_items (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  title TEXT,
  content TEXT,
  metadata_json JSONB,
  agent_id TEXT,
  model_id TEXT,
  change_agent TEXT DEFAULT 'unknown',
  importance DOUBLE PRECISION DEFAULT 0.5,
  source TEXT DEFAULT 'agent',
  origin_device TEXT DEFAULT 'macbook',
  is_deleted INTEGER DEFAULT 0,
  expires_at TIMESTAMPTZ,
  decay_rate DOUBLE PRECISION DEFAULT 0.0,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  last_accessed_at TIMESTAMPTZ,
  access_count BIGINT DEFAULT 0,
  user_id TEXT,
  scope TEXT,
  valid_from TIMESTAMPTZ,
  valid_to TIMESTAMPTZ,
  content_hash TEXT,
  read_at TIMESTAMPTZ,
  conversation_id TEXT,
  refresh_on TIMESTAMPTZ,
  refresh_reason TEXT,
  variant TEXT
);
SELECT pg_temp.m3_add_column('memory_items', 'title', $d$TEXT$d$);
SELECT pg_temp.m3_add_column('memory_items', 'content', $d$TEXT$d$);
SELECT pg_temp.m3_add_column('memory_items', 'metadata_json', $d$JSONB$d$);
SELECT pg_temp.m3_add_column('memory_items', 'agent_id', $d$TEXT$d$);
SELECT pg_temp.m3_add_column('memory_items', 'model_id', $d$TEXT$d$);
SELECT pg_temp.m3_add_column('memory_items', 'change_agent', $d$TEXT DEFAULT 'unknown'$d$);
SELECT pg_temp.m3_add_column('memory_items', 'importance', $d$DOUBLE PRECISION DEFAULT 0.5$d$);
SELECT pg_temp.m3_add_column('memory_items', 'source', $d$TEXT DEFAULT 'agent'$d$);
SELECT pg_temp.m3_add_column('memory_items', 'origin_device', $d$TEXT DEFAULT 'macbook'$d$);
SELECT pg_temp.m3_add_column('memory_items', 'is_deleted', $d$INTEGER DEFAULT 0$d$);
SELECT pg_temp.m3_add_column('memory_items', 'expires_at', $d$TIMESTAMPTZ$d$);
SELECT pg_temp.m3_add_column('memory_items', 'decay_rate', $d$DOUBLE PRECISION DEFAULT 0.0$d$);
SELECT pg_temp.m3_add_column('memory_items', 'created_at', $d$TIMESTAMPTZ DEFAULT NOW()$d$);
SELECT pg_temp.m3_add_column('memory_items', 'updated_at', $d$TIMESTAMPTZ DEFAULT NOW()$d$);
SELECT pg_temp.m3_add_column('memory_items', 'last_accessed_at', $d$TIMESTAMPTZ$d$);
SELECT pg_temp.m3_add_column('memory_items', 'access_count', $d$BIGINT DEFAULT 0$d$);
SELECT pg_temp.m3_add_column('memory_items', 'user_id', $d$TEXT$d$);
SELECT pg_temp.m3_add_column('memory_items', 'scope', $d$TEXT$d$);
SELECT pg_temp.m3_add_column('memory_items', 'valid_from', $d$TIMESTAMPTZ$d$);
SELECT pg_temp.m3_add_column('memory_items', 'valid_to', $d$TIMESTAMPTZ$d$);
SELECT pg_temp.m3_add_column('memory_items', 'content_hash', $d$TEXT$d$);
SELECT pg_temp.m3_add_column('memory_items', 'read_at', $d$TIMESTAMPTZ$d$);
SELECT pg_temp.m3_add_column('memory_items', 'conversation_id', $d$TEXT$d$);
SELECT pg_temp.m3_add_column('memory_items', 'refresh_on', $d$TIMESTAMPTZ$d$);
SELECT pg_temp.m3_add_column('memory_items', 'refresh_reason', $d$TEXT$d$);
SELECT pg_temp.m3_add_column('memory_items', 'variant', $d$TEXT$d$);
SELECT pg_temp.m3_create_index('idx_memory_items_updated_at', 'memory_items', $d$CREATE INDEX IF NOT EXISTS idx_memory_items_updated_at ON memory_items(updated_at)$d$);
SELECT pg_temp.m3_create_index('idx_mi_type', 'memory_items', $d$CREATE INDEX IF NOT EXISTS idx_mi_type ON memory_items(type)$d$);
SELECT pg_temp.m3_create_index('idx_mi_agent', 'memory_items', $d$CREATE INDEX IF NOT EXISTS idx_mi_agent ON memory_items(agent_id)$d$);
SELECT pg_temp.m3_create_index('idx_mi_model', 'memory_items', $d$CREATE INDEX IF NOT EXISTS idx_mi_model ON memory_items(model_id)$d$);
SELECT pg_temp.m3_create_index('idx_mi_created', 'memory_items', $d$CREATE INDEX IF NOT EXISTS idx_mi_created ON memory_items(created_at)$d$);
SELECT pg_temp.m3_create_index('idx_mi_deleted', 'memory_items', $d$CREATE INDEX IF NOT EXISTS idx_mi_deleted ON memory_items(is_deleted)$d$);
SELECT pg_temp.m3_create_index('idx_mi_conversation_composite', 'memory_items', $d$CREATE INDEX IF NOT EXISTS idx_mi_conversation_composite ON memory_items(conversation_id, created_at) WHERE is_deleted = 0$d$);
SELECT pg_temp.m3_create_index('idx_memory_items_chat_log', 'memory_items', $d$CREATE INDEX IF NOT EXISTS idx_memory_items_chat_log ON memory_items(conversation_id, created_at) WHERE type = 'chat_log' AND is_deleted = 0$d$);

-- ── memory_embeddings ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS memory_embeddings (
  id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
  embedding BYTEA NOT NULL,
  embed_model TEXT DEFAULT 'jina-embeddings-v5',
  dim BIGINT DEFAULT 1024,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  content_hash TEXT,
  vector_kind TEXT NOT NULL DEFAULT 'default',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  is_deleted INTEGER NOT NULL DEFAULT 0,
  deleted_at TIMESTAMPTZ
);
SELECT pg_temp.m3_add_column('memory_embeddings', 'embed_model', $d$TEXT DEFAULT 'jina-embeddings-v5'$d$);
SELECT pg_temp.m3_add_column('memory_embeddings', 'dim', $d$BIGINT DEFAULT 1024$d$);
SELECT pg_temp.m3_add_column('memory_embeddings', 'created_at', $d$TIMESTAMPTZ DEFAULT NOW()$d$);
SELECT pg_temp.m3_add_column('memory_embeddings', 'content_hash', $d$TEXT$d$);
SELECT pg_temp.m3_add_column('memory_embeddings', 'vector_kind', $d$TEXT NOT NULL DEFAULT 'default'$d$);
SELECT pg_temp.m3_add_column('memory_embeddings', 'updated_at', $d$TIMESTAMPTZ NOT NULL DEFAULT NOW()$d$);
SELECT pg_temp.m3_add_column('memory_embeddings', 'is_deleted', $d$INTEGER NOT NULL DEFAULT 0$d$);
SELECT pg_temp.m3_add_column('memory_embeddings', 'deleted_at', $d$TIMESTAMPTZ$d$);
SELECT pg_temp.m3_create_index('idx_memory_embeddings_updated_at', 'memory_embeddings', $d$CREATE INDEX IF NOT EXISTS idx_memory_embeddings_updated_at ON memory_embeddings(updated_at)$d$);
SELECT pg_temp.m3_create_index('idx_me_memory_id', 'memory_embeddings', $d$CREATE INDEX IF NOT EXISTS idx_me_memory_id ON memory_embeddings(memory_id)$d$);
SELECT pg_temp.m3_create_index('idx_me_content_hash_model', 'memory_embeddings', $d$CREATE INDEX IF NOT EXISTS idx_me_content_hash_model ON memory_embeddings(content_hash, embed_model)$d$);
SELECT pg_temp.m3_create_index('idx_me_memory_kind', 'memory_embeddings', $d$CREATE INDEX IF NOT EXISTS idx_me_memory_kind ON memory_embeddings(memory_id, vector_kind)$d$);

-- ── memory_relationships ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS memory_relationships (
  id TEXT PRIMARY KEY,
  from_id TEXT NOT NULL REFERENCES memory_items(id),
  to_id TEXT NOT NULL REFERENCES memory_items(id),
  relationship_type TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  is_deleted INTEGER NOT NULL DEFAULT 0,
  deleted_at TIMESTAMPTZ
);
SELECT pg_temp.m3_add_column('memory_relationships', 'created_at', $d$TIMESTAMPTZ DEFAULT NOW()$d$);
SELECT pg_temp.m3_add_column('memory_relationships', 'updated_at', $d$TIMESTAMPTZ NOT NULL DEFAULT NOW()$d$);
SELECT pg_temp.m3_add_column('memory_relationships', 'is_deleted', $d$INTEGER NOT NULL DEFAULT 0$d$);
SELECT pg_temp.m3_add_column('memory_relationships', 'deleted_at', $d$TIMESTAMPTZ$d$);
SELECT pg_temp.m3_create_index('idx_memory_relationships_updated_at', 'memory_relationships', $d$CREATE INDEX IF NOT EXISTS idx_memory_relationships_updated_at ON memory_relationships(updated_at)$d$);
SELECT pg_temp.m3_create_index('idx_mr_from', 'memory_relationships', $d$CREATE INDEX IF NOT EXISTS idx_mr_from ON memory_relationships(from_id)$d$);
SELECT pg_temp.m3_create_index('idx_mr_to', 'memory_relationships', $d$CREATE INDEX IF NOT EXISTS idx_mr_to ON memory_relationships(to_id)$d$);

-- ── tasks (pg_sync's tombstone-aware shape) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS tasks (
  id                TEXT PRIMARY KEY,
  title             TEXT NOT NULL,
  description       TEXT DEFAULT '',
  state             TEXT NOT NULL DEFAULT 'pending',
  owner_agent       TEXT,
  created_by        TEXT NOT NULL,
  parent_task_id    TEXT,
  result_memory_id  TEXT,
  metadata_json     TEXT DEFAULT '{}',
  created_at        TEXT,
  updated_at        TEXT,
  completed_at      TEXT,
  deleted_at        TEXT
);
SELECT pg_temp.m3_add_column('tasks', 'deleted_at', $d$TEXT$d$);
SELECT pg_temp.m3_create_index('idx_tasks_owner_state', 'tasks', $d$CREATE INDEX IF NOT EXISTS idx_tasks_owner_state ON tasks(owner_agent, state)$d$);
SELECT pg_temp.m3_create_index('idx_tasks_parent', 'tasks', $d$CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id)$d$);
SELECT pg_temp.m3_create_index('idx_tasks_state', 'tasks', $d$CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state)$d$);
SELECT pg_temp.m3_create_index('idx_tasks_deleted_at', 'tasks', $d$CREATE INDEX IF NOT EXISTS idx_tasks_deleted_at ON tasks(deleted_at)$d$);

-- ── tier tables ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_retention_policies (
  agent_id     TEXT PRIMARY KEY,
  max_memories INTEGER DEFAULT 1000,
  ttl_days     INTEGER DEFAULT 0,
  auto_archive INTEGER DEFAULT 1,
  created_at   TIMESTAMPTZ DEFAULT NOW(),
  updated_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS gdpr_requests (
  id             TEXT PRIMARY KEY,
  subject_id     TEXT NOT NULL,
  request_type   TEXT NOT NULL,
  status         TEXT DEFAULT 'pending',
  items_affected INTEGER DEFAULT 0,
  requested_at   TIMESTAMPTZ DEFAULT NOW(),
  completed_at   TIMESTAMPTZ
);
SELECT pg_temp.m3_create_index('idx_gdpr_subject', 'gdpr_requests', $d$CREATE INDEX IF NOT EXISTS idx_gdpr_subject ON gdpr_requests(subject_id)$d$);

-- ── synchronized secrets, SQLite stats mirror ────────────────────────────────
CREATE TABLE IF NOT EXISTS synchronized_secrets (
  service_name    TEXT PRIMARY KEY,
  encrypted_value TEXT NOT NULL,
  version         BIGINT DEFAULT 1,
  origin_device   TEXT,
  updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS sqlite_stat1 (
  tbl  TEXT,
  idx  TEXT,
  stat TEXT
);

-- ── pg_setup.py's auxiliary tables: in PUBLIC, qualified ─────────────────────
-- Their readers (audit trail, weekly auditor, mission control) use the default
-- search_path; an unqualified copy in m3_warehouse would split their data.
SELECT pg_temp.m3_create_aux('activity_logs', $d$CREATE TABLE IF NOT EXISTS public.activity_logs (
  id         SERIAL PRIMARY KEY,
  timestamp  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
  query      TEXT,
  response   TEXT,
  model_used TEXT DEFAULT 'DeepSeek-R1-70B'
)$d$);
SELECT pg_temp.m3_create_aux('project_decisions', $d$CREATE TABLE IF NOT EXISTS public.project_decisions (
  id        SERIAL PRIMARY KEY,
  project   TEXT,
  decision  TEXT,
  rationale TEXT,
  timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
)$d$);
SELECT pg_temp.m3_create_aux('hardware_specs', $d$CREATE TABLE IF NOT EXISTS public.hardware_specs (
  id        SERIAL PRIMARY KEY,
  component TEXT,
  spec      TEXT,
  timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
)$d$);
SELECT pg_temp.m3_create_aux('system_focus', $d$CREATE TABLE IF NOT EXISTS public.system_focus (
  id        INTEGER PRIMARY KEY,
  summary   TEXT,
  timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
)$d$);
