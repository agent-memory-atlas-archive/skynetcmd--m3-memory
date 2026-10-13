-- 058_memory_grade_daily.up.sql
--
-- One row per UTC day: memory_grade verdicts accepted and rejected as stale (outside
-- the feedback window). memory_feedback_stats and memory_lifecycle_summary read it,
-- so the grading signal survives a restart (memory credit loop part A, user
-- decision 2026-10-12). Per day, so a tuning change shows as a change between days
-- instead of blending into one running total.

CREATE TABLE IF NOT EXISTS memory_grade_daily (
    day            TEXT PRIMARY KEY,               -- UTC date, YYYY-MM-DD
    accepted       INTEGER NOT NULL DEFAULT 0,
    rejected_stale INTEGER NOT NULL DEFAULT 0
);
