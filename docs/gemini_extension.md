# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> m3-memory as a Gemini CLI extension

The m3-memory repo is also a Gemini CLI extension (`gemini-extension.json` at
the repo root). The extension registers the m3 memory MCP server and loads a
short guide to using m3 (`GEMINI.md`) as context in every session.

The extension runs the `m3` CLI, so install that first:

```bash
pipx install m3-memory && pipx ensurepath
m3 setup
```

Chat capture in Gemini CLI comes from the hooks `m3 setup` writes into
`~/.gemini/settings.json`; the extension ships no hooks of its own.

## Install

```bash
gemini extensions install https://github.com/skynetcmd/m3-memory
```

Restart Gemini CLI. `gemini extensions list` shows **m3** with its context file
(`GEMINI.md`), MCP server (`memory`) and skill (`m3-guide`).

## Updating

```bash
gemini extensions update m3
```

The hooks and the server come from the installed m3 package, so also keep m3
current with `m3 upgrade`.

## What it adds

- **MCP server** — `memory`, started as `m3`, the same server the Claude Code
  and Codex plugins start. The full catalog of 100+ tools is reachable through it.
- **Context** — `GEMINI.md`: the search-first protocol, write vs supersede,
  chatlog access, and checking that capture is live. It is generated from
  `skills/m3-guide/SKILL.md` by `bin/gen_agent_plugins.py`; edit the skill, not
  `GEMINI.md`.
- **Skill** — `m3-guide`, the same guide as an agent skill.

## Extension or `m3 setup`'s registration

`m3 setup` writes the same `memory` server into `~/.gemini/settings.json`. If
`/mcp` lists m3 twice, remove the `memory` entry from `settings.json` or
uninstall the extension; the hooks `m3 setup` wrote keep working either way.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `memory` server fails to start | `m3` not on PATH for Gemini | `pipx install m3-memory && pipx ensurepath`, then restart Gemini CLI |
| No turns captured | Hooks not wired | `m3 setup` (or `m3 doctor --fix --fix-hooks`), then `m3 chatlog status` |
| `gemini extensions validate` warns the version is not semver | m3 versions are `YYYY.M.D.N` | Expected; nothing to do |

## Uninstall

```bash
gemini extensions uninstall m3
```

The `m3` CLI, its hooks in `~/.gemini/settings.json` and your memory data are not
touched (see the [Claude Code plugin page](claude_code_plugin.md#uninstall) for
removing those).
