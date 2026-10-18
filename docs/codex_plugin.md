# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> m3-memory as a Codex plugin

`m3-memory` ships a Codex plugin in the same repo as the Python package
(`plugins/codex/`, listed by `.agents/plugins/marketplace.json`). The plugin
registers the m3 memory MCP server, adds chat capture and a session-start check
as hooks, and adds the `m3-*` skills: the same set as the Claude Code plugin's
`/m3:*` commands, plus the `m3-guide` skill.

The plugin runs the `m3` CLI, so install that first:

```bash
pipx install m3-memory && pipx ensurepath
m3 setup
```

## Install

```bash
codex plugin marketplace add skynetcmd/m3-memory
```

Then, in Codex, enter `/plugins`, open the **skynetcmd** tab and install
**m3**. Start a new session before using its skills or tools.

**Trust the hooks.** Codex does not run a plugin's hooks until you approve
them: review and trust m3's two hooks in `/hooks`. Until then nothing is
captured.

## Updating

```bash
codex plugin marketplace upgrade skynetcmd
```

Then start a new session. The plugin's hooks and skills come from the
marketplace; the hook code itself comes from the installed m3 package, so also
keep m3 current with `m3 upgrade`.

## What it adds

**MCP server** — `memory`, started as `m3` (the same server the Claude Code
plugin starts). The full catalog of 100+ tools is reachable through it.

**Skills** — mention one with `$`, e.g. `$m3-search release checklist`:

| Skill | What |
|---|---|
| `$m3-health` | Health check — package, payload, chatlog DB, hook state |
| `$m3-status` | Chatlog row counts, queue, last capture |
| `$m3-search <q>` | Hybrid memory search |
| `$m3-save <content>` | Auto-classified memory_write with confirmation |
| `$m3-write <content>` | Direct memory_write |
| `$m3-get <id>` | Fetch one memory |
| `$m3-graph <id>` | Knowledge-graph traversal |
| `$m3-forget <id>` | Delete with confirmation |
| `$m3-export` | GDPR Article 20 export |
| `$m3-tasks` | Task list |
| `$m3-agents` | Registered agents |
| `$m3-notify` | Inbox poll |
| `$m3-find-in-chat <q>` | Search captured chat-log turns |
| `$m3-install` | Install / upgrade |
| `$m3-help` | This list |
| `$m3-guide` | How to use m3 memory well (Codex also picks it up on its own) |

The skills are generated from the Claude Code commands (`commands/*.md`) and
`skills/m3-guide/SKILL.md` by `bin/gen_agent_plugins.py`; edit those sources,
not `plugins/codex/skills/`.

**Hooks** — each is one line that runs `m3 hook <name>`:

- `SessionStart` (startup, resume) — `m3 hook codex-session-start`: capture
  status and version
- `Stop` — `m3 hook codex-capture`: ingests the session's rollout into the
  chatlog after every turn (a repeat is skipped by the ingest cursor)

On Windows, Codex runs the `commandWindows` form of each line through cmd.exe.

## Plugin or `m3 setup`'s registration

`m3 setup` registers the same server in `~/.codex/config.toml`
(`[mcp_servers.memory]`). Codex keeps plugin servers in the plugin's own
namespace, so with both you get two m3 servers. Keep one: either remove the
`[mcp_servers.memory]` table (and answer no to "Wire m3 into Codex CLI?" in a
later `m3 setup`), or turn the plugin's server off and keep its skills and hooks.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `[m3-memory] ... m3 CLI not on PATH` | Codex cannot find `m3` | `pipx install m3-memory && pipx ensurepath`, then restart Codex |
| `invalid choice: 'hook'` | m3 is older than the plugin (`m3 hook` arrived in 2026.10.18.0) | `m3 upgrade` |
| No turns captured | Hooks not trusted yet | Trust them in `/hooks`; then `m3 chatlog status` |
| Skills missing | Session started before install | Start a new session |

`m3 doctor` and `m3 chatlog doctor` report capture health from the shell.

## Uninstall

Uninstall **m3** from `/plugins`, then
`codex plugin marketplace remove skynetcmd`. The `m3` CLI and your memory data
are not touched (see the [Claude Code plugin page](claude_code_plugin.md#uninstall)
for removing those).
