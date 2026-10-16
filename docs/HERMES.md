# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> m3 Memory for Hermes Agent

m3 plugs into [Hermes Agent](https://github.com/nousresearch/hermes-agent) as an
external **memory provider**: Hermes' single slot for long-term memory, beside its
always-on built-in `MEMORY.md` / `USER.md`. m3 runs inside the Hermes process. It
needs no server and no API key, and memories stay on the machine unless you
configure sync.

What the provider gives the agent:

| Hermes hook | What m3 does |
|---|---|
| `prefetch` (before each turn) | hybrid recall (FTS5 + vector + MMR) for the user's message |
| `sync_turn` (after each turn) | queues the turn into m3's chatlog; facts are extracted in the background |
| `m3_search` tool | hybrid recall, scoped to the user |
| `m3_conclude` tool | stores a fact verbatim |
| `m3_profile` tool | lists the user's stored facts |

Cron jobs and subagents (`agent_context` other than `primary`) are **recall-only**:
they can search, but they store no facts and no turns, as Hermes' provider
contract asks.

## Requirements

- **Hermes Agent 0.22 or newer.** Earlier versions put their `plugins/` folder on
  the import path, where it shadows m3's `memory` package.
- **Python 3.12 or newer in Hermes' environment.** Current Hermes installs use
  3.14. A Hermes on Python 3.11 cannot load m3. The provider reports this,
  naming the interpreter, rather than failing silently.
- **For vector recall, a local embedder** (`m3 embedder --help`: the CPU
  embedder service, or the native core with `m3 embedder fetch-model`; the
  BGE-M3 model is about 418 MB from Hugging Face, downloaded only when you set
  it up). Without one, recall is keyword-only, and m3 logs that once.

## Install

Install m3-memory into **the same Python that runs Hermes**, not your system Python.
Hermes finds the provider through the `hermes_agent.memory_providers` entry point
that m3-memory declares, so you don't need a plugin folder or a `PYTHONPATH`.

```bash
# the interpreter of Hermes' venv; `head -1 "$(command -v hermes)"` shows it
/path/to/hermes/venv/bin/python -m pip install m3-memory
```

Then select m3:

```bash
hermes memory setup     # choose "m3"; optionally set user_id / agent_id
hermes memory status    # "Provider: m3" and m3 listed under installed plugins
```

`hermes memory setup` writes `memory.provider: m3` to Hermes' `config.yaml`. You
can also set that key yourself.

### Configuration

| Setting | Default | Where |
|---|---|---|
| `user_id` | `hermes-user` | `$HERMES_HOME/m3.json`, m3 setting `integrations.hermes.user_id`, or `M3_USER_ID` |
| `agent_id` | `hermes` | `$HERMES_HOME/m3.json`, m3 setting `integrations.hermes.agent_id` |

A gateway that passes a `user_id` to the provider overrides the configured one,
so each user's memories stay separate.

### The folder plugin (catalog shape)

`m3_memory/integrations/hermes/folder_plugin/` is a manifest plus a one-line stub
that imports the provider from the installed package. It is the shape Hermes'
plugin catalog installs, and `m3 setup` can place it in Hermes' plugins tree. The
stub holds no code of its own, so upgrading m3-memory upgrades the provider.

## Verify

```bash
hermes plugins doctor /path/to/site-packages/m3_memory/integrations/hermes/folder_plugin
hermes memory status
```

Then have a conversation and ask the agent to recall something from it. The
turns are in m3's chatlog (`m3 chatlog status` shows the latest write), and
facts extracted from them appear in `m3_search` results.

If m3 does not appear in `hermes memory status` at all, m3-memory is not
installed in Hermes' Python. Install it into that interpreter, as shown above.
If m3 appears but shows `Status: not available`, the `unavailable:` line under
`m3 config` gives the reason and its fix: a missing payload or a shadowing
`memory` package. The same message is logged once when Hermes starts.

## Also: m3's MCP tools in Hermes

To give the agent m3's full tool catalog (linking, tasks, file ingest, chatlog
search) as well, register m3's MCP server in Hermes' `config.yaml`:

```yaml
mcp_servers:
  m3:
    command: m3
```

This works with or without the memory provider. Both read the same store.

## See also

- [Main documentation](../README.md)
- [MCP tools catalog](MCP_TOOLS.md)
- [Extending m3 / integrations](EXTENDING.md)
