---
tool: bin/m3_chatlog_enrich_backfill.py
sha1: 6d7989a503ca
mtime_utc: 2026-10-10T15:07:57.463339+00:00
generated_utc: 2026-10-10T15:13:47.216080+00:00
private: false
---

# bin/m3_chatlog_enrich_backfill.py

## Purpose

Backfill `observation_queue` from existing chatlog rows.

Scans `memory_items WHERE type='chat_log'` in the chatlog DB, groups by
conversation_id, and INSERTs one row per conversation into `observation_queue`
of the main DB. Order is reverse-chronological (newest conversations first),
so when the periodic drain runs it processes the most recent material first.

Idempotent: the dialect's insert-or-ignore, so re-running won't duplicate
enqueued work for conversations still in the queue. Conversations already drained out
of the queue WILL be re-enqueued — use `--skip-already-enriched` to also
exclude conversations that already produced observations under the configured
target variant.

Every backend: on SQLite the chatlog is its own file (--chatlog-db) and the
queue is in the main file (--main-db); on PostgreSQL both are tables of the one
configured database and the path flags are refused.

---

## Entry points

- `def main()` (line 123)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--main-db` | Main SQLite DB (default: the active store). Not used on PostgreSQL. | None |  | str |  |
| `--chatlog-db` | Chatlog SQLite DB (default: the configured chatlog store). Not used on PostgreSQL. | None |  | str |  |
| `--since` | Only enqueue conversations with rows since this ISO timestamp (e.g. 2026-04-01). | None |  | str |  |
| `--skip-already-enriched` | Skip conversation_ids already present under this variant in memory_items. | None |  | str |  |
| `--limit` | Cap how many conversations to enqueue. | None |  | int |  |
| `--dry-run` | Count what would be enqueued, write nothing. | `False` |  | store_true |  |
| `--yes` | Skip the confirmation prompt. | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `chatlog_config (chatlog_db_path)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `m3_core.context (pointed_store_connection)`
- `m3_core.paths (resolve_cli_store, seam_backend, seam_dialect)`
- `memory.backends.dialect (Dialect)`
- `memory.backends.dialect (chatlog_table)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
