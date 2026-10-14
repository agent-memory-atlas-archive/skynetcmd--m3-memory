-- 056_session_handoff_and_cohesion.up.sql
--
-- Two tables m3 created at first use, now owned by migrations (m3 schemas change
-- only through migrations). One definition per backend
-- (PostgreSQL pg_064). An existing store already has them in this shape: every
-- statement is IF NOT EXISTS, so this migration adopts them unchanged.
--
--   session_handoff     save_handoff / get_handoff (bin/session_handoff.py)
--   m3_system_cohesion  the device-salt hash a store was first opened with
--                       (m3_core.context.verify_salt_cohesion)

CREATE TABLE IF NOT EXISTS session_handoff (
    id         INTEGER PRIMARY KEY,
    project    TEXT,
    summary    TEXT,
    next_steps TEXT,
    timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS m3_system_cohesion (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
