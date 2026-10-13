-- 058_memory_grade_daily.down.sql
-- No-op: the table holds the grading record; dropping it would erase data, which
-- only gdpr_forget may do. A re-applied 058 is a no-op (IF NOT EXISTS).

SELECT 1;
