-- pg_064_session_handoff_and_cohesion.up.sql
--
-- PostgreSQL counterpart of 056_session_handoff_and_cohesion.up.sql: two tables m3
-- created at first use, now owned by migrations (m3 schemas change only through
-- migrations, user decision 2026-10-13). The column types are the ones the runtime
-- DDL produced (dialect autoincrement/timestamp types), so an existing table is
-- adopted unchanged.

CREATE TABLE IF NOT EXISTS session_handoff (
    id         SERIAL PRIMARY KEY,
    project    TEXT,
    summary    TEXT,
    next_steps TEXT,
    timestamp  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS m3_system_cohesion (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
