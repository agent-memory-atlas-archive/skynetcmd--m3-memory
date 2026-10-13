-- 007_observation_queue.down.sql
-- Reverses 007, as main-lane 025's down does: the queues are work queues, not
-- memories.

DROP INDEX IF EXISTS idx_mi_type_user_obs;
DROP INDEX IF EXISTS idx_rq_attempts;
DROP INDEX IF EXISTS idx_rq_user_conv;
DROP TABLE IF EXISTS reflector_queue;
DROP INDEX IF EXISTS idx_oq_attempts;
DROP INDEX IF EXISTS idx_oq_conversation_id;
DROP TABLE IF EXISTS observation_queue;
