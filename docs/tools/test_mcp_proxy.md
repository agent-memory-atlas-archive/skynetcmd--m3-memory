---
tool: bin/test_mcp_proxy.py
sha1: 89b3f73a9e5b
mtime_utc: 2026-10-12T18:29:52.681063+00:00
generated_utc: 2026-10-12T18:59:10.691513+00:00
private: false
---

# bin/test_mcp_proxy.py

## Purpose

test_mcp_proxy.py — End-to-end proxy test suite
================================================
Tests the MCP Tool Execution Proxy (localhost:9000) with:
  T1 — Health check (all 5 backends listed)
  T2 — Claude via proxy (tool call execution verified)
  T3 — Gemini via proxy (tool call execution verified)
  T4 — aider-claude non-interactive (subprocess, exit 0)

Usage:
  # Start proxy first:
  bash ~/m3-memory/repo/bin/start_mcp_proxy.sh --background
  # Then run:
  python3 ~/m3-memory/repo/bin/test_mcp_proxy.py

---

## Entry points

- `async def main()` (line 236)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)`
- `m3_sdk (resolve_db_path)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `cmd`` (line 207)

**http**

- `httpx.AsyncClient()` (line 65)
- `httpx.AsyncClient()` (line 88)

**sqlite**

- `sqlite3.connect()  → `DB_PATH`` (line 76)


---

## Notable external imports

- `httpx`

---

## File dependencies (repo paths referenced)

- `{WORKSPACE}/.aider.conf.yml`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
