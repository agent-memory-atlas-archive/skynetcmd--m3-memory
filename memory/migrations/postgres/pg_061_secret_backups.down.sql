-- pg_061_secret_backups.down.sql
-- Reverses pg_061. Drops the rotation backups; synchronized_secrets is untouched.

DROP TABLE IF EXISTS secret_backups;
