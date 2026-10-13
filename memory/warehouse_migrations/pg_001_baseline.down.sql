-- pg_001_baseline.down.sql
-- No-op: the warehouse holds every machine's replicated memories, and its tables
-- predate this chain. Dropping them would erase that data, which only gdpr_forget
-- may do (and only for a data subject).

SELECT 1;
