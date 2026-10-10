-- pg_061_secret_backups.up.sql
--
-- PostgreSQL counterpart of 052_secret_backups.up.sql (rationale there): the
-- rotation-backup table, previously created at runtime by secret_rotator.

CREATE TABLE IF NOT EXISTS secret_backups (
    service_name    TEXT,
    encrypted_value TEXT,
    rotated_at      TEXT
);
