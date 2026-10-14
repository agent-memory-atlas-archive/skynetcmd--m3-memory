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
-- current shape, then every column an older shape may lack is ADD COLUMN IF NOT
-- EXISTS. Column TYPES are the current ones (pg_warehouse_chatlog_v1); an existing
-- column keeps its type (additive only: e.g. valid_from stays TEXT where pg_sync
-- once added it as TEXT). Constraints are not retrofitted onto existing tables.
--
-- Later warehouse changes are pg_002..., additive only, so a machine still on an
-- older m3 keeps syncing against a newer warehouse.

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
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS content TEXT;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS metadata_json JSONB;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS agent_id TEXT;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS model_id TEXT;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS change_agent TEXT DEFAULT 'unknown';
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS importance DOUBLE PRECISION DEFAULT 0.5;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'agent';
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS origin_device TEXT DEFAULT 'macbook';
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS is_deleted INTEGER DEFAULT 0;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS decay_rate DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS access_count BIGINT DEFAULT 0;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS user_id TEXT;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS scope TEXT;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS read_at TIMESTAMPTZ;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS conversation_id TEXT;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS refresh_on TIMESTAMPTZ;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS refresh_reason TEXT;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS variant TEXT;
CREATE INDEX IF NOT EXISTS idx_memory_items_updated_at ON memory_items(updated_at);
CREATE INDEX IF NOT EXISTS idx_mi_type ON memory_items(type);
CREATE INDEX IF NOT EXISTS idx_mi_agent ON memory_items(agent_id);
CREATE INDEX IF NOT EXISTS idx_mi_model ON memory_items(model_id);
CREATE INDEX IF NOT EXISTS idx_mi_created ON memory_items(created_at);
CREATE INDEX IF NOT EXISTS idx_mi_deleted ON memory_items(is_deleted);
CREATE INDEX IF NOT EXISTS idx_mi_conversation_composite ON memory_items(conversation_id, created_at) WHERE is_deleted = 0;
CREATE INDEX IF NOT EXISTS idx_memory_items_chat_log ON memory_items(conversation_id, created_at) WHERE type = 'chat_log' AND is_deleted = 0;

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
ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS embed_model TEXT DEFAULT 'jina-embeddings-v5';
ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS dim BIGINT DEFAULT 1024;
ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS vector_kind TEXT NOT NULL DEFAULT 'default';
ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS is_deleted INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_memory_embeddings_updated_at ON memory_embeddings(updated_at);
CREATE INDEX IF NOT EXISTS idx_me_memory_id ON memory_embeddings(memory_id);
CREATE INDEX IF NOT EXISTS idx_me_content_hash_model ON memory_embeddings(content_hash, embed_model);
CREATE INDEX IF NOT EXISTS idx_me_memory_kind ON memory_embeddings(memory_id, vector_kind);

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
ALTER TABLE memory_relationships ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE memory_relationships ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE memory_relationships ADD COLUMN IF NOT EXISTS is_deleted INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memory_relationships ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_memory_relationships_updated_at ON memory_relationships(updated_at);
CREATE INDEX IF NOT EXISTS idx_mr_from ON memory_relationships(from_id);
CREATE INDEX IF NOT EXISTS idx_mr_to ON memory_relationships(to_id);

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
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS deleted_at TEXT;
CREATE INDEX IF NOT EXISTS idx_tasks_owner_state ON tasks(owner_agent, state);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_deleted_at ON tasks(deleted_at);

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
CREATE INDEX IF NOT EXISTS idx_gdpr_subject ON gdpr_requests(subject_id);

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
CREATE TABLE IF NOT EXISTS public.activity_logs (
  id         SERIAL PRIMARY KEY,
  timestamp  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
  query      TEXT,
  response   TEXT,
  model_used TEXT DEFAULT 'DeepSeek-R1-70B'
);
CREATE TABLE IF NOT EXISTS public.project_decisions (
  id        SERIAL PRIMARY KEY,
  project   TEXT,
  decision  TEXT,
  rationale TEXT,
  timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS public.hardware_specs (
  id        SERIAL PRIMARY KEY,
  component TEXT,
  spec      TEXT,
  timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS public.system_focus (
  id        INTEGER PRIMARY KEY,
  summary   TEXT,
  timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
