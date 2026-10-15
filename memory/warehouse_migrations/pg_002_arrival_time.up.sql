-- pg_002_arrival_time.up.sql
--
-- When each row reached the warehouse, stamped by the warehouse itself.
--
-- Pulls used to ask for rows created or updated after the previous pull. Those
-- times come from the machine that wrote the row, and a row reaches the warehouse
-- only at that machine's next push, so a row written before another machine's pull
-- and pushed after it was older than that machine's next watermark and was never
-- pulled there. wh_arrived_at is set by a trigger on every insert and update,
-- whichever m3 version pushed the row, and pulls ask for rows that ARRIVED since.
--
-- Existing rows keep wh_arrived_at NULL (adding a nullable column rewrites
-- nothing): a machine's first pull on this version reads everything once, which
-- also brings it every row the old pulls missed.

-- Created only when missing: replacing an existing function needs its ownership.
DO $do$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = current_schema() AND p.proname = 'm3_stamp_arrival'
  ) THEN
    EXECUTE $f$CREATE FUNCTION m3_stamp_arrival() RETURNS trigger
      LANGUAGE plpgsql AS $fn$
      BEGIN
        NEW.wh_arrived_at := clock_timestamp();
        RETURN NEW;
      END $fn$$f$;
  END IF;
END $do$;

SELECT pg_temp.m3_add_column('memory_items', 'wh_arrived_at', $d$TIMESTAMPTZ$d$);
SELECT pg_temp.m3_add_column('memory_relationships', 'wh_arrived_at', $d$TIMESTAMPTZ$d$);
SELECT pg_temp.m3_add_column('memory_embeddings', 'wh_arrived_at', $d$TIMESTAMPTZ$d$);

SELECT pg_temp.m3_create_trigger('m3_items_arrival', 'memory_items',
  $d$CREATE TRIGGER m3_items_arrival BEFORE INSERT OR UPDATE ON memory_items
     FOR EACH ROW EXECUTE FUNCTION m3_stamp_arrival()$d$);
SELECT pg_temp.m3_create_trigger('m3_relationships_arrival', 'memory_relationships',
  $d$CREATE TRIGGER m3_relationships_arrival BEFORE INSERT OR UPDATE ON memory_relationships
     FOR EACH ROW EXECUTE FUNCTION m3_stamp_arrival()$d$);
SELECT pg_temp.m3_create_trigger('m3_embeddings_arrival', 'memory_embeddings',
  $d$CREATE TRIGGER m3_embeddings_arrival BEFORE INSERT OR UPDATE ON memory_embeddings
     FOR EACH ROW EXECUTE FUNCTION m3_stamp_arrival()$d$);

-- Partial: rows from before this migration (NULL) are read only by a full pull.
SELECT pg_temp.m3_create_index('idx_memory_items_arrived', 'memory_items',
  $d$CREATE INDEX idx_memory_items_arrived ON memory_items (wh_arrived_at) WHERE wh_arrived_at IS NOT NULL$d$);
SELECT pg_temp.m3_create_index('idx_memory_relationships_arrived', 'memory_relationships',
  $d$CREATE INDEX idx_memory_relationships_arrived ON memory_relationships (wh_arrived_at) WHERE wh_arrived_at IS NOT NULL$d$);
SELECT pg_temp.m3_create_index('idx_memory_embeddings_arrived', 'memory_embeddings',
  $d$CREATE INDEX idx_memory_embeddings_arrived ON memory_embeddings (wh_arrived_at) WHERE wh_arrived_at IS NOT NULL$d$);
