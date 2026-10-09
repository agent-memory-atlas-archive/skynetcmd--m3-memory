---
tool: bin/measure_tool_tokens.py
sha1: 380ed97e316a
mtime_utc: 2026-10-09T19:38:31.134235+00:00
generated_utc: 2026-10-09T19:39:08.457822+00:00
private: false
---

# bin/measure_tool_tokens.py

## Purpose

measure_tool_tokens.py — quantify token cost of MCP tool schemas.

Usage:
    python bin/measure_tool_tokens.py

Reports the tokenized size of:
  - Full repertoire (every tool the proxy can dispatch)
  - Lazy-mode startup set (essentials + meta-tools — what an agent pays at
    session start under M3_TOOLS_LAZY, the default)
  - Per-domain cost (what `tools_load_domain(domain=…)` adds on demand)

Uses tiktoken if available (matches OpenAI/Claude tokenization closely);
falls back to a 4-chars-per-token approximation otherwise.

Run this whenever the catalog grows or descriptions change so the numbers
in CLAUDE.md / GEMINI.md / README.md / docs/* stay honest.

---

## Entry points

- `def main()` (line 108)
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
- `memory_bridge`
- `tool_domains`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `tiktoken`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
