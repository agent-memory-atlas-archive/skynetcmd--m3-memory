---
tool: bin/memory_bridge.py
sha1: cf58e5785754
mtime_utc: 2026-10-17T18:40:55.325332+00:00
generated_utc: 2026-10-17T18:47:23.081700+00:00
private: false
---

# bin/memory_bridge.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `M3_HTTP_HOST`
- `M3_HTTP_PATH`
- `M3_HTTP_PORT`
- `M3_HTTP_PUBLIC_HOST`
- `M3_TRANSPORT`

---

## Calls INTO this repo (intra-repo imports)

- `m3_halt`
- `m3_http_auth`
- `m3_memory.installer (load_config)`
- `mcp_compat`
- `mcp_compat (FastMCP)`
- `mcp_tool_catalog`
- `memory_core`
- `tool_domains`
- `tool_loader`
- `tools_config`
- `version_drift (check_and_record)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `atexit`
- `catalog.annotations (tool_annotations)`
- `m3_core.caller (as_caller)`
- `m3_core.config (settings)`
- `m3_core.errors (log_swallowed)`
- `memory.backends (dialect)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
