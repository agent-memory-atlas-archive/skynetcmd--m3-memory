# m3-memory provider for Hermes Agent

A [Hermes Agent](https://github.com/nousresearch/hermes-agent) `MemoryProvider`
that uses m3-memory as the agent's long-term memory backend. User docs:
[docs/HERMES.md](../../../docs/HERMES.md).

## Layout

```
__init__.py              M3MemoryProvider(MemoryProvider) + register(ctx)
folder_plugin/
  __init__.py            delegating stub: imports the provider from this package
  plugin.yaml            manifest (catalog shape; `hermes plugins validate` clean)
```

Hermes 0.22+ loads the provider through the `hermes_agent.memory_providers`
entry point in m3-memory's `pyproject.toml`. The folder plugin is the catalog
install shape; it holds no code, so it can never drift from the package.

## How it maps to m3

All calls go through the shared client `m3_memory.integrations._client.M3Client`
(in-process catalog dispatch on one long-lived event loop).

| Hermes hook | m3 call |
|---|---|
| `prefetch` / `m3_search` | `memory_search_scored` (scope=user) |
| `m3_profile` | `memory_search_scored` with `query=""`, `type_filter="user_fact"` (filter-only listing) |
| `m3_conclude` | `memory_write` (type=user_fact, scope=user) |
| `sync_turn` | `M3Client.write_turns` → `chatlog_write`, host_agent `hermes` |

Non-`primary` `agent_context` (cron, subagent) is recall-only. `is_available()`
stays cheap: it locates m3's payload without importing the catalog, and
`unavailable_reason()` says why and how to fix it.

## Testing

- `tests/test_hermes_provider_contract.py`: the contract with Hermes' modules
  stubbed, run in CI.
- End to end: install m3-memory into a Hermes 0.22+ venv and drive the provider
  through Hermes' own `plugins.memory.load_memory_provider("m3")`, via both the
  entry point and the folder plugin, with every m3 root in a temp dir.
