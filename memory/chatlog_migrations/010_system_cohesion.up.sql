-- 010_system_cohesion.up.sql
--
-- The CHATLOG lane's copy of main-lane 056's m3_system_cohesion: the salt
-- cohesion check runs on every SQLite store a context opens, chatlog included,
-- and created this table at runtime. m3 schemas change only through migrations
-- (user decision 2026-10-13). IF NOT EXISTS adopts a store that already has it.

CREATE TABLE IF NOT EXISTS m3_system_cohesion (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
