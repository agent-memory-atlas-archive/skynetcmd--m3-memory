---
tool: bin/install_schedules.py
sha1: f2001534a916
mtime_utc: 2026-10-18T01:55:02.136894+00:00
generated_utc: 2026-10-18T01:57:36.047350+00:00
private: false
---

# bin/install_schedules.py

## Purpose

M3 Memory: Cross-Platform Schedule Installer.
Automatically configures crontab (macOS/Linux) or schtasks (Windows).
Uses project virtual environment paths and ensures log directories exist.

---

## Entry points

- `def main()` (line 2976)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--list` | List configured schedules and exit. | `False` | Prints "Nothing to do" message and exits. | store_true | Lists all 5 schedules (auditor, sync, maintenance, rotator, chatlog-embed-sweep). |
| `--add` | Install one schedule by name (e.g. chatlog-embed-sweep) or 'all'. | — | Prints "Nothing to do" message and exits. | str | Installs Windows Task(s) or crontab entries matching NAME; 'all' installs all 5. |
| `--remove` | Remove one schedule by name, or 'all'. | — | Prints "Nothing to do" message and exits. | str | Removes Windows Task(s) matching NAME; 'all' removes all; Unix users edit crontab. |
| `--repair` | Re-install every configured schedule in place (alias for --add all). | `False` |  | store_true |  |
| `--verify` | Verify the registered job(s) match the spec (Windows task / macOS launchd / Linux systemd). NAME or 'all' (default). Exit code is non-zero if verification fails. | — |  | str |  |
| `--port` | Port for the dashboard service (with --add dashboard). Default 8088. | `8088` |  | int |  |

---

## Environment variables read

- `USER`
- `USERDOMAIN`
- `USERNAME`
- `XDG_CONFIG_HOME`

---

## Calls INTO this repo (intra-repo imports)

- `_task_runtime (ensure_log_config)`
- `governor_migration (GOVERNOR_ELIGIBLE)`
- `governor_migration (KEEP_SCHEDULED_FLOOR, NOT_MIGRATABLE)`
- `governor_migration (_cron_line_is)`
- `governor_migration (cognitive_loop_installed)`
- `m3_halt (base_role)`
- `m3_halt (holds_store)`
- `m3_memory._ports (held_by_another_owner)`
- `m3_memory.jobs (launcher_for)`
- `m3_sdk (ensure_governor_config)`
- `m3_sdk (get_m3_engine_root)`
- `m3_sdk (kill_stale_daemons)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `*args`` (line 31)


---

## Notable external imports

- `html`
- `m3_core.atomic_io (GuardedRewrite)`
- `m3_core.atomic_io (GuardedRewrite, atomic_write)`
- `m3_core.autonomy (ensure_autonomy_config)`
- `m3_core.config (locate)`
- `m3_core.config (settings)`
- `m3_core.config.keys (REGISTRY)`
- `m3_core.paths (EMBED_SERVER_INPROC_LOG)`
- `m3_core.paths (add_config_arg)`
- `m3_core.paths (apply_early_argv)`
- `m3_core.paths (get_m3_logs_root)`
- `m3_core.seams (is_macos, is_windows, roots)`
- `m3_core.seams (os_name)`
- `memory.backends (dialect)`
- `memory.orchestration (_db)`
- `memory.orchestration (agent_list_impl)`
- `plistlib`
- `shlex`
- `xml.sax.saxutils (escape)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
