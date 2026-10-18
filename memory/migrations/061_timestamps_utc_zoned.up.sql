-- 061_timestamps_utc_zoned.up.sql
--
-- Every m3 timestamp is UTC and says so. The write paths now store zoned UTC
-- (m3_core.runtime.utc_stored); this fixes rows written before that:
--
--   bare date        '2026-04-18'           -> '2026-04-18T00:00:00+00:00'
--   no zone          '2026-04-18T09:30:00'  -> '2026-04-18T09:30:00+00:00'
--   (space form '2026-04-18 09:30:00' also gets its 'T')
--
-- A zoneless value was already read as UTC everywhere, so no row changes the
-- instant it means; it only stops being ambiguous. Values that carry 'Z' or an
-- offset are untouched. Idempotent: a second run matches nothing.
--
-- SQLite only. PostgreSQL stores these columns as TIMESTAMPTZ, which always
-- carries its zone, so it has no counterpart.

UPDATE memory_items SET valid_from = valid_from || 'T00:00:00+00:00'
 WHERE valid_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]';
UPDATE memory_items SET valid_to = valid_to || 'T00:00:00+00:00'
 WHERE valid_to GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]';
UPDATE memory_items SET expires_at = expires_at || 'T00:00:00+00:00'
 WHERE expires_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]';

-- No zone: date, 'T' or space, hh:mm:ss, an optional fraction, and nothing
-- after the last digit ('Z' and '+hh:mm'/'-hh:mm' offsets end otherwise or
-- carry a sign six from the end).
UPDATE memory_items SET valid_from = replace(valid_from, ' ', 'T') || '+00:00'
 WHERE valid_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T ][0-9][0-9]:[0-9][0-9]:[0-9][0-9]*'
   AND valid_from GLOB '*[0-9]' AND substr(valid_from, -6, 1) NOT IN ('+', '-');
UPDATE memory_items SET valid_to = replace(valid_to, ' ', 'T') || '+00:00'
 WHERE valid_to GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T ][0-9][0-9]:[0-9][0-9]:[0-9][0-9]*'
   AND valid_to GLOB '*[0-9]' AND substr(valid_to, -6, 1) NOT IN ('+', '-');
UPDATE memory_items SET expires_at = replace(expires_at, ' ', 'T') || '+00:00'
 WHERE expires_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T ][0-9][0-9]:[0-9][0-9]:[0-9][0-9]*'
   AND expires_at GLOB '*[0-9]' AND substr(expires_at, -6, 1) NOT IN ('+', '-');
UPDATE memory_items SET created_at = replace(created_at, ' ', 'T') || '+00:00'
 WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T ][0-9][0-9]:[0-9][0-9]:[0-9][0-9]*'
   AND created_at GLOB '*[0-9]' AND substr(created_at, -6, 1) NOT IN ('+', '-');
UPDATE memory_items SET updated_at = replace(updated_at, ' ', 'T') || '+00:00'
 WHERE updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T ][0-9][0-9]:[0-9][0-9]:[0-9][0-9]*'
   AND updated_at GLOB '*[0-9]' AND substr(updated_at, -6, 1) NOT IN ('+', '-');
