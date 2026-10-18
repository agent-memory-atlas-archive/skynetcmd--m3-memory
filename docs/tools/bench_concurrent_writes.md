---
tool: bin/bench_concurrent_writes.py
sha1: 682aab25e26c
mtime_utc: 2026-10-18T04:50:33.379574+00:00
generated_utc: 2026-10-18T04:50:41.092214+00:00
private: false
---

# bin/bench_concurrent_writes.py

## Purpose

bench_concurrent_writes.py — many agents writing to one SQLite store at once,
optionally while an ingestion pipeline writes to the same store.

Usage:
    python bin/bench_concurrent_writes.py                      # 1,4,8,16 writers, no ingestion
    python bin/bench_concurrent_writes.py --ingest import      # bulk import running alongside
    python bin/bench_concurrent_writes.py --ingest maintenance # decay/purge passes alongside
    python bin/bench_concurrent_writes.py --writers 32 --per-writer 100 --ingest both --json

Each writer is a separate OS process, as real agents are, writing through m3's own
write path (memory_write, not raw SQL), embedding off so the figures are the
database write and its locking. The ingestion process runs m3's own long write
transactions (memory_import of a batch, memory_maintenance) in a loop on the same
store for the whole run. Everything happens under a throwaway config root whose
engine, logs and memory roots are temp directories: the developer's store is never
touched.

Reported per run: per-write latency p50/p95/max, writes failed, and the longest
ingestion transaction (the bound on how long any writer can be made to wait).

---

## Entry points

- `def main()` (line 231)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--writers` | comma-separated writer counts | `1,4,8,16` |  | str |  |
| `--per-writer` | writes per writer | `40` |  | int |  |
| `--ingest` |  | `none` |  | str |  |
| `--batch` | rows per memory_import transaction | `500` |  | int |  |
| `--json` | print results as JSON | `False` |  | store_true |  |
| `--role` | (hidden from --help) | — |  | str |  |
| `--start-at` | (hidden from --help) | — |  | float |  |
| `--tag` | (hidden from --help) | `w` |  | str |  |
| `--stop-file` | (hidden from --help) | — |  | str |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `memory_core`
- `memory_maintenance`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.Popen()` (line 156)
- `subprocess.Popen()` (line 183)


---

## Notable external imports

- `statistics`

---

## File dependencies (repo paths referenced)

- `m3_config.toml`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
