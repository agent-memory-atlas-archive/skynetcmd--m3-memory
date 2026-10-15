-- 059_fts_update_on_text_only.down.sql
-- Restores the trigger that reindexes on an update of any column.

DROP TRIGGER IF EXISTS mi_fts_update;
CREATE TRIGGER mi_fts_update AFTER UPDATE ON memory_items BEGIN
    INSERT INTO memory_items_fts(memory_items_fts, rowid, title, content)
    VALUES ('delete', old.rowid, old.title, old.content);
    INSERT INTO memory_items_fts(rowid, title, content)
    VALUES (new.rowid, new.title, new.content);
END;
