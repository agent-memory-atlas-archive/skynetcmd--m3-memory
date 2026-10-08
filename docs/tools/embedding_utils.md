---
tool: bin/embedding_utils.py
sha1: deee18e1dbd2
mtime_utc: 2026-10-08T23:32:48.445921+00:00
generated_utc: 2026-10-08T23:33:07.195481+00:00
private: false
---

# bin/embedding_utils.py

## Purpose

Shared embedding and vector-math utilities for MCP bridges.

Consolidates embedding helpers used by memory_bridge.py:
  - Binary packing/unpacking for embedding storage
  - Cosine similarity (numpy-accelerated with pure-Python fallback)
  - Model size parsing for dynamic model selection
  - Change-agent inference from agent_id/model_id hints

---

## Entry points

_(no conventional entry point detected)_

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `numpy`
- `unicodedata`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
