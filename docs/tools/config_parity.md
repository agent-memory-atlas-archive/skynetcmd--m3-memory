---
tool: bin/config_parity.py
sha1: efa565f522ac
mtime_utc: 2026-10-14T03:18:55.665469+00:00
generated_utc: 2026-10-14T03:18:56.144669+00:00
private: false
---

# bin/config_parity.py

## Purpose

Config parity snapshot: what m3 resolves today, so the config migration can prove it changed nothing (plan Step 2A.2).

Before the first setting moves behind the new resolver, capture every setting
as today's code sees it; after each migration step, ``check`` recaptures and
diffs. Only entries on the user-reviewed intended-change list may differ
(CONFIG_DESIGN §8).

Four layers per snapshot:
  env     every constant env name m3 reads (the 2A.1 inventory,
          ``check_env_access``), BEFORE and AFTER M3Context loads ``.env``
  parsed  the constants m3's config modules compute from the env at import:
          the per-key legacy parser, whatever today's code makes of the string
  files   each JSON config file under the config root, flattened
  store   ``active_backend().store_identity()`` (credential-free; never a path)

SECRETS never reach the file. A secret-named key, or any value carrying URL
credentials, is recorded as HMAC-SHA256(value) under a random per-machine key
kept in the OS keyring and never exported, so the snapshot still catches a
corrupted DSN or token, but only on the machine that took it (#P10; a keyed
HMAC, not the unsalted hash CWE-759 warns about). On a machine with NO keyring
backend (a desktop-less Linux box) the key is a user-only file under the
config root instead (user decision 2026-10-08); a keyring that exists but is
locked is an error, never a fallback.

FAIL LOUD (DESIGN_PHILOSOPHIES §3): ``check`` fails when the baseline or the
HMAC key is missing. It never writes a baseline it did not find; that
"seed and skip" is the defect of the older local parity test.

Usage:
    python bin/config_parity.py init-key                  # once per machine; never overwrites
    python bin/config_parity.py capture --context shell   # writes <config root>/parity/<context>.json
    python bin/config_parity.py check --context shell [--intended FILE]

Exit: 0 = ok / no drift; 1 = drift; 2 = cannot check (missing key or baseline).

---

## Entry points

- `def main()` (line 388)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--context` | process context label, e.g. shell, scheduled-task | — |  | str |  |
| `--path` | snapshot file (default: <config root>/parity/<context>.json) | — |  | str |  |
| `--intended` | JSON list of 'layer:key' entries allowed to differ | — |  | str |  |

---

## Environment variables read

- `M3_X`

---

## Calls INTO this repo (intra-repo imports)

- `check_env_access`
- `crypto_provider (get_hmac_sha256)`
- `crypto_provider (get_sha256)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `importlib`
- `keyring`
- `keyring.errors`
- `m3_core.atomic_io (atomic_write)`
- `m3_core.config (REGISTRY)`
- `m3_core.context (M3Context)`
- `m3_core.paths (get_m3_config_root)`
- `m3_core.seams (current_uid)`
- `memory.backends (active_backend)`
- `secrets`

---

## File dependencies (repo paths referenced)

- `.chatlog_config.json`
- `.cognitive_loop_config.json`
- `.embed_config.json`
- `.governor_config.json`
- `.llm_config.json`
- `.log_config.json`
- `.migrate_config.json`
- `.spill_config.json`
- `.tools_config.json`
- `.wiki_admission.json`
- `.wiki_authority.json`
- `.wiki_pii.json`
- `.wiki_self_entities.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
