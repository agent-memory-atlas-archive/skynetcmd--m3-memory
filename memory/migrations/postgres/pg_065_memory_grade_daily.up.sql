-- pg_065_memory_grade_daily.up.sql
--
-- PostgreSQL counterpart of 058_memory_grade_daily.up.sql: one row per UTC day of
-- memory_grade verdicts accepted and rejected as stale (memory credit loop part A).

CREATE TABLE IF NOT EXISTS memory_grade_daily (
    day            TEXT PRIMARY KEY,               -- UTC date, YYYY-MM-DD
    accepted       INTEGER NOT NULL DEFAULT 0,
    rejected_stale INTEGER NOT NULL DEFAULT 0
);
