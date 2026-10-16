# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> m3 Memory — Roadmap

> Current version: **v2026.10.7.1** — actively maintained, with SOTA local-first retrieval (99.2% SHR@8, 100% @ k=20; 92% end-to-end QA, no oracle metadata on LongMemEval-S). Priorities shift based on community feedback; open an issue to vote on a feature.

---

## 🔜 Next release — in testing

Built and under test; not yet released. Details in the
[CHANGELOG `[Unreleased]` section](../CHANGELOG.md#unreleased).

- [ ] **One configuration file** — `m3_config.toml` in the config root holds every setting, so background services read the same values as your shell; `m3 config show` reports each setting and where it came from. Legacy environment names keep working for now and log a notice.
- [ ] **Named stores** — `[stores.<name>]` defines a store, selected per call (`store`) or per process (`--store`).
- [ ] **A user-only secrets file** — `m3_secrets.toml`, for machines without an OS keyring; the installer stores the primary PostgreSQL DSN in the keyring or this file, not in plaintext environment variables. m3 no longer reads its secrets from the environment: `m3 secret set NAME` stores one, and `m3 config import-env` (also run by `m3 upgrade`) moves existing ones.
- [ ] **More of m3 runs on PostgreSQL** — including the LangGraph checkpointer (`M3Saver`), GLiNER entity extraction, and the enrichment tools.
- [ ] **`search_profile` tool** — per-stage timing of one real search.
- [ ] **pgvector** — on a PostgreSQL server with pgvector, exact vector search runs in the database.
- [ ] **`m3 config get` / `set` / `unset` / `import-env`, `m3 --config FILE` and `--set`** — read and change one setting, move legacy settings into the file, point any command at another config file, or override a setting for one run.
- [ ] **`m3 batch`** — several commands in one process, one per line of stdin.
- [ ] **Read-access audit log** — every `memory_get` and search hit is recorded (agent, data subject, time), kept 90 days (`audit.read_log_retention_days`), included in `gdpr_export` and erased by `gdpr_forget`; chat-log reads are not audited yet.
- [ ] **m3's state moves from `~/.m3-memory` to `~/.m3`.**
- [ ] **Python 3.12 codebase** — the code is brought up to the supported floor and held there by lint and type checks.

## ✅ Shipped — 2026-Q4 so far

- [x] **Web dashboard / observability portal — stable** (moved from hardening, 2026-10-13) (`bin/dashboard_server.py`) — local FastAPI + HTMX UI: multi-DB overview, graph explorer, KB browser, conflict & audit log, background maintenance launcher. Run `m3 dashboard` (listens on `127.0.0.1:8088`; change with `m3 config set dashboard.host` / `dashboard.port`).
- [x] **Autonomous cognitive loop — stable** (moved from hardening, 2026-10-13) (`bin/m3_cognitive_loop.py`) — background daemon running eleven independently skippable passes — including entity extraction, observation extraction with reflection (contradiction resolution), classification, embedding backfill, belief consolidation, procedural distillation, sync, and maintenance (see [ARCHITECTURE.md](ARCHITECTURE.md#the-passes)).
- [x] **Observer & Reflector SLM stages — stable** (moved from hardening, 2026-10-13) (`bin/run_observer.py`, `bin/run_reflector.py`) — LLM-based semantic contradiction detection beyond embeddings.
- [x] **Verified backups** — backups taken by maintenance tools are complete and verified, and tools refuse a store they were not meant for (v2026.10.4.0).
- [x] **Upgrades that finish on their own** — on Windows and Linux, without manual steps, and an upgrade can no longer remove m3 (v2026.10.5.0 → v2026.10.7.1).
- [x] **Fresh pipx installs work** (v2026.10.4.3).
- [x] **Release engineering** — PyPI and TestPyPI publishing use trusted publishing (no stored tokens) for both m3-memory and m3-core-rs; every release is published to the MCP Registry; one command keeps every version string and manifest in sync, and tests fail when they drift.

## ✅ Shipped — 2026-Q3 highlights

### Pluggable storage backend + framework breadth + procedural memory (v2026.7.17.0)
- [x] **PostgreSQL as a first-class primary backend** (`[db] backend = "postgres"`) via a pluggable SQL storage seam (`bin/memory/backends/`) — SQLite and PostgreSQL ship, with identical semantics across backends.
- [x] **CrewAI (v1.x) integration** — a native `StorageBackend` (`m3-memory[crewai]`); a CrewAI memory stays searchable by every other m3 agent.
- [x] **PydanticAI integration** — deps-injected tools + a recall history-processor, plus a formal `M3MemoryToolset` (`m3-memory[pydantic-ai]`); Python 3.14-native.
- [x] **Procedural memory** — a first-class `procedure` type (skill/runbook/how-to/checklist) distilled from successful task runs (writing is opt-in: `autonomy.distill_auto = true`, else dry-run), with `distills_from` provenance and a procedural retrieval boost.
- [x] **Test suite hardening** — 5,700+ test functions (7,700+ collected cases, 8,100+ with the native core) across 680+ files, run with `filterwarnings=error` (a new warning fails the build).

### Later in Q3 (v2026.7.18 → v2026.9.21)
- [x] **Memory wiki** — `m3 wiki generate` builds a wiki from your memories (v2026.7.21.1).
- [x] **Cursor and Cline** are first-class MCP hosts, configured by `m3 setup` (v2026.7.21.0).
- [x] **Web dashboard in the base install** — no extra needed (v2026.7.30.2).
- [x] **Local reasoning models** — fact extraction works on Ollama reasoning models (v2026.8.11.0), and the cognitive loop drains queued file extraction on its own (v2026.8.17.0).
- [x] **A much smaller download** — the wheel is about 87% smaller (v2026.9.9.0).
- [x] **Python 3.12 floor** — m3 requires Python 3.12 or newer (v2026.9.13.0).
- [x] **Documents of every common format** — HTML, Word, PowerPoint, Excel, RTF, EPUB, OpenDocument and Apple iWork (v2026.9.16.0).
- [x] **A lighter startup surface** — 10 tool schemas registered at startup instead of 20 (v2026.9.17.0).
- [x] **Memories with dynamics** — importance decays and strengthens with use instead of staying fixed (v2026.9.20.0).

## ✅ Shipped — 2026-Q2 highlights

Roughly two months of releases (≈25 between `v2026.4.12b` and `v2026.6.8.1`); grouped by theme below rather than version-by-version.

### Cross-platform install hardening (v2026.6.8.0 → v2026.6.8.1)

- [x] `install.sh` upgrades in place — no more "repo already exists" abort on re-run
- [x] Project Oxidation wheel installs into the pipx venv (not the sibling `repo/.venv` `mcp-memory` couldn't see)
- [x] 3-tier wheel install cascade — PyPI prebuilt → GitHub Release prebuilt → recommend source build
- [x] GitHub Release fallback is **required** for size-capped Linux CUDA (464 MB) and Windows CUDA (122 MB) wheels — both exceed PyPI's 100 MiB cap
- [x] `M3_EMBED_GGUF` auto-discovery + persistence — `~/.zshrc` / `~/.bashrc` on Unix, `setx` on Windows, plus the `memory` MCP server `env` block in `~/.claude/settings.json` and `~/.gemini/settings.json`
- [x] Build-tools preflight catches missing Rust toolchain — probes `~/.rustup/toolchains/<triple>/bin/cargo` in addition to PATH

### m3-core-rs 3.6.6 wheels (v2026.06.07)

- [x] 28 wheels: 7 (os × backend) combinations × cp311–cp314
- [x] PyPI distribution for 20 wheels (5 backends fit under the 100 MiB cap: `macos-metal`, `linux-cpu`, `linux-vulkan`, `windows-cpu`, `windows-vulkan`)
- [x] GitHub Release for all 28 — `linux-cuda` and `windows-cuda` stay GH-only by size policy
- [x] In-process Tier-1 embedder — Metal / CUDA / Vulkan / CPU backends
- [x] Sovereign Tier-2 HTTP embedder (`m3-embed-server`, BGE-M3 on port 8082) as launchd / systemd / Windows Service

### Retrieval quality + LongMemEval-S benchmarks (v2026.6.6.0, v2026.6.8.1)

- [x] **99.2% SHR @ k=8** on LongMemEval-S — full sweep **98.2% / 99.2% / 99.2% / 100.0%** @ k=5/8/10/20 (BGE-M3 hybrid FTS5 + vector + MMR; k=8 is m3's default search depth)
- [x] ~~89.0% E2E QA~~ (**superseded** — oracle-routed configuration; replaced by the 92.0% no-oracle figure below. Not a current or a recall number.)
- [x] **92.0% E2E QA — no oracle metadata** on LongMemEval-S (460 / 500, v3 inferred strategy routing, Claude Opus 4.6 answerer, gpt-4o judge) — supersedes the oracle-routed 89.0% headline; see the [LME-S Benchmarking Report](../benchmarks/longmemeval/LME-S_Benchmarking_Report.md) and [xiaowu0162/LongMemEval#49](https://github.com/xiaowu0162/LongMemEval/issues/49)
- [x] FTS5 sanitizer rewrite — allowlist tokenization fixes search crashes on queries containing hyphens, colons, `field:value` tokens (`gpt-4o`, `claude-code`, `100-200MB`, …)
- [x] LongMemEval-S benchmark harness shipped under `benchmarks/longmemeval/`
- [x] Live methodology + k-sweep + engine-upgrade addendum discussion: [xiaowu0162/LongMemEval#43](https://github.com/xiaowu0162/LongMemEval/issues/43)

### Files-memory subsystem (v2026.5.18.0 → v2026.5.29.5)

- [x] 25-tool files-memory layer — directory ingestion, hierarchical chunking, ascension to core memory, watch-mode staleness review
- [x] Files entity-linking + fact-extraction with bug fixes (v2026.5.29.4 / .5)
- [x] Modular `memory_core` refactor — submodule extraction with shim-preserved identity (no public surface drift; 322+ symbols snapshot-fingerprinted)

### Entity coalescing (v2026.5.29.7 → v2026.5.30.0)

- [x] Entity coalescing v1 → v2 — dedup/merge across mentions, structured `{a, b, score, title_a, title_b}` return per pair
- [x] MCP tool catalog registration for entity ops; CLI exit-code fix

### Engine v3 — bitemporal + decoupled roots (v2026.6.1.0)

- [x] Polars-accelerated bitemporal history analytics (pure-Python fallback retained; Polars optional)
- [x] Doctor quick-repair mode — `m3 doctor --fix` with `--dry-run` preview
- [x] SDK oxidation — Rust-backed FFI shims for `sysinfo`, advisory file locking (`fs2`), atomic circuit breakers (PyO3)
- [x] Decoupled config/engine roots (`M3_CONFIG_ROOT` / `M3_ENGINE_ROOT`) for clean security boundaries

### Multi-agent reach

- [x] **Hermes Agent** memory-provider plugin (file-based, vendored under `m3_memory/integrations/hermes/`)
- [x] **Antigravity** CLI/Desktop wiring via `~/.gemini/antigravity-cli/settings.json`
- [x] **OpenClaw** native MCP server, registered by `m3 setup` (native since 2026.3.22; the proxy path it originally shipped with is retired)
- [x] **LangChain / LangGraph** native surfaces — drop-in Mem0 replacement, LangMem-compatible `M3Store`, `M3Saver` LangGraph checkpointer (pause/resume/time-travel), chat-message history, RAG retriever, and LCEL-native `MemoryWrite`/`MemoryRetrieve`/`with_m3_memory` (`m3_memory/integrations/langchain/`, `pip install m3-memory[langchain]`)
- [x] Chatlog capture hooks — Claude Code (Stop + PreCompact, zero-gap) and Gemini CLI (SessionEnd)
- [x] curate-memory + curate-chatlog subagents with UUID-integrity defense (v2026.6.8.0)

### Knowledge maintenance — confidence, trust, reinforcement, beliefs

Memory as a maintained body of knowledge, not a flat index. All additive and
**off by default** (migrations 035–036). See `docs/CONFIDENCE_AND_TRUST.md`.

- [x] First-class **confidence** on `memory_items` (provenance + corroboration aggregate; NULL ⇒ importance)
- [x] **Trust-weighted / consensus provenance** — `agents.trust_score`, append-only corroboration ledger, `agent_set_trust` tool, corroboration-on-write (closes the orphan-duplicate gap)
- [x] **Reinforcement** — confidence decays toward NEUTRAL (uncertainty), re-aggregates from the ledger, access as weak capped evidence
- [x] **Autonomous episodic→semantic consolidation** — `belief` type, weekly `consolidate_beliefs.py`, reversible (soft-delete + edges)
- [x] **Confidence in ranking** behind `search.confidence_ranking` (zero-regression flag-off contract, tested)

### Compliance + privacy

- [x] FIPS 140-3 deployment-ready: tiered crypto (`crypto.fips_mode` = hardened
  wolfCrypt, `crypto.fips_strict` = CMVP-validated module), power-up KATs, secure
  DLL-hijack-resistant loading, `m3 fips install-wolfssl` helper, algorithm
  whitelist enforcement (see `FIPS_MODULE_BOUNDARY.md`)
- [x] GDPR Article 17 (`gdpr_forget`) + Article 20 (`gdpr_export`) as MCP tools
- [x] Per-conversation isolation enforced at the SQL layer (`WHERE conversation_id = ?` baked in)
- [x] Audit log entry via `_record_history` for every destructive op
- [x] Content-safety regex hardening (CodeQL #29, v2026.5.18.1)

### Windows hardening (v2026.5.29.3, .6)

- [x] Installer crash fixed
- [x] UTF-8 mode forced — eliminates the cp1252 emoji/box-drawing crash class
- [x] WMI-safe OS detection — no `platform.system()` hangs on Py3.14

### Sustained engineering

- [x] **100+ MCP tools** (was 66 at v2026.4.12b)
- [x] **5,700+ test functions (7,700+ collected cases, 8,100+ with the native core) across 680+ files** (warnings-as-errors)
- [x] PyPI Trusted Publishing via OIDC — no token in CI
- [x] Pre-push tool-catalog drift gate + bench-data leakage scan (`.githooks/pre-push`)
- [x] CodeQL security gates + periodic Bandit + pip-audit + secrets-scan reports under [`docs/audits/`](./audits/)

---

## ✅ Shipped — Foundation (2026-Q1)

### v2026.4.12b — Conversation Grouping & Refresh Lifecycle (April 12)

- [x] `conversation_id` on `memory_write` / `memory_search` / `memory_update`
- [x] Refresh lifecycle — `refresh_on`, `refresh_reason`, `memory_refresh_queue`
- [x] Reversible migration system with backup/restore

### v2026.4.12 — Multi-Agent Orchestration (April 12)

- [x] Agent registry, handoffs, notifications, task trees
- [x] `m3-team` CLI for multi-agent teams from YAML
- [x] MCP proxy v2 — catalog-driven dispatch
- [x] License → Apache 2.0

### v2026.4.8 — PyPI Launch (April 10)

- [x] `pip install m3-memory` works out-of-the-box
- [x] `mcp-memory` CLI entry point auto-starts the server
- [x] `publish.yml` GitHub Actions — automated PyPI publish via OIDC

### v2026.04.06 — Initial Production Release (April 6)

- [x] Core memory system — write, search, update, delete, link
- [x] Hybrid retrieval — FTS5 + vector similarity + MMR re-ranking
- [x] Contradiction detection and bitemporal versioning
- [x] Knowledge graph (memory-link relationship types — 11 today, plus internal `distills_from`; entity-graph layer with a user-configurable 34-predicate vocabulary via `entity.vocab_yaml`)
- [x] Cross-device sync — SQLite ↔ PostgreSQL
- [x] LLM auto-classification, conversation summarization, memory consolidation

---

## 🚧 In progress

- [ ] **LoCoMo audit** — harness scaffolded under `benchmarks/locomo/`; full run pending

---

## 📦 Planned — Distribution & Deployment

> **Order after the next release:** health and Prometheus metrics endpoint, HNSW approximate search, official Docker image (on demand), server / multi-user deployment guide, stable public SDK, search-explainability UI. The README's [Project status](../README.md#project-status) summarizes this list.

- [ ] **HNSW approximate search on the PostgreSQL primary backend** — exact search already scores in the database when the server has pgvector (migration `pg_066` keeps a `vector` copy of each embedding; older rows are filled in the background after the upgrade). An HNSW index would trade exactness for speed on stores far larger than 50,000 memories.
- [ ] **Docker image** — built on community demand: today's users run m3 directly (`pip install` and `m3 setup`) and have not asked for one. Shape if built: `docker run -v ~/.m3:/data ghcr.io/skynetcmd/m3-memory:latest`
- [ ] **TestPyPI as a release gate** — TestPyPI is published alongside PyPI today; make it pass before PyPI publishes
- [ ] **Homebrew formula** — `brew install m3-memory` (macOS / Linuxbrew)
- [ ] **Server / multi-user deployment guide** — PostgreSQL-primary deployment, backups, upgrades, monitoring, and the measured point at which to move from SQLite

---

## 📈 Planned — Observability & Web UI

> The **web dashboard** has shipped and is stable (see [Shipped — 2026-Q4](#-shipped--2026-q4-so-far)); the items below extend it.

- [x] **Search explain mode** — `memory_search(explain=true)` attaches a per-result breakdown (vector, keyword, title overlap, importance)
- [ ] **Real-time contradiction log** — surfaced in dashboard and via `memory_verify` tool
- [ ] **Health and Prometheus metrics endpoint** — `/health`, latency, write/read counts, cache hit rates
- [ ] **Grafana dashboard template** — pre-built panels for memory health

---

## 👥 Planned — Multi-Agent & Collaboration

- [ ] **Shared memory namespaces** — optional scoped memory pools across multiple local agents
- [ ] **Agent identity model** — per-agent memory isolation with explicit cross-agent read grants (today: per-`agent_id` scoping at the SQL layer; the cross-agent grant primitive is still implicit)
- [ ] **Remote P2P sync** — encrypted memory replication over WireGuard / Tailscale without a central server
- [ ] **Discord, Matrix and Slack for agent collaboration** — m3 already includes a FIPS 140-3 ready communication and notification path that agents use to coordinate and collaborate with each other. This adds Discord, Matrix and Slack as optional channels on top of it, so you can follow the agents' conversation and step in from a chat app. Available only where FIPS is not mandated, and only when you opt in, since messages then leave your machine for the chat service. m3 remains the record, and agents keep working without the chat service.

---

## 🏆 Planned — Benchmark & Stability

- [ ] **Public benchmark suite** — MRR, Hit@5, latency vs. Mem0 / LangMem / raw ChromaDB on standard datasets (LongMemEval-S already published — see Shipped above)
- [ ] **Formal accuracy regression CI** — block merges that degrade retrieval quality (today: behavior baseline at `tests/capture_retrieval_baseline.py` is the local gate)
- [ ] **Stable public API** — `m3_memory.sdk` Python API with semver guarantees
- [ ] **Full documentation site** — MkDocs or Docusaurus with API reference, tutorials, architecture deep-dives
- [ ] **Plugin system** — register custom memory types, custom embedders, custom sync backends

---

## ❄️ Icebox (considering, no timeline)

- Browser extension for passive memory capture
- iOS / Android companion app for on-device sync
- LlamaIndex adapter (LangChain / LangGraph already shipped — see above)
- OpenTelemetry trace export

---

## 🤝 Contributing

Vote on features by reacting to [GitHub Issues](https://github.com/skynetcmd/m3-memory/issues) with 👍. Open a new issue with the `roadmap` label to propose something not listed here.

See [GOOD_FIRST_ISSUES.md](./GOOD_FIRST_ISSUES.md) for tasks ready to pick up right now.
