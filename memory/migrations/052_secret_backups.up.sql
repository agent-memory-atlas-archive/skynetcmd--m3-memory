-- 052_secret_backups.up.sql
--
-- The table secret_rotator writes the previous encrypted value into before it
-- rotates a secret. It was created at runtime by secret_rotator itself; the
-- migration chain now owns it, like every other table (one definition per
-- backend: PostgreSQL pg_061). Columns are unchanged.
--
-- IF NOT EXISTS: stores that already rotated a secret have the table from the
-- runtime DDL this replaces.

CREATE TABLE IF NOT EXISTS secret_backups (
    service_name    TEXT,
    encrypted_value TEXT,
    rotated_at      TEXT
);
