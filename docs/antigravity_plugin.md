# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> m3-memory as a Google Antigravity plugin

`m3-memory` ships as a Google Antigravity plugin in the same repo as the Python package. The plugin auto-registers the memory MCP, wires up chatlog hooks, and adds 15 `/m3:*` slash commands as native Skills plus two curator subagents (`m3:curate-memory` for the memory store and `m3:curate-chatlog` for captured agentic-coding conversations).

## Install

```bash
agy plugin install https://github.com/skynetcmd/m3-memory
```

After install, restart your Antigravity CLI/Desktop session (or reload the environment) so the new MCP server, hooks, and Skills take effect.

## Updating m3

Re-install over the existing plugin to upgrade to the latest published version, then restart Antigravity:

```bash
agy plugin install https://github.com/skynetcmd/m3-memory   # re-install → upgrades in place
# restart the Antigravity CLI/Desktop session so the new MCP server + Skills load
```

> **⚠️ Re-installing can leave the plugin disabled.** As with other agent hosts, re-installing an already-installed plugin can flip it to **disabled** — after which m3's tools and `/m3:*` Skills stop loading. Antigravity exposes a direct fix (no manual config edit needed):
> ```bash
> agy plugin list                 # check whether m3 shows as disabled
> agy plugin enable m3            # re-enable it
> # then restart the Antigravity session
> ```

**Verify the update:** run `agy plugin list` to confirm m3 is present and enabled, and `m3 doctor` for overall m3 health. (`m3 doctor`'s plugin-version check currently covers the Claude Code plugin specifically; for Antigravity, `agy plugin list` is the authoritative check.)

---

## What it does on first run

The plugin's hooks run `m3 hook <name>`, so the `m3` CLI must be on PATH. If it is
not, the hook fails and Antigravity notes it only in its own log, so install m3
first:

```
pipx install m3-memory && pipx ensurepath
m3 setup
```

---

## Slash commands (Agent Skills)

Run `/m3:help` (leveraging the `m3-help` Skill) to see the full list of commands. Highlights:

| Command / Skill | What |
|---|---|
| `/m3:health` | Health check — package, payload, chatlog DB, hook state |
| `/m3:status` | Chatlog row counts, queue, last capture |
| `/m3:search <q>` | Hybrid memory search |
| `/m3:save <content>` | Auto-classified memory_write with confirmation |
| `/m3:write <content>` | Direct memory_write |
| `/m3:get <id>` | Fetch one memory |
| `/m3:graph <id>` | Knowledge-graph traversal |
| `/m3:forget <id>` | Delete with confirmation |
| `/m3:export` | GDPR Article 20 export |
| `/m3:tasks` | Task list |
| `/m3:agents` | Registered agents |
| `/m3:notify` | Inbox poll |
| `/m3:find-in-chat <q>` | Search captured chat-log turns |
| `/m3:install` | Install / upgrade |
| `/m3:help` | This list |

The full catalog of MCP tools remains callable directly via tool calls — these Skills are shortcuts to the high-leverage subset.

---

## Subagents: `m3:curate-memory` and `m3:curate-chatlog`

Two curator subagents handle the two stores:

- **`m3:curate-memory`** — triggered by "curate memory", "tidy memory", "dedupe memory", or "consolidate memory". Surveys the memory store, finds clusters of near-duplicates, and proposes a consolidate / supersede / leave-alone plan that you confirm before any deletion.
- **`m3:curate-chatlog`** — triggered by "curate chatlog", "tidy chatlog", "dedupe chatlog", or "consolidate chatlog". Same workflow against the chatlog store, plus aggressive ephemeral-content decay.

Both use a two-spawn execution model: the first invocation surveys and proposes a plan; you re-spawn with the structured plan back as input (prefixed with `apply`) to actually execute it. The plan is always human-reviewable and reversible.

---

## Hooks installed by the plugin

- `SessionStart` (`m3 hook antigravity-session-start`): chat-capture status, the
  m3 version, and the session-recall menu, given to the agent as context
- `Stop` (`m3 hook antigravity-capture`): saves the conversation's new turns each
  time the agent finishes

Antigravity has no compaction hook; Stop already captures every turn. These run
alongside your own blocks in `~/.gemini/config/hooks.json`.

---

## Configuration

The plugin has no settings of its own. Every setting lives in `m3_config.toml`: the enrichment chat model (`m3 config set llm.endpoints <url>`) and the embedder (the `[embed]` keys). The former `endpoint`, `embed_fallback_url` and `embed_gguf` knobs set environment variables m3 no longer reads, so they were removed.
