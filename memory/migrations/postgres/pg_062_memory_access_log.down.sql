-- pg_062_memory_access_log.down.sql
-- Reverses pg_062: drops the read-access audit log.

DROP TABLE IF EXISTS memory_access_log;
