---
tool: bin/chatlog_config.py
sha1: 5a7dcb5ca1cd
mtime_utc: 2026-10-16T18:49:02.328254+00:00
generated_utc: 2026-10-16T19:03:59.022810+00:00
private: false
---

# bin/chatlog_config.py

## Purpose

chatlog_config.py — configuration resolver for the chat log subsystem.

The three-mode (integrated/separate/hybrid) system has been collapsed: there
is now only a single chatlog DB path. If it happens to equal the main memory
DB path, chat log rows live in the main store (equivalent to the old
"integrated" behavior). Otherwise they live in a dedicated file (equivalent
to "separate"), and promote operations ATTACH the main DB and copy rows
across (what used to be called "hybrid" is just copy=True, which is the
default).

Resolution order for the chatlog DB path:
    1. the configured chatlog store: a script's --chatlog-db pin, else a legacy
       chatlog env name until N+3 (m3_core.paths.configured_chatlog_db)
    2. active_database() ContextVar (per-call override set by the MCP tool
       dispatcher when a caller passes `database` on a chatlog_* tool)
    3. .chatlog_config.json db_path field, when it pins one
    4. the configured main DB (a script's --database pin, else the legacy
       M3_DATABASE env) — unified: chatlog shares it
    5. Default: memory/agent_chatlog.db (separate file; historical default)

Consumers:
    bin/chatlog_core.py       - write queue, search, promote, cost report
    bin/chatlog_status.py     - observability summary
    bin/chatlog_init.py       - interactive setup
    bin/chatlog_ingest.py     - stdin → bulk write
    bin/migrate_memory.py     - multi-target migration runner
    bin/m3_sdk.py             - get_chatlog_conn()

Zero dependency on memory_core, memory_bridge, or mcp_tool_catalog. Safe to import
from any module in bin/ without creating cycles.

Deprecated env var:
    CHATLOG_MODE — ignored with a warning. The former "integrated" behavior
    is now achieved by setting CHATLOG_DB_PATH equal to the main DB (or by
    leaving it unset when a shared --database covers everything).

---

## Entry points

- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (_active_db)`
- `m3_sdk (get_m3_config_root, get_m3_engine_root)`
- `m3_sdk (resolve_db_path)`
- `sqlite_pragmas (apply_pragmas, refresh_schema)`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 664)


---

## Notable external imports

- `m3_core.atomic_io (atomic_write)`
- `m3_core.config (settings)`
- `m3_core.locking (file_lock)`
- `m3_core.paths (canonical_path, main_db_path)`
- `m3_core.paths (configured_chatlog_db)`
- `m3_core.paths (configured_main_db, pin_chatlog_db, pin_database)`
- `m3_core.paths (resolve_config_file)`
- `m3_core.paths (resolve_engine_file)`
- `m3_core.runtime (utc_iso)`

---

## File dependencies (repo paths referenced)

- `.chatlog_config.json`
- `.chatlog_ingest_cursor.json`
- `.chatlog_state.json`
- `.reason.json`
- `agent_chatlog.db`
- `agent_memory.db`
- `alt_chatlog.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
