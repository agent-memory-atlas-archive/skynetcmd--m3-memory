-- pg_064_session_handoff_and_cohesion.down.sql
-- No-op: both tables predate pg_064 on existing stores and hold data (handoffs,
-- the salt hash); dropping them would erase it, which only gdpr_forget may do.

SELECT 1;
