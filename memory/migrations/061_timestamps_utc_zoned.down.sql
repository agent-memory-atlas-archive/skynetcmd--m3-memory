-- 061_timestamps_utc_zoned.down.sql
--
-- No-op. The up migration only added the UTC zone a value was already read
-- with, so every converted row still means the same instant; and once
-- converted, a row cannot be told apart from one written zoned. Stripping
-- zones would corrupt the latter. The pre-migration file backup is the
-- byte-level way back.

SELECT 1;
