---
tool: bin/measure_tool_tokens.py
sha1: a61d8bb4a0ac
mtime_utc: 2026-10-17T18:41:17.982172+00:00
generated_utc: 2026-10-17T18:47:23.057091+00:00
private: false
---

# bin/measure_tool_tokens.py

## Purpose

measure_tool_tokens.py — quantify token cost of MCP tool schemas.

Usage:
    python bin/measure_tool_tokens.py

Reports the tokenized size of:
  - Full repertoire (every tool the proxy can dispatch)
  - Lazy-mode startup set (tools_config.DEFAULT_STARTUP_TOOLS — what an agent
    pays at session start with the shipped default)
  - Per-domain cost (what `tools_load_domain(domain=…)` adds on demand)

Uses tiktoken if available (matches OpenAI/Claude tokenization closely);
falls back to a 4-chars-per-token approximation otherwise.

Run this whenever the catalog grows or descriptions change so the numbers
in CLAUDE.md / GEMINI.md / README.md / docs/* stay honest.

---

## Entry points

- `def main()` (line 114)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `mcp_proxy`
- `mcp_tool_catalog`
- `tool_domains`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `catalog.annotations (tool_annotations)`
- `tiktoken`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
