# m3 Memory for Hermes Agent

m3 is a local-first memory provider for Hermes: hybrid recall (FTS5 + vector +
MMR) before each turn, and the turn stored after it, for m3 to extract facts
from. It runs inside the Hermes process. It needs no server and no API key.

This folder is a manifest and a one-line stub. The provider itself ships in the
`m3-memory` package, which installing this plugin installs, so upgrading
m3-memory upgrades the provider.

| Hermes hook | What m3 does |
|---|---|
| `prefetch` (before each turn) | hybrid recall for the user's message |
| `sync_turn` (after each turn) | stores the turn in m3's chatlog |
| `m3_search` tool | hybrid recall, scoped to the user |
| `m3_conclude` tool | stores a fact verbatim |
| `m3_profile` tool | lists the user's stored facts |

Cron jobs and subagents are recall-only: they search, but store no turns and no
facts.

## Requirements

- Hermes Agent 0.22 or newer, on Python 3.12 or newer.
- For vector recall, a local embedder (`m3 embedder --help`). Without one,
  recall is keyword-only, and m3 logs that once.

## What it does on your machine

- **Data:** every turn of the conversation and every fact the agent concludes
  is stored on this machine, under `~/.m3` (the engine, config and logs
  roots), in SQLite by default.
- **Network:** none by default. m3 connects only to what you configure: an
  embedding or LLM endpoint you set, the optional cloud embedding tier (off
  unless enabled), or PostgreSQL sync to a server you name. Setting up the local
  embedder downloads its BGE-M3 model (about 418 MB) from Hugging Face, once,
  and only when you run that setup.
- **Background work:** each turn is written to m3's chatlog from a background
  thread of the Hermes process. Facts are extracted from stored turns by m3's
  Observer and Reflector passes, which run in m3's cognitive loop, a background
  service that `m3 setup` installs, and call an LLM (a local one by default).
  This plugin starts no services: without the loop, turns are stored but no
  facts are extracted from them.
- **Credentials:** none are needed. A PostgreSQL connection string, if you
  configure sync, is stored in m3's user-only secrets file or the OS keyring,
  never in environment variables.
- **Telemetry:** none.

## Configuration

`hermes memory setup` and choose `m3`. `user_id` (default `hermes-user`) and
`agent_id` (default `hermes`) can be set in `$HERMES_HOME/m3.json`; a gateway
that passes a `user_id` overrides it, so each user's memories stay separate.

Full guide: [docs/HERMES.md](../../../../docs/HERMES.md).
