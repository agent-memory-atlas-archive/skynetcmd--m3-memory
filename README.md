<p align="center">
  <a href="https://github.com/skynetcmd/m3-memory">
    <img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3-banner.jpg" alt="m3 Memory Banner" width="100%">
  </a>
</p>

# 🧠 m3 Memory

**Your AI agents forget everything between sessions. m3 helps them remember.**

Switch from Claude Code to Antigravity, then to Cursor, and you lose nothing. Your agents'
accumulated memory comes with you, so you never start from scratch. The memory is yours,
and it stays with you whatever agent or whatever model you use.

m3 is local-first and cloud-friendly. It runs on your own machine, with no account and no
API key, and by default nothing leaves it. When you want the cloud, it's there: cloud models
for enrichment, a shared PostgreSQL server, or cloud AI apps such as claude.ai through a
remote MCP endpoint.

<p align="center">
  <a href="https://pypi.org/project/m3-memory/"><img alt="PyPI" src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/badges/pypi-version.svg"></a>
  <a href="https://www.python.org"><img alt="Python 3.12+" src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/badges/python.svg"></a>
  <a href="https://github.com/skynetcmd/m3-memory/blob/main/LICENSE"><img alt="Apache 2.0" src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/badges/license.svg"></a>
  <a href="https://modelcontextprotocol.io"><img alt="MCP" src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/badges/mcp.svg"></a>
  <img alt="macOS" src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/badges/os-macos.svg">
  <img alt="Windows" src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/badges/os-windows.svg">
  <img alt="Linux" src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/badges/os-linux.svg">
</p>

```bash
pipx install m3-memory  # or pip install m3-memory in a virtual environment; use one, not both
m3 setup                # finds your agents and connects them to m3
m3 doctor               # checks that everything is working
```

No pipx yet? `sudo apt install pipx` (Debian, Ubuntu), `brew install pipx` (macOS) or
`py -m pip install --user pipx` (Windows). If `m3` is not found right after installing,
open a new terminal.

That's the whole install. New here? The [5-minute Getting Started guide](docs/GETTING_STARTED.md)
walks you through your first saved memory.

---

## <a id="at-a-glance"></a>⚡ m3 at a glance

<!-- For search engines and AI agents that read only this page: one line per fact, each
     linking to the doc that owns the detail. Keep it the ONE summary; do not restate
     these facts elsewhere in this README. -->

