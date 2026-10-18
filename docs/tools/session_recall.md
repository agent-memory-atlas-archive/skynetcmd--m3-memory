---
tool: bin/session_recall.py
sha1: dfc8e236575e
mtime_utc: 2026-10-18T21:33:56.206018+00:00
generated_utc: 2026-10-18T21:34:39.374780+00:00
private: false
---

# bin/session_recall.py

## Purpose

Session recall: summaries of recent chat sessions, offered as a menu.

Three parts share this module because they share one record, the
``session_summary`` memory:

* **The summary pass** (``run_summary_pass``, run by the cognitive loop's
  ``summarize_sessions`` pass). For each captured chat session that is READY
  (quiet for ``recall.quiet_minutes``, or whose latest turn was captured by a
  compaction or exit hook) and has no summary covering its latest turn, it
  writes ONE ``session_summary`` memory: a one-line menu text (the title) and a
  short summary of the session's tail (the content), tagged with the agent,
  conversation, user, git tree and branch, and the first and last turn times.
  The text comes from the best source available, in this order:
    1. a ``session_note`` the agent wrote for that conversation after its last
       turn;
    2. the client's own compaction summary (Claude Code writes one into the
       transcript; capture flags it ``compact_summary``) when no user turn
       follows it;
    3. a model, through the ``session_summary`` SLM profile (local by default;
       ``recall.summary_profile`` opts into ``session`` or a named profile).
  A model that is unavailable leaves the session unsummarized for the next
  sweep; nothing is dropped. A session that grew is summarized again and the
  new summary supersedes the old one (memory_supersede, into the new row).
* **The menu** (``session_brief``): a filtered lookup over those rows for
  (host_agent, user_id, cwd), numbered per the approved design: this agent's
  sessions first, then each other agent's latest, current git tree first. No
  embedding, no model.
* **The session-start text** (``recall_menu_text``): the block a session-start
  hook injects, framed as data written by another session.

All SQL goes through the Dialect; times are stored as ISO-8601 UTC and shown in
local time with the zone only in the menu text.

---

## Entry points

- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--summarize` |  | — |  | str |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `_task_runtime (_rotate_if_oversized)`
- `auth_utils (get_api_key)`
- `chatlog_ingest (capture_user_id)`
- `llm_failover (LLMUnavailable)`
- `llm_failover (LLMUnavailable, discover_model_async)`
- `llm_failover (LLMUnavailable, report_available, report_unavailable)`
- `m3_memory.hook_launchers (ISOLATE_FLAGS)`
- `m3_sdk (M3Context)`
- `memory_core`
- `slm_intent (_call_model)`
- `slm_intent (_is_loopback, _resolve_api_key, localize_endpoint)`
- `slm_intent (load_profile)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.Popen()` (line 981)

**http**

- `httpx.AsyncClient()` (line 708)


---

## Notable external imports

- `httpx`
- `m3_core.config (settings)`
- `m3_core.paths (get_m3_logs_root)`
- `m3_core.paths (resolve_db_path)`
- `m3_core.runtime (utc_iso)`
- `m3_core.seams (detached_spawn_kwargs)`
- `m3_core.seams (git_checkout)`
- `memory.backends (active_backend)`
- `memory.backends (chatlog_table, dialect)`
- `memory.backends (dialect)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
