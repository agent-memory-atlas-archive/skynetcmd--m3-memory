---
tool: bin/gen_agent_plugins.py
sha1: 62eaf8d41c80
mtime_utc: 2026-10-18T18:31:43.723839+00:00
generated_utc: 2026-10-18T18:31:49.678622+00:00
private: false
---

# bin/gen_agent_plugins.py

## Purpose

Generate the Codex plugin's skills and the Gemini CLI extension's context file
from the one source the Claude Code plugin ships.

Sources (edit these, never the outputs):

  - ``commands/<name>.md``       the Claude Code ``/m3:<name>`` commands
  - ``skills/m3-guide/SKILL.md`` the how-to-use-m3 skill

Outputs:

  - ``plugins/codex/skills/m3-<name>/SKILL.md`` one Codex skill per command, with
    Claude's syntax rendered for Codex (``/m3:<name>`` -> ``$m3-<name>``,
    ``$ARGUMENTS`` -> ``<input>``, ``m3:<tool>`` -> ``<tool>``)
  - ``plugins/codex/skills/m3-guide/SKILL.md`` the guide, verbatim
  - ``GEMINI.md`` the guide's body, which ``gemini-extension.json`` loads as
    context (``contextFileName``)

Run ``python bin/gen_agent_plugins.py`` after editing a source; ``--check``
writes nothing and exits 1 when an output is stale, missing or no longer has a
source (``tests/test_agent_plugins_fresh.py`` runs it).

Output is deterministic and LF-only, so a re-run with no source change is
byte-identical.

---

## Entry points

- `def main()` (line 146)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--check` | Write nothing; exit 1 if any output is stale. | `False` |  | store_true |  |

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

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `GEMINI.md`
- `SKILL.md`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
