-- pg_060_enrichment_send_to.up.sql
--
-- PostgreSQL counterpart of 031_enrichment_send_to.up.sql (full rationale there).
-- `send_to` routes enrichment_groups rows to one provider; `m3_enrich --send-to`
-- claims only its assigned rows. Never ported, so a routed run on PostgreSQL
-- failed on the undefined column. NULL means unassigned.

ALTER TABLE enrichment_groups ADD COLUMN IF NOT EXISTS send_to TEXT;
CREATE INDEX IF NOT EXISTS idx_eg_send_to ON enrichment_groups(send_to, status);
