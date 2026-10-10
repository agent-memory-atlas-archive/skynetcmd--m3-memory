---
tool: bin/enrichment_state.py
sha1: 7401a491a723
mtime_utc: 2026-10-10T23:42:28.666811+00:00
generated_utc: 2026-10-10T23:43:51.542810+00:00
private: false
---

# bin/enrichment_state.py

## Purpose

Durable per-group enrichment state for m3_enrich.

Backs migration 028's enrichment_groups + enrichment_runs tables. Pure helper
module — m3_enrich.py imports from here; no reverse dependency. Designed to
be unit-testable in isolation against a fresh sqlite file.

State machine:
    pending ──claim──▶ in_progress ──┬── success (obs_emitted > 0)
                                      ├── empty   (extractor OK, 0 obs)
                                      ├── failed  (transient — eligible for retry)
                                      └── dead_letter (deterministic OR attempts>=N)

    stale claim (claimed_at older than CLAIM_TIMEOUT_SEC) ──▶ pending  (auto on resume)
    source_content_hash changed                              ──▶ superseded (old row)

All callers should hold a per-DB sqlite3.Connection in WAL mode. The module
neither opens nor closes connections — that's the caller's responsibility,
matching the rest of bin/.

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

- `_task_runtime (no_window_kwargs)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['git', 'rev-parse', 'HEAD']`` (line 123)


---

## Notable external imports

- `m3_core.context (open_pointed_store)`
- `m3_core.runtime (utc_iso)`
- `memory.backends (dialect)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
