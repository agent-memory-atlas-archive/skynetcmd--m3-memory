-- pg_060_enrichment_send_to.down.sql
-- Reverses pg_060. Loses provider routing only; every row becomes claimable by
-- an unrouted run.

DROP INDEX IF EXISTS idx_eg_send_to;
ALTER TABLE enrichment_groups DROP COLUMN IF EXISTS send_to;
