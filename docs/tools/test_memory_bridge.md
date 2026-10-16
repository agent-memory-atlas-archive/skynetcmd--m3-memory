---
tool: bin/test_memory_bridge.py
sha1: 87834627a590
mtime_utc: 2026-10-16T19:12:28.101291+00:00
generated_utc: 2026-10-16T19:12:52.832296+00:00
private: false
---

# bin/test_memory_bridge.py

## Purpose

End-to-end test suite for memory_bridge.py.

Tests all 38 MCP tools (including agent registry, notifications, task orchestration,
memory_history, memory_link, memory_graph, memory_verify, memory_set_retention,
gdpr_export, gdpr_forget, memory_cost_report, memory_handoff, memory_inbox, memory_inbox_ack).
Embedding-dependent tests are attempted and gracefully skipped when an
embedding model is not loaded in LM Studio.

---

## Entry points

- `async def run()` (line 183)
- `async def main()` (line 1308)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `COMPUTERNAME`
- `HOSTNAME`

---

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)`
- `m3_cognitive_loop (run_classify_pass)`
- `m3_sdk (getenv_compat)`
- `m3_sdk (resolve_db_path)`
- `memory_bridge (VALID_MEMORY_TYPES, _content_hash, _ensure_sync_tables, agent_get, agent_heartbeat, agent_list, agent_offline, agent_register, conversation_append, conversation_messages, conversation_search, conversation_start, conversation_summarize, gdpr_export, gdpr_forget, memory_consolidate, memory_cost_report, memory_delete, memory_export, memory_get, memory_graph, memory_handoff, memory_history, memory_import, memory_inbox, memory_inbox_ack, memory_link, memory_maintenance, memory_search, memory_set_retention, memory_suggest, memory_update, memory_verify, memory_write, notifications_ack, notifications_ack_all, notifications_poll, notify, task_assign, task_create, task_get, task_list, task_set_result, task_tree, task_update)`

---

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 103)


---

## Notable external imports

- `httpx`
- `m3_core.context (open_pointed_store)`
- `m3_core.paths (seam_backend, seam_dialect, store_is_a_file)`
- `memory.db (_db)`
- `platform`
- `types (SimpleNamespace)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