- **What it is:** a local-first, cloud-friendly, persistent memory layer for AI agents. It works as an MCP server and as a JSON-in, JSON-out command-line tool. It is not an LLM, a chatbot, a plain vector database or a RAG framework. ([Memory model](docs/MEMORY_MODEL.md))
- **Works with:** Claude Code, Cursor, Cline, Gemini CLI, Google Antigravity, Aider, OpenCode, OpenClaw, Hermes, LangChain/LangGraph, CrewAI, PydanticAI, and any MCP client. ([Client setup](docs/MCP_CLIENT_INSTALL.md))
- **Search:** hybrid search (meaning plus keywords) over nine ranking signals, and every result can explain its score. **99.2% session hit rate at k=8** on LongMemEval-S, retrieval alone. ([Ranking signals](docs/TECHNICAL_DETAILS.md#ranking-signals) · [Benchmarks](#-benchmarks))
- **Keeps history:** text is stored verbatim and corrections never overwrite it. Storage is bitemporal, so you can ask what was believed at any point in time, and contradictions are caught on write. ([Technical details](docs/TECHNICAL_DETAILS.md#contradiction-detection))
- **Improves in the background:** an optional cognitive loop embeds, extracts entities and relationships, finds duplicates, decays stale memories and distils reusable procedures. ([Enrichment](docs/M3_ENRICH_GUIDE.md) · [How memory maintains itself](docs/MEMORY_MODEL.md#how-the-memory-maintains-itself))
- **Files and chats:** a 25-tool files domain ingests and chunks directories; a 9-tool chatlog domain captures conversation turns before compaction. ([Files memory](docs/FILES_MEMORY.md) · [Chat log](docs/CHATLOG.md))
- **Storage:** SQLite by default, with nothing to run. PostgreSQL 16 or newer (tested on 16, 17 and 18) is a first-class alternative for one store shared across machines. ([Architecture](docs/ARCHITECTURE.md) · [Sync](docs/SYNC.md))
- **Speed:** a write takes ~31 ms with its embedding, or ~2 ms with the embedding deferred and is keyword-searchable at once; hybrid search takes ~45 ms (p50). ([Performance](docs/PERFORMANCE.md))
- **Privacy and compliance:** works fully offline and installs air-gapped. FIPS 140-3-ready crypto, GDPR export and erasure, and an audit log of who read which memory. ([Sovereign deployment](docs/SOVEREIGN_DEPLOYMENT.md) · [Compliance](docs/COMPLIANCE.md))
- **Multi-agent:** per-agent, per-user and per-org scopes, handoffs and shared tasks; concurrent readers and writers are safe. ([Multi-agent](docs/MULTI_AGENT.md))
- **Tools:** 100+ tools, of which 10 load at startup, about 2% of a 200K context window; the rest load on demand. ([Domain gating](#domain-gating) · [MCP tools](docs/MCP_TOOLS.md))
- **Configuration:** one file, `m3_config.toml`. `m3 config show` prints every setting and where it came from. ([Config reference](docs/CONFIG_REFERENCE.md))
- **Tested:** 5,700+ tests on Windows, macOS and Linux × Python 3.12–3.15, plus a PostgreSQL (with pgvector) lane on Linux, with the native (Rust) core that ships with m3; warnings fail the build. Security scans are published in [`docs/audits/`](docs/audits/). ([Testing](docs/TESTING.md))
- **Status:** stable: the core, the web dashboard, the autonomous cognitive loop and the Observer & Reflector stages. Apache 2.0. ([Project status](#project-status) · [Roadmap](docs/ROADMAP.md))
- **For AI agents:** [llm-context.md](docs/llm-context.md) lists the real tool names and usage rules, and [features.json](docs/features.json) is the machine-readable summary.

---

## <a id="project-status"></a>📍 Project status

**The core is stable, and so is everything else m3 ships.** One summary line per point, each linked to the doc that owns the detail. The test suite checks these lines against the code and the [ROADMAP](docs/ROADMAP.md).

| Status | What it means | Features |
| :--- | :--- | :--- |
| **Stable** | Safe to build on. Covered by the full test matrix on every release; changes go through a deprecation notice. | Memory write / search / update / supersede · hybrid retrieval (BM25 + vectors + MMR) · bitemporal history · contradiction detection on write · GDPR forget / export · SQLite and PostgreSQL storage · MCP server and `m3` CLI · cross-device sync · web dashboard · autonomous cognitive loop · Observer & Reflector stages |
| **Planned** | Not available yet, in this order ([ROADMAP](docs/ROADMAP.md#-planned--distribution--deployment)). | Health and Prometheus metrics endpoint · HNSW approximate search on PostgreSQL · official Docker image, if users ask for one · server / multi-user deployment guide · stable public SDK with semver guarantees |

*   **Search time by store size** (default hybrid search, exact on both backends): SQLite answers in 0.45 s at 50,000 memories, 0.97 s at 100,000 and 1.55 s at 150,000. PostgreSQL: 0.88 s, 1.70 s and 2.54 s; with the pgvector extension, 0.27 s, 0.35 s and 0.46 s. Real stores hold about two vectors per memory: a real 83,000-memory SQLite store answers in 2.7 s, and a real 90,000-memory PostgreSQL store in 1.6 s, or 0.54 s with pgvector ([measured](docs/PERFORMANCE.md#search-time-by-store-size)).
*   **Many writers on SQLite:** SQLite takes one write at a time; writes take milliseconds, so concurrent writers wait briefly instead of failing. 16 agents writing at once lost nothing and waited at most half a second ([measured](docs/PERFORMANCE.md#sqlite-many-agents-writing-at-once)). Agents on several machines, or a busier fleet, should share a PostgreSQL store ([Multi-agent](docs/MULTI_AGENT.md)).
*   **Reads are audited:** every `memory_get` and every search hit is logged with the reading agent, the data subject and the time, kept 90 days, included in `gdpr_export` and erased by `gdpr_forget` ([GDPR](docs/GDPR_COMPLIANCE.md)). Chat-log reads are not audited yet.
*   **Configuration lives in `m3_config.toml`:** `m3 config show` prints every setting and where it came from, and `m3 config set` changes one ([Config reference](docs/CONFIG_REFERENCE.md)). A new install gets one listing every setting with its doc, commented at its default. Older environment-variable names keep working with a warning for two releases, and `m3 doctor` and `m3 config import-env` name any still in use ([Environment variables](docs/ENVIRONMENT_VARIABLES.md)).
*   **Versioning:** calendar versions (`YYYY.M.D.N`). A renamed or removed setting, argument or tool keeps working, with a warning, for at least two releases after its replacement ships ([Changelog](CHANGELOG.md)).

---

## 👀 See it work

Save a decision — your AI agent, or you from the shell:

```console
$ m3 memory memory_write --type decision --title "auth-jwt-algorithm" \
    --content "The auth service uses RS256 JWTs. HS256 was rejected because we need asymmetric verification at the edge."
"Created: 84a944fb-ef3e-403b-9240-f53ab3c015f7"
```

Next week, in a different agent, on a different model — ask in your own words:

```console
$ m3 memory memory_search --query "which signing algorithm did we pick for tokens?" --k 3
{
  "count": 1,
  "items": [
    {
      "id": "84a944fb-ef3e-403b-9240-f53ab3c015f7",
      "score": 0.7501,
      "type": "decision",
      "title": "auth-jwt-algorithm",
      "content": "The auth service uses RS256 JWTs. HS256 was rejected because we need asymmetric verification at the edge."
    }
  ]
}
```

<sub>Prefer the rendered form for reading? Add <code>--no-as_records</code>.</sub>

The query shares no keywords with the stored text — no "RS256", no "JWT" — and still finds it. That's the hybrid engine: BM25 for exact terms, local BGE-M3 vectors for meaning, MMR for diversity. Your agent calls the same tools over MCP, so it recalls this automatically instead of asking you again.

[Core Tools](#-core-tools) lists the five you'll use most. Prefer to watch? A [short captioned video](https://github.com/user-attachments/assets/09ab194a-d2a0-4fe5-a7db-69ae8225e39b) shows one decision saved in one agent and recalled by another ([download](https://github.com/skynetcmd/m3-memory/releases/download/v2026.7.30.1/m3-promo.mp4)).

---

## 🧭 Where to go next

| I want to... | Read |
| :--- | :--- |
| Get set up step by step | [Getting Started](docs/GETTING_STARTED.md) · [Windows](docs/QUICKSTART_WINDOWS.md) · [macOS](docs/QUICKSTART_MACOS.md) · [Linux](docs/QUICKSTART_LINUX.md) |
| Connect a specific agent or framework | [Installation](#-installation) · [Client setup](docs/MCP_CLIENT_INSTALL.md) · [LangChain](docs/integrations/LANGCHAIN.md) · [CrewAI](m3_memory/integrations/crewai/README.md) · [PydanticAI](m3_memory/integrations/pydantic_ai/README.md) |
| Make my agent use it well | [Agent instructions](docs/AGENT_INSTRUCTIONS.md) · [For AI agents](#-for-ai-agents) |
| Use it from scripts, hooks or CI | [For developers](#for-developers) · [Coding guide](docs/CODING_FAQ.md) · [CLI reference](docs/CLI_REFERENCE.md) |
| Understand how it works | [Memory model](docs/MEMORY_MODEL.md) · [Architecture](docs/ARCHITECTURE.md) · [Technical details](docs/TECHNICAL_DETAILS.md) |
| Compare it with Mem0, Letta, Zep and others | [Comparison](docs/COMPARISON.md) |
| Run it offline or in a regulated environment | [Sovereign deployments](#sovereign-air-gapped) · [Compliance](docs/COMPLIANCE.md) |
| Fix a problem | [Troubleshooting](docs/TROUBLESHOOTING.md) · [FAQ](docs/FAQ.md) · [Health FAQ](docs/M3_HEALTH_FAQ.md) |
| See every doc | [Documentation index](docs/README.md) |

---

## <a id="for-developers"></a>🛠️ For developers: a memory backend, not a framework

**MCP is optional.** m3 is a memory *layer* — it owns durable, searchable,
multi-agent memory and stops there, so it drops into whatever you already have
instead of asking you to adopt a stack.

Every tool in the catalog reads JSON on **stdin** and writes JSON on **stdout**,
so m3 is scriptable from **any language or runtime** — and from hooks, CI, and
cron. No SDK, no client library, no MCP server required:

```bash
echo '{"query":"auth","k":3}' | m3 memory memory_search --json-file - | jq '.items[].id'
```

Results compose, so one tool's output drives the next:

```bash
# Raise the importance of everything matching a query: search, transform, bulk-update.
m3 memory memory_search --query "deployment runbook" --k 20 \
  | jq '{updates: [.items[] | {id: .id, importance: 0.9}]}' \
  | m3 memory memory_update_bulk --json-file -
```

**Language-specific work stays on your side of the boundary — by design.**
Code parsing and VCS watching are integrations, not missing features, and each
is a few lines of your own code:

```python
# AST indexing with any parser you already trust — ast, tree-sitter, ts-morph.
symbols = [n.name for n in ast.walk(ast.parse(src))
           if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
subprocess.run(["m3", "memory", "memory_write", "--json-file", "-"],
               input=json.dumps({"type": "reference", "title": path,
                                 "content": "\n".join(symbols)}), text=True)
```

That is what keeps one memory layer serving a Python monorepo, a Rust service
and a TypeScript frontend without forking it.

> **[Using m3 for coding work →](docs/CODING_FAQ.md)** — the full integration
> guide: composing tools, git hooks, CI steps, and where the layer boundary sits.

*(`jq` is not a dependency — it just reads well in examples. m3 emits plain
JSON, so any parser works.)*

---

## 🧩 Beyond the core

The install at the top is the whole product for most people: shared memory, wired into your agents, working offline. Everything below is **optional surface** you can ignore until you want it — each row says what it costs to turn on.

<table border="0">
<tr><td valign="top">🤖</td><td><b>Coding agents</b> · <sub><b>included in the base install</b></sub><br><code>m3 setup</code> auto-detects and wires m3 into <b>Claude Code, Cursor, Cline, Gemini CLI, Google Antigravity, Aider, OpenCode, OpenClaw, Hermes</b> — one shared memory across every agent, and any agent you add later is picked up automatically. (See <a href="docs/MCP_CLIENT_INSTALL.md">MCP Client Install</a>)</td></tr>
<tr><td valign="top">👥</td><td><b>Multi-agent synchronization</b> · <sub><b>included in the base install</b></sub><br>agents coordinate through one store: memory scoped per <code>agent</code> / <code>org</code> / <code>user</code>, direct handoffs into another agent's inbox, shared tasks with a recursive task tree, and opt-in SQL-layer isolation so an agent's private notes stay private. Concurrent readers and writers are safe by design (WAL + retry), so a planner, an implementer and a reviewer can work at the same time. (See <a href="docs/MULTI_AGENT.md">Multi-Agent Orchestration</a>)</td></tr>
<tr><td valign="top">🖥️</td><td><b>Web dashboard, open to all users — not just developers</b> · <sub><b>included in the base install</b></sub><br>a built-in, backend-agnostic control panel (default <code>http://127.0.0.1:8088</code>): browse memory, read your auto-generated Memory Wiki, explore the interactive knowledge graph, and watch system health / load. Just run <code>m3 dashboard</code>. (See <a href="docs/DASHBOARD.md">Dashboard Guide</a>)</td></tr>
<tr><td valign="top">📖</td><td><b>Auto-generated wiki + Obsidian export</b> · <sub>core feature — in the base install, nothing extra to enable</sub><br><code>m3 wiki generate</code> compiles your canonical memories (pinned, high-confidence, beliefs, procedures) and indexed files into a browsable, interlinked Markdown vault — one page per topic, real hyperlinks for every relationship, and provenance links down to the source document each fact came from. Renders on GitHub, in a self-contained offline HTML viewer, or as an <b>Obsidian vault</b> (<code>--obsidian</code> for graph view + backlinks). (See <a href="docs/WIKI.md">Wiki Guide</a>)</td></tr>
<tr><td valign="top">🐘</td><td><b>PostgreSQL</b> · <sub>optional — you do not need a database</sub><br><b>Most people should ignore this row.</b> m3 stores everything in a local SQLite file by default: nothing to install, nothing to run. Point m3 at a PostgreSQL server instead when you want <b>one shared store for several machines</b> — install <code>pip install "m3-memory[postgres]"</code>, store your server's DSN with <code>m3 secret set M3_PRIMARY_PG_URL</code>, and run <code>m3 install-m3 --db-backend postgres</code>. <code>m3 setup</code> always keeps SQLite; it does not configure PostgreSQL for you. (See <a href="docs/ARCHITECTURE.md">Architecture</a> · <a href="docs/SYNC.md">Sync</a>)</td></tr>
</table>

<sub>Also a drop-in memory backend for <b><a href="docs/integrations/LANGCHAIN.md">LangChain / LangGraph</a></b>, <b><a href="m3_memory/integrations/crewai/README.md">CrewAI</a></b>, and <b><a href="m3_memory/integrations/pydantic_ai/README.md">PydanticAI</a></b> — see the framework guides.</sub>

> Every path gains automatic contradiction supersession, bitemporal historical queries, local sovereign embedding, and the full 100+ MCP tool set.

---

## ⚖️ How m3 Compares

A full, feature-by-feature **comparison table** — m3 vs **Mem0, Letta, Zep, Graphiti, LangChain Memory / LangMem, agentmemory, Chronos, Hindsight, Mastra OM, Memento**, and more — with sourced benchmarks and honest "when to choose the other tool" guidance, lives in **[COMPARISON.md](docs/COMPARISON.md)**.

Short version: m3 is the **local-first, cloud-friendly, MCP-native** option that stays *yours* and works across every agent — where cloud services (Mem0), full agent runtimes (Letta), and graph-database systems (Zep, Graphiti) each ask you to adopt their infrastructure. See the [comparison guide](docs/COMPARISON.md) for the row-by-row detail.

---

## 📦 Installation

> ### ⚠️ Python 3.12+ required (changed in `2026.9.13.0`)
>
> **m3 now requires Python 3.12 or newer.** Releases up to and including
> `2026.9.12.0` supported Python 3.11; from `2026.9.13.0` onward, `pip` will
> refuse to install m3 on 3.11 and will silently keep you on the last 3.11-era
> release instead of upgrading.
>
> **On Python 3.11?** Check with `python --version`. To upgrade:
> * **macOS:** `brew install python@3.14 && brew link --overwrite python@3.14`
> * **Windows:** `winget install -e --id Python.Python.3.14`
> * **Debian/Ubuntu:** use [deadsnakes](https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa) or a distro release shipping 3.14.
>
> After a Python minor-version bump, **recreate your virtualenv**
> (`rm -rf .venv && python3 -m venv .venv`) — see
> [HOW-TO-UPGRADE.md](docs/HOW-TO-UPGRADE.md). Your memories are unaffected:
> the databases live outside the venv under `~/.m3/engine`.
>
> We recommend **3.14 or newer** for new installs. Python 3.12 and 3.13 now
> receive security fixes only upstream (no further bug fixes),
> so a future m3 release will raise the floor again — announced at least one
> minor release in advance.

*The [install at the top](#-m3-memory) covers the common path (`pip install m3-memory` → `m3 setup`). This section adds the alternatives: the shell installer, per-agent wiring, and manual MCP configuration.*

### The One-Liner (macOS & Linux)
```bash
curl -fsSL https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh | bash
```
*   *For Windows, please follow the [Windows Manual Installation Guide](docs/install_windows.md).*
*   *To install manually on any platform, refer to the [OS-Specific Install Instructions](INSTALL.md#tldr--manual-path-per-os) or examine the [installer script](https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh).*

### Developer Setup Wizard
If you are developing inside python environments:
```bash
pip install m3-memory
m3 setup
```
The `m3 setup` wizard automatically **detects your installed agents** — Claude Code, Cursor, Cline, Gemini CLI, OpenCode, Antigravity, OpenClaw, Hermes — and wires the m3 `memory` MCP server into each, installs settings files/hooks, provisions the sovereign CPU embedder, and performs a system diagnostic. Detection and wiring re-run on every `m3 update`/`m3 setup`, and `m3 doctor --fix` repoints any config whose paths have moved — so an agent you install *later* gets picked up automatically the next time you run setup or update.

### Integrating with AI Coding Tools

#### 🤖 Claude Code
Install as a plugin to unlock `/m3:*` slash commands, curation subagents, and automatic hooks:
```
/plugin marketplace add skynetcmd/m3-memory
/plugin install m3@skynetcmd
```
*See [Claude Code Plugin Reference](docs/claude_code_plugin.md) and [Claude.ai Connector Guide](docs/claude_ai_connector.md).*

#### ▷ Cursor
Auto-detected and wired by the setup wizard — it writes the m3 `memory` MCP server into `~/.cursor/mcp.json`:
```bash
m3 setup
```
Re-run after installing Cursor and it's picked up automatically; `m3 doctor --fix` repoints the entry if paths move. *See [MCP Client Install Guide](docs/MCP_CLIENT_INSTALL.md).*

#### ◧ Cline (VS Code)
Auto-detected and wired by the setup wizard — it writes the m3 `memory` MCP server into Cline's `cline_mcp_settings.json`:
```bash
m3 setup
```
Also available from [Cline's MCP marketplace](https://github.com/cline/mcp-marketplace) (see [`llms-install.md`](llms-install.md)). *See [MCP Client Install Guide](docs/MCP_CLIENT_INSTALL.md).*

#### 🪐 Google Antigravity
Install the plugin directly:
```bash
agy plugin install https://github.com/skynetcmd/m3-memory
```
*See [Antigravity Plugin Reference](docs/antigravity_plugin.md).*

#### 🦊 Hermes Agent
Run the wizard to automatically wire up optimal memory providers:
```bash
m3 setup
```
*See [Hermes Plugin Integration Guide](docs/HERMES.md).*

#### 🐍 Python / LangChain & LangGraph
Use m3 as a drop-in Mem0 replacement or LangMem backend:
```bash
pip install m3-memory[langchain]
```
*See [LangChain Integration Guide](docs/integrations/LANGCHAIN.md).*

#### 👥 CrewAI (v1.x)
A drop-in `StorageBackend` for CrewAI's unified memory:
```bash
pip install m3-memory[crewai]   # crewai>=1.10,<2 · Python 3.12–3.13 (a 3.14 escape hatch is documented)
```
*See [CrewAI Integration Guide](m3_memory/integrations/crewai/README.md).*

#### 🧩 PydanticAI
m3 tools + auto-recall, or a formal `M3MemoryToolset`. Built on Pydantic v2 — runs natively on Python 3.14:
```bash
pip install m3-memory[pydantic-ai]   # pydantic-ai-slim>=2,<3
```
*See [PydanticAI Integration Guide](m3_memory/integrations/pydantic_ai/README.md).*

---

### ⌨️ The `m3` CLI — the same memory, without an agent

MCP is not the only way in. The `m3` CLI and the MCP server are **two front doors
to the same database**, so anything an agent can do over MCP you can do from a
shell — the whole tool catalog, grouped as `memory`, `files`, `chat`, `tasks`,
`agent`, `admin`, `conversations`, `diagnostics` and `entity`:

```bash
m3 memory memory_search --query "which signing algorithm did we pick?" --k 5
m3 memory memory_write --content "..." --type belief --title "..."
m3 memory memory_write_from_file --path notes.md --type belief --title "..."
m3 chatlog status
```

Content comes from a command-line argument (`--content`) or from a file
(`--path`) — handy when the body is long enough that shell quoting would mangle
it.

Results go to **stdout** and logs to **stderr**, so output pipes cleanly into
`jq`, `grep` or a script:

```bash
m3 memory memory_search --query "postgres" --k 20 2>/dev/null | jq '.'
```

This matters when your MCP client drops the connection: **that is not a memory
outage**. A dropped stdio session only the client can respawn leaves the store
completely intact and fully usable from the CLI until you reconnect (`/mcp` in
Claude Code). Check with `m3 --version`; if the CLI answers, m3 is up.

*See the [CLI Reference](docs/CLI_REFERENCE.md) for the full command surface.*

---

### Manual MCP Server Configuration
To expose m3 to any Model Context Protocol host, add it to your configuration file:

```json
{
  "mcpServers": {
    "memory": {
      "command": "m3"
    }
  }
}
```

---

## <a id="domain-gating"></a>🎚️ Domain Gating: the Full Catalog Without the Context Cost

Most MCP servers make you pay for every tool in every prompt; m3 registers a small set at startup and loads the rest on demand. Tools are grouped into **9 domains** (`memory`, `chatlog`, `files`, `entity`, `agent`, `tasks`, `conversations`, `diagnostics`, `admin`) and loaded lazily.

Only 10 schemas register at startup (~3,885 tokens). That set is chosen by measurement rather than judgement: across real-world multi-agent development sessions it absorbed **95% of all observed tool calls**, so gating the rest costs almost nothing in practice. When your agent needs more, it calls `tools_load_domain(domain="...")` to fetch a domain on demand — or invokes any single tool by name through `m3_call`, with no domain load at all.

| Gating Mode | Registered Tools | Tokens in Schema | % of 200K Window |
| :--- | :---: | :---: | :---: |
| **Lazy (Default)** | **10** | **~3,885** | **1.9%** |
| Typical Active Session (+`memory` +`admin`) | 61 | ~18,613 | 9.3% |
| Eager Mode (`tools.lazy = false`) | 119 | ~29,485 | 14.7% |

> 🛠️ *Note: If your client does not support dynamic tool registration, run `m3 config set tools.lazy false` (or set `lazy = false` under `[tools]` in `m3_config.toml`) to register every tool at startup.*

---

## <a id="sovereign-air-gapped"></a>🛡️ Sovereign & Air-Gapped Deployments

m3 operates completely offline by default.

### Sovereign Local Embedder
A high-performance BGE-M3 embedder runs locally after installation.
*   **Default:** one **shared local embed server** on `127.0.0.1:8082`, running the `m3-embed-server` binary that ships inside the `m3-core-rs` wheel. CPU execution using GGUF format (`_assets/models/bge-m3-Q4_K_M.gguf`). Every m3 process reuses that single server — one model in host RAM — and one GPU context when a GPU wheel is installed — instead of each loading its own copy. It is local-only and never leaves the machine.
*   **Optional (opt-in at `m3 setup`):** additionally embed **in-process** via the `m3-core-rs` native module (llama.cpp linked in-process, zero IPC). On measured real text it is **faster** than the shared server — ~1.9× on short chunks, ~1.75× on medium — so the reason to prefer shared is **memory, not latency**: in-process loads one model copy *per process*, and a typical setup runs several (MCP server, cognitive loop, CLI), while the shared server keeps one model in RAM for all of them. Choose in-process when you have RAM to spare and a short-text workload. ([measurements and caveats](docs/PERFORMANCE.md#embedding-and-the-shared-vs-in-process-question))
*   **Hardware Acceleration (GPU):** Run `m3 embedder install-gpu` to install the CUDA, Vulkan, or Metal build — a prebuilt wheel where one matches your platform, otherwise a source build.
*   **External Provider Fallback:** set `embed.url` (`m3 config set embed.url ...`) to any OpenAI-compatible `/v1/embeddings` endpoint (Ollama, LM Studio, vLLM, or another machine's m3 embed server), and `embed.fallback_url` for a second endpoint to try if the first is unreachable.

### Rust-Oxidized Performance Core
m3 ships a Rust compute core (`m3_core_rs`) that speeds up MMR re-ranking by **95× to 846×** and packed batch cosine by **97× to 178×** (list-based cosine and other operations gain less — see the per-operation table). It is installed **by default** (the installer's `--no-native-wheel` is the opt-*out*), not an optional add-on. A pure-Python fallback covers every code path and is **results-equivalent** — exact for FTS compilation and graph traversal, and within float tolerance for vector math, enforced by `tests/test_oxidation_parity.py`, `test_fts_parity.py` and `test_graph_neighbor_parity.py`. So the core changes speed, never answers: if the wheel is absent, m3 falls back automatically and returns the same results more slowly. (See [Oxidation Benchmarks](docs/OXIDATION_BENCHMARKS.md)).

### Enterprise Security & Compliance
*   **FIPS 140-3 Ready:** m3 implements no cryptography; its own (secrets vault, key derivation, HMACs, audit chain, random keys) runs through one provider that a validated module (wolfCrypt FIPS) can serve, and every network path is named and fails closed under FIPS mode. Ready, not validated: see the [FIPS Boundary Reference](docs/FIPS_MODULE_BOUNDARY.md) for each path and the deployment profiles.
*   **Air-Gapped Install:** Supports installation without internet access via pre-compiled python wheels. (See [Sovereign Deployment Guide](docs/SOVEREIGN_DEPLOYMENT.md) & [FIPS Boundary Reference](docs/FIPS_MODULE_BOUNDARY.md)).
*   **Storage Location:** State lives under separate roots, so databases, configuration and logs can be relocated and secured independently:

    | Root | Default | Holds |
    | :--- | :--- | :--- |
    | `[paths] engine_root` | `~/.m3/engine` | Databases + runtime state (`agent_memory.db`, `agent_chatlog.db`, `files_database.db`) |
    | `M3_CONFIG_ROOT` (or `m3 --config FILE`) | `~/.m3/config` | Configuration: `m3_config.toml`, chatlog config, salt |
    | `[paths] logs_root` | `~/.m3/logs` | Scheduled-task and service logs |
    | `[paths] memory_root` | unset | Legacy, removed at release N+3: when set, the others derive from it. Set `engine_root` and `logs_root` instead |

    **All are relocatable.** Set a root under `[paths]` in `m3_config.toml`; the config file itself is found through `M3_CONFIG_ROOT` or `m3 --config`. A specific root wins over `memory_root`, from which the others otherwise derive as `<root>/engine` and `<root>/logs`. (See [Architecture](docs/ARCHITECTURE.md).)

---

## 🎯 Who This Is For

### m3 is a great fit if...
*   **You run a homelab or self-hosted stack:** m3 is a single `pip install` with no
    account, no API key, and no outbound calls — it runs on the hardware you already
    own, alongside your other self-hosted services. SQLite by default (zero
    infrastructure); PostgreSQL when you want a shared store across machines.
*   **You operate under sovereignty or data-residency requirements** — corporate,
    government, defence, healthcare, or any regulated environment: memory and
    embeddings never leave your boundary. The embedder runs on your own hardware, the
    store is a file you control, and installation works **fully air-gapped** from
    pre-compiled wheels. FIPS 140-3-ready crypto (`crypto.fips_mode`), GDPR
    `gdpr_forget` / `gdpr_export`, audit logs, and relocatable storage roots so
    databases and configuration can be secured independently.
*   **You want the freedom to switch or add agents without losing what they know:** change tools on the fly or down the road — Claude Code, Gemini, OpenClaw, Hermes, whatever comes next — and your project's knowledge carries over instead of disappearing with the switch.
*   **You build with LangChain/LangGraph:** An advanced replacement for standard memory models, adding bitemporal queries, contradiction management, and local embeddings.
*   **You build with CrewAI (v1.10–1.x):** A drop-in `StorageBackend` (`Memory(storage=M3StorageBackend(user_id="crew-alpha"))`) that gives CrewAI bitemporal recall, contradiction-aware supersession, and local embeddings — plus the thing single-vector stores can't do: a CrewAI-written memory can **also be searchable by every other m3 agent** (Claude Code, Gemini, LangChain) if you want. `pip install m3-memory[crewai]`. See the [CrewAI integration guide](m3_memory/integrations/crewai/README.md).
*   **You build with PydanticAI:** m3-backed memory as either drop-in tools + auto-recall (`register_m3_tools`, `m3_recall_processor`) **or** a formal `M3MemoryToolset` (a real PydanticAI `AbstractToolset`). Built on Pydantic v2, so it runs on Python 3.14 with a plain `pip install m3-memory[pydantic-ai]`. See the [PydanticAI integration guide](m3_memory/integrations/pydantic_ai/README.md).
*   **You need security and compliance:** Built-in `gdpr_forget` and `gdpr_export` tools, air-gapped support, and audit logs.
*   **You value privacy:** Zero external cloud requests or subscriptions required.

### m3 is NOT a fit if...
*   You need a hosted SaaS dashboard with managed infrastructure (use [Letta](https://letta.ai)).
*   **You don't want persistent memory:** you want each session to start fresh, with no ability to retrieve prior sessions' knowledge — m3 exists to do the opposite, so your agent's built-in defaults are the simpler fit.

---

## 📊 Benchmarks

> **Read retrieval accuracy first — it is the only number that measures the memory layer.**
>
> **Session Hit-Rate (SHR)** asks one question: *did the system surface the
> evidence that answers the query?* No answer model is involved, so the score
> reflects the memory layer and nothing else. It is the like-for-like metric
> across memory systems.
>
> **End-to-end QA accuracy** runs that retrieved context through an LLM and has
> a judge model grade the answer. Both choices move the score independently of
> retrieval: a stronger answerer lifts a weaker memory layer, a lenient judge
> lifts everyone, and neither is held constant across published comparisons. Two
> systems quoting QA numbers are usually not measuring the same thing.
>
> Both are reported below. **SHR is the headline; QA is context.**

### Retrieval Accuracy — Session Hit-Rate @ k *(the memory-layer metric)*
Evaluated on the 500-question [LongMemEval-S](https://github.com/xiaowu0162/LongMemEval) dataset, using m3's production search function:

| Retrieve Depth (k) | Session Hit-Rate (SHR) ⁂ | Success Count | vs. Prior Version |
| :---: | :---: | :---: | :---: |
| 1 | **91.8%** | 459 / 500 | First Report † |
| 5 | **98.2%** | 491 / 500 | +2.0pp |
| 8 (default) | **99.2%** | 496 / 500 | — |
| 10 | **99.2%** | 496 / 500 | +2.4pp |
| 20 | **100.0%** | 500 / 500 | First Report ‡ |

> † **SHR@1** is the strictest cut — a gold session as the single top-ranked result. At **k=8**, m3's default search depth, a gold session is present for 99.2% of questions — the same as at k=10, which retrieves 25% more rows on LongMemEval-S for no gain; k=1 is reported here for completeness, not as the headline. Cross-system SHR/recall figures are usually quoted at k=5, k=10, k=20, or k=50, so comparing another system's k=10+ number against this k=1 figure is not a like-for-like comparison. Where m3 is set beside other systems, its figure is the k=8 one; k=8 is not reported by others, and SHR can only rise with k.

> ⁂ **Which aggregation.** These are binary per-question `recall_any@k` values — the convention adjacent LongMemEval submissions report. The benchmarking report's per-question-type table aggregates slightly differently and reads marginally higher at shallow depth (98.8% at k=5, 99.4% at k=10); k=20 is 100.0% either way. The table above quotes the more conservative figures.

> ‡ **v3 improvement** — the v3 engine reaches **100% SHR at k=20**, exceeding the prior version's **97.8% measured at the deeper k=30** ([LongMemEval issue #43](https://github.com/xiaowu0162/LongMemEval/issues/43)) — higher recall at shallower depth. Both figures are retrieval-only SHR (no answerer). The "vs. Prior Version" deltas at k=5/k=10 compare v3 against the prior version's 96.2% / 96.8% at the same k.

### End-to-End QA Accuracy *(answer-model and judge dependent — not a memory-layer comparison)*
**92.0% accuracy** (460/500 correct responses) with zero oracle metadata routing.
Reported for completeness; see the note above on why this number is not
comparable across systems the way SHR is:

| Question Domain | Count (n) | Accuracy |
| :--- | :---: | :---: |
| single-session-user | 70 | 94.3% |
| single-session-assistant | 56 | 96.4% |
| single-session-preference | 30 | 80.0% |
| multi-session | 133 | 87.2% |
| temporal-reasoning | 133 | 95.5% |
| knowledge-update | 78 | 93.6% |
| **Overall Summary** | **500** | **92.0%** |

> **How the QA figure was produced.** m3 retrieves; it does not answer questions. In deployment the calling agent chooses what to retrieve and writes the answer. The benchmark harness plays that role: it chooses retrieval options per question (session expansion and a precomputed recall surface, both available in m3's engine but off by default) using a question-text router that is part of the harness, and it supplies the answer prompts and model. m3's built-in intent routing is on by default in both. The retrieval figures (SHR) use m3's search alone.

*Methodology and reproducibility details are located in the [LongMemEval-S Benchmarking Report](benchmarks/longmemeval/LME-S_Benchmarking_Report.md).*

---

## 🧰 Core Tools

While m3 features 100+ tools, these five serve as your primary interface:

| Tool Name | Operation Description |
| :--- | :--- |
| `memory_write` | Save a specific fact, project preference, or technical configuration. |
| `memory_search` | Run hybrid keyword (BM25) and semantic vector search. |
| `memory_update` | Edit existing facts to keep memory accurate. |
| `memory_suggest` | Query memories alongside a mathematically explicit score breakdown. |
| `memory_get` | Fetch details of a single memory using its unique ID. |

*Refer to the [Agent Instructions Guide](docs/AGENT_INSTRUCTIONS.md) and [Full MCP Tool Catalog](docs/MCP_TOOLS.md) for complete parameter definitions.*

---

## 🤖 For AI Agents

You can drop the agent ruleset file [`examples/AGENT_RULES.md`](examples/AGENT_RULES.md) into your workspace to teach your agent best practices (e.g., query before writing, update existing records instead of duplicating).

### Command Installation Prompts
Copy and paste these prompts into your terminal client to let your agent set up m3 for you:

#### Claude Code Prompt
```text
Install m3-memory for persistent memory. Run: pip install m3-memory
Then run: m3 setup
That wires the m3 memory MCP server into my agents and provisions the
local BGE-M3 embedder — no external embedding service is needed. If it
doesn't detect Claude Code, run: claude mcp add --scope user -- m3_memory m3
Then use /mcp to verify the memory server loaded.
```

#### Gemini CLI Prompt
```text
Install m3-memory for persistent memory. Run: pip install m3-memory
Then run: m3 setup
That wires the m3 "memory" MCP server into my agents and provisions the
local BGE-M3 embedder — no external embedding service is needed. If it
doesn't detect Gemini CLI, add {"mcpServers":{"memory":{"command":"m3"}}}
to my ~/.gemini/settings.json under "mcpServers".
```

#### Active Chatlog Capture Plugin
To configure instant conversation logging and backup, tell your active coding agent:
```text
Install the m3-memory chat log subsystem.
```
The agent executes `bin/chatlog_init.py` and configures execution triggers (see [Chat Log Architecture Guide](docs/CHATLOG.md)).

---

## 🎬 See it in action

### Contradiction Detection & Automatic Resolution
<p align="center">
  <img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/demo_contradiction.svg" alt="Contradiction Demo" width="100%">
</p>

### Hybrid Search Scoring Details
<p align="center">
  <img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/demo_search.svg" alt="Hybrid Search Demo" width="100%">
</p>

### Multi-Device Database Sync
<p align="center">
  <img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/demo_sync.svg" alt="Sync Demo" width="100%">
</p>

---

## 💬 Community

[![Discord Badge](https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/badges/discord.svg)](https://discord.gg/ZcJ3EGC99B)
&nbsp;
[![GitHub Issues Badge](https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/badges/github-issues.svg)](https://github.com/skynetcmd/m3-memory/issues)

[How to Contribute](docs/CONTRIBUTING.md) · [FAQ for Developers](docs/FAQ_FOR_DEVELOPERS.md) · [Good First Issues](docs/GOOD_FIRST_ISSUES.md)

---

## 📜 License & Attributions

This project is licensed under the Apache License 2.0. See [LICENSE](LICENSE) for details.

### Built with

m3 Memory is authored and maintained by **skynetCMD**. It was built with the help of
AI coding assistants — **Gemini CLI**, **Claude Code**, and **Google Antigravity** —
which contributed code under the author's direction. (They are tools that assisted;
they are not maintainers, sponsors, or co-owners of the project.)

### Asset & Icon Credits
The provider badges under [`docs/badges/`](docs/badges/) embed small logo glyphs:
* **OpenClaw & OpenCode icons** are from the MIT-licensed [LobeHub icon set](https://github.com/lobehub/lobe-icons) (`lobe-icons`).
* **The Hermes badge** uses a generic caduceus glyph.

See [NOTICE](NOTICE) for the full third-party attribution list.

<br>
<p align="center">
  <a href="https://pypi.org/project/m3-memory/"><img alt="PyPI downloads, last 30 days" src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/badges/pypi-monthly.svg"></a>
  <a href="https://pypi.org/project/m3-memory/"><img alt="PyPI downloads, all-time" src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/badges/pypi-downloads.svg"></a>
  <a href="https://github.com/skynetcmd/m3-memory"><img alt="GitHub clones" src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/badges/github-clones.svg"></a>
</p>
<p align="center"><sub>PyPI downloads, all-time and last 30 days, count every download, mirror and CI-bot traffic included, as most projects' download badges do; the figures without mirrors, closer to real installs, are in <a href="https://github.com/skynetcmd/m3-memory/blob/main/docs/badges/counters.json">counters.json</a>. GitHub clones are unique clones. Badges are regenerated on a schedule by <a href="https://github.com/skynetcmd/m3-memory/blob/main/.github/workflows/star-history.yml">star-history.yml</a>.</sub></p>
<p align="center"><sub><b>Python:</b> m3 core runs on Python 3.12, 3.13, 3.14 and 3.15. The optional framework extras follow their own caps — <b>PydanticAI</b> is 3.14-native (plain <code>pip install</code>); <b>CrewAI</b> requires 3.12–3.13 with m3 (a 3.14 escape hatch is <a href="https://github.com/skynetcmd/m3-memory/blob/main/m3_memory/integrations/crewai/README.md">documented</a>).</sub></p>
</br><p></p>
---

### ⭐ Star History

<details>
<summary><b>⭐ View star history →</b> (click to expand the chart)</summary>

<br>

<a href="https://star-history.com/#skynetcmd/m3-memory&Date">
  <img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/star-history.svg" alt="Star history for skynetcmd/m3-memory" width="100%">
</a>

<sup>Chart regenerated on a schedule by [`.github/workflows/star-history.yml`](.github/workflows/star-history.yml) using the repo's own token — no third-party embed. Click through for the live interactive version.</sup>

</details>

<!-- mcp-name: io.github.skynetcmd/m3-memory -->
