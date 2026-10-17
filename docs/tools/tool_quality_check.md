---
tool: bin/tool_quality_check.py
sha1: c20c7fb784ab
mtime_utc: 2026-10-17T20:29:08.819960+00:00
generated_utc: 2026-10-17T20:55:05.426140+00:00
private: false
---

# bin/tool_quality_check.py

## Purpose

tool_quality_check.py — check what m3's MCP tools tell an agent about themselves.

Usage:
    python bin/tool_quality_check.py          # report; exit 1 on any failure
    python bin/tool_quality_check.py --json

Registries grade MCP servers on their tool definitions (Glama's TDQS: purpose,
usage guidance, parameter documentation, annotations). Glama's grader is a
hosted model and is not published, so this checks the objective half locally,
on every catalog tool:

  * every parameter has a description;
  * every tool's annotations (readOnlyHint / destructiveHint) are consistent:
    a read-only tool is never destructive, every READ_ONLY_TOOLS name exists;
  * every tool description is at least MIN_DESCRIPTION characters;
  * what the server SENDS equals the catalog: until 2026-10-17 FastMCP rebuilt
    each schema from a generated signature and dropped every parameter
    description, so clients saw 0% while the catalog held ~90%.

tests/test_tool_quality.py runs it, so a regression fails the suite.

---

## Entry points

- `def main()` (line 105)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--json` | print findings as JSON | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `mcp_compat`
- `mcp_tool_catalog`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `catalog.annotations (READ_ONLY_TOOLS, tool_annotations)`
- `catalog.annotations (tool_annotations)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
