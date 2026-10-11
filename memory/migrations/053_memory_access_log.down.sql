-- 053_memory_access_log.down.sql
-- Reverses 053: drops the read-access audit log.

DROP TABLE IF EXISTS memory_access_log;
