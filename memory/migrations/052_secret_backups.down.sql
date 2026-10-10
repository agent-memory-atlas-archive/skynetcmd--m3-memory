-- 052_secret_backups.down.sql
-- Reverses 052. Drops the rotation backups (the previous encrypted values);
-- the live secrets in synchronized_secrets are untouched.

DROP TABLE IF EXISTS secret_backups;
