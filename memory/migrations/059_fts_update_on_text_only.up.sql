-- 059_fts_update_on_text_only.up.sql
--
-- Reindex FTS only when title or content changes. mi_fts_update fired on an
-- update of ANY column, so a search's access stamps (access_count,
-- last_accessed_at) and every decay or grading pass rewrote the row's FTS
-- entry, and the index merges that follow stalled a search for up to ~240 ms
-- (a copy of a live store, 200 rounds of 8 stamps: 389 ms -> 56 ms in all,
-- worst 238 ms -> 1.4 ms; 2026-10-15). The FTS table indexes only title and
-- content.

DROP TRIGGER IF EXISTS mi_fts_update;
CREATE TRIGGER mi_fts_update AFTER UPDATE OF title, content ON memory_items BEGIN
    INSERT INTO memory_items_fts(memory_items_fts, rowid, title, content)
    VALUES ('delete', old.rowid, old.title, old.content);
    INSERT INTO memory_items_fts(rowid, title, content)
    VALUES (new.rowid, new.title, new.content);
END;
