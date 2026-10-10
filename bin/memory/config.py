"""Configuration for the m3-memory core.

Two kinds of names live here:

1. **Pure constants** read once at import time from environment variables or
   literals. Read-only after import. Other modules `from .config import X`
   to get them.
2. **Mutable state** (singletons, caches) that live here to avoid circular
   imports. These are prefixed with `_` and other modules should import the
   `config` module and access them as `config._X`.

Phase 1 extracted basic paths/names.
Phase 3 moved the HTTP-client singleton and backend stats to `embed.py` but
kept the thresholds here.
Phase 5 introduced circuit-breaker thresholds.
"""
from __future__ import annotations

import os
import platform
from collections.abc import Callable
from pathlib import Path
from typing import Any

from m3_core.config import REGISTRY as _REGISTRY
from m3_core.config import settings as _settings


def _setting(name: str) -> Any:
    """Key ``name``'s value from the config snapshot (plan 2D.1): m3_config.toml,
    then the legacy env names, then the registry default, parsed by the key's
    one parser. Read ONCE, at import: none of these keys is reloadable, so the
    import-time binding is their contract, exactly as the env reads it replaces
    (a changed file says "restart required"). A name that is not a registry key
    raises here, at import, rather than reading a default nobody declared."""
    key = _REGISTRY.get(name)
    if key is None:
        raise KeyError(f"observed: memory.config reads {name!r}, which is not a registry key. "
                       "inspect: m3_core/config/keys*.py")
    return _settings.current()[key]


def _set_by_file_or_canonical_env(name: str) -> bool:
    """True when key ``name`` came from m3_config.toml or its CANONICAL env name,
    not a legacy alias and not the default. An override is something the user
    asked for under today's name: the legacy ``EMBED_MODEL`` never was one, and
    counting it would change the tier-1 model tag for users who still set it."""
    from m3_core.config.resolve import ORIGIN_DEFAULT

    key = _REGISTRY.get(name)
    if key is None:
        raise KeyError(f"not a registered setting; observed: {name!r}. inspect: m3_core/config/keys*.py")
    got = _settings.current().resolved[key.name]
    if got.origin == ORIGIN_DEFAULT:
        return False
    return got.env_name is None or got.env_name == key.env_names[0]

# Oxidation / Rust availability.
# _OXIDATION_DISABLED must be bound UNCONDITIONALLY — `memory_core.py`
# re-exports it via `from memory.config import _OXIDATION_DISABLED`, and
# any deferred reference (chatlog_core, auto_route, chatlog_redaction)
# crashes at import time on a host without `m3_core_rs` installed. The
# Phase 7+8 refactor (commit bd07525) inlined this read inside the try/
# except, which made the symbol conditionally bound — fixed here.
_OXIDATION_DISABLED: bool = os.environ.get("M3_CORE_RS_DISABLE", "0").lower() in (
    "1", "true", "yes"
)
# Typed `Any` so callers can access the Rust extension's attributes without
# mypy flagging every use as "None has no attribute ..."; the None case is a
# real runtime fallback (extension not installed / disabled).
m3_core_rs: Any = None
if not _OXIDATION_DISABLED:
    try:
        import m3_core_rs  # type: ignore[no-redef]  # noqa: F811 — intentional rebind
    except ImportError:
        m3_core_rs = None  # extra not installed — Python path is the default


# ──────────────────────────────────────────────────────────────────────────────
# Mutable config (set at import from env, written at runtime by setters)
# ──────────────────────────────────────────────────────────────────────────────
# Embedder URL/model overrides. `set_embed_override()` (in memory_core.py for
# now; moves to embed.py in Phase 3) writes to these. Readers MUST go through
# the module attribute, not a local alias:
#
#   # Right:
#   from .. import config
#   url = config._EMBED_URL_OVERRIDE
#
#   # Wrong (binds at import time; misses later sets):
#   from ..config import _EMBED_URL_OVERRIDE
_EMBED_URL_OVERRIDE: str | None = (_setting("embed.url") or "").strip() or None
# An override only when SET (file or env), not the registry default.
_EMBED_MODEL_OVERRIDE: str | None = (
    (_setting("embed.model") or "").strip() or None
    if _set_by_file_or_canonical_env("embed.model") else None)


# ──────────────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────────────
from m3_sdk import get_m3_config_root, get_m3_engine_root, get_m3_root

# BASE_DIR remains the m3-memory repo root for internal assets (e.g. config lists).
BASE_DIR: str = str(Path(__file__).absolute().parents[2])

# Default state (DBs) now lives in the unified M3 root (~/.m3-memory/memory/).
_M3_CONFIG_ROOT = get_m3_config_root()
_M3_ENGINE_ROOT = get_m3_engine_root()
_M3_ROOT = get_m3_root()

# Path resolution (new-root-with-legacy-fallback) is the single source of truth
# in m3_core.paths; this local name is a thin alias.
from m3_core.paths import resolve_engine_file as _resolve_engine_file

DB_PATH: str = os.environ.get("M3_DATABASE") or _resolve_engine_file("agent_memory.db")
# DEPRECATED (2026-07-24): the archive is now the `memory_archive` table IN the
# primary store (SQLite migration 042 / pg_047), written through the seam so it
# works on both backends — see memory_maintenance._transfer_to_archive. The old
# separate sidecar file this path points at was never given its table, so every
# archive write silently no-opped. Retained only so external references and the
# config-parity snapshot don't break; nothing in the codebase reads/writes it.
ARCHIVE_DB_PATH: str = _resolve_engine_file("agent_memory_archive.db")

# files.db (FILE_INGESTION_PLAN.md). Separate physical store with its own
# lifecycle (high-volume, regeneratable, version-tracked, promotable).
# Resolution order: M3_FILES_DB_PATH env > get_m3_root()/memory/files_database.db.
# abspath, not Path.absolute(): this is an identity string compared with
# files_memory.db's own abspath of the same file, and only abspath collapses "..".
FILES_DB_PATH: str = os.path.abspath(  # noqa: PTH100 — identity string; see above
    os.environ.get("M3_FILES_DB_PATH")
    or _resolve_engine_file("files_database.db")
)
# When true, the ingester prompts on first-ever invocation to confirm the
# files.db path (and offers to write M3_FILES_DB_PATH to a user-shell env
# file). Off in non-interactive contexts (CI, MCP server). The ingester
# auto-disables this when stdin is not a TTY.
# RETIRED (user decision, 2C.1): no reader; kept for importers. Env has no effect.
FILES_DB_PROMPT_ON_FIRST_USE: bool = True


# ──────────────────────────────────────────────────────────────────────────────
# Embedding model + transport
# ──────────────────────────────────────────────────────────────────────────────
# BGE-M3 (1024-dim) is the canonical embedder for m3-memory. The store's
# dominant embeddings are `text-embedding-bge-m3`; Qwen embedding is retired.
# Mixing models in one store produces semantically incomparable vectors, so
# the default must name BGE-M3 — an operator can still override via EMBED_MODEL.
EMBED_MODEL: str  # accessor (registry)
EMBED_DIM: int  # accessor (registry)

# ── Proper-embedder identity ───────────────────────────────────────────────────
# A stored vector is only comparable to the rest of the store if it came from the
# SAME embedder: same model name, dimension, normalization scheme, and embed
# space. These knobs define that identity (model-agnostic — driven by config, not
# a hardcoded model). A tier whose output fails this identity is treated as a
# failed tier (the cascade tries the next one); if no tier validates, embedding
# is DEFERRED (the row stays keyword-searchable and is retried next sweep).
#   require_unit_norm: output vectors must be L2-unit-length (the cosine/vector
#       store assumes this). Off only for an embedder that emits raw magnitudes.
#   norm_tol: tolerance for the sampled unit-norm check.
#   space_tag: an explicit embed-space id; defaults to EMBED_MODEL. Bump it (or
#       EMBED_MODEL) when the vector space changes so old vectors are excluded.
#   compatible_models: extra embed_model strings that map to the SAME space
#       (back-compat for vectors written under a different-but-equivalent tag).
EMBED_REQUIRE_UNIT_NORM: bool  # accessor (registry)
EMBED_NORM_TOL: float  # accessor (registry)
EMBED_SPACE_TAG: str  # accessor (registry)
# Tier-2 (the local CPU HTTP embed server) historically mis-tagged its vectors
# with the tier-1 GGUF filename; it should carry the proper identity name.
EMBED_FALLBACK_MODEL_TAG: str  # accessor (registry)
# Operator-supplied extra compatible model tags (comma-separated).
EMBED_COMPATIBLE_MODELS: tuple[str, ...]  # accessor (registry)

EMBED_TIMEOUT_READ: float = 30.0
# TCP connect timeout for the embed HTTP client (LM Studio / embed server).
EMBED_TIMEOUT_CONNECT: float = 3.0
# Hard wall-clock ceiling for embedding a QUERY on the interactive search path
# (bin/memory/search.py). The full _embed cascade can stack per-tier timeouts
# (30s read × ×2 × ×4 + retries + a 30s semaphore wait) into multiple minutes
# on a degraded box — and because the stdio MCP server is a single event loop,
# that stall freezes EVERY concurrent tool call (the "MCP server locked up"
# symptom). memory_write already sidesteps this by DEFERRING its embed when no
# fast tier is available; search cannot defer (no query vector, no semantic
# search), so instead it bounds the embed with this deadline and degrades to
# FTS-only results on timeout. Interactive-only: bulk/backfill paths keep the
# full EMBED_TIMEOUT_READ budget. 0 disables the ceiling (pre-fix behavior).
EMBED_SEARCH_DEADLINE_S: float  # accessor (registry)
ORIGIN_DEVICE: str  # accessor (registry)

# Storage backend selection (Phase 0 of PostgreSQL-as-primary). Default 'sqlite'
# — the only required store (DESIGN_PHILOSOPHIES §1). 'postgres' is opt-in and
# lands in a later phase; selecting it now raises a clear error via
# backends.active_backend(). Read live at resolve time (backends.selector), not
# cached here, so tests can flip it without reimporting config.
DB_BACKEND: str  # accessor (registry)

# Per-backend circuit-breaker thresholds for the embed cascade in
# `bin/memory/embed.py`. Each backend gets its own m3_core_rs.CircuitBreaker;
# after `_THRESHOLD` consecutive failures the breaker opens and skips that
# tier entirely (no FFI / HTTP attempt) for `_RESET_SECS` seconds. A single
# probe is then allowed (half-open); success closes the breaker, failure
# re-opens it. Defaults tuned for typical home-lab + production shapes:
#   - embedded: 5 failures / 30s reset — kernel hiccups recover fast,
#     and a stale CUDA context usually clears itself on the next call.
#   - cpu_fallback: 3 / 30s — when the local llama-server is down,
#     every call burns its full ~30s timeout. 3 strikes catches it quick.
#   - primary: 3 / 60s — primary outages are usually upstream
#     (LM Studio / Ollama crash), longer reset to avoid retry storms
#     during a real outage.
# Set any to 0 to disable that breaker (Python fallback retains the
# pre-breaker behavior — try every call, eat the timeout).
EMBED_BREAKER_EMBEDDED_THRESHOLD: int  # accessor (registry)
EMBED_BREAKER_EMBEDDED_RESET_SECS: float  # accessor (registry)
EMBED_BREAKER_CPU_FALLBACK_THRESHOLD: int  # accessor (registry)
EMBED_BREAKER_CPU_FALLBACK_RESET_SECS: float  # accessor (registry)
EMBED_BREAKER_PRIMARY_THRESHOLD: int  # accessor (registry)
EMBED_BREAKER_PRIMARY_RESET_SECS: float  # accessor (registry)


# ──────────────────────────────────────────────────────────────────────────────
# Dedup, contradiction, supersede
# ──────────────────────────────────────────────────────────────────────────────
DEDUP_LIMIT: int  # accessor (registry)
DEDUP_THRESHOLD: float  # accessor (registry)
CONTRADICTION_THRESHOLD: float  # accessor (registry)
# SUPERSEDES_PENALTY: at retrieval time, hits that appear as the to_id of a
# 'supersedes' edge (i.e., their newer version exists) get score multiplied
# by this factor. 0.5 = visible but ranked below newer fact. 0.0 = hide.
# 1.0 = disable demotion (legacy pre-2026-04-27 behavior).
SUPERSEDES_PENALTY: float  # accessor (registry)

# CONTRADICTION_TITLE_GATE: 'strict' = require title substring match (legacy);
# 'loose' = cosine >= threshold + same type + content-differs only (default
# since 2026-04-27); 'off' = treat ALL high-cosine same-type pairs as
# supersedence regardless of title or content (research mode only).
CONTRADICTION_TITLE_GATE: str  # accessor (registry)

# CONTRADICTION_TYPE_EXCLUSIONS: comma-separated memory types skipped during
# contradiction-check. Default skips 'conversation'; set to
# 'conversation,message' to restore the legacy pre-2026-04-27 behavior. Empty
# string = check all types.
CONTRADICTION_TYPE_EXCLUSIONS: frozenset[str]  # accessor (registry)

# ──────────────────────────────────────────────────────────────────────────────
# Retrieval params
# ──────────────────────────────────────────────────────────────────────────────
SEARCH_ROW_CAP: int  # accessor (registry)
LLM_TIMEOUT: float  # accessor (registry)
SPEAKER_IN_TITLE: bool  # accessor (registry)
SHORT_TURN_THRESHOLD: int  # accessor (registry)
TITLE_MATCH_BOOST: float  # accessor (registry)
IMPORTANCE_WEIGHT: float  # accessor (registry)

# ── Confidence & trust (knowledge-maintenance) ───────────────────────────────
# All default OFF / neutral: nothing about retrieval changes until explicitly
# enabled. See docs/plans/KNOWLEDGE_MAINTENANCE_PLAN.md.
#
# M3_CONFIDENCE_RANKING: when '1', blend a memory's stored `confidence` into the
# retrieval score as an additive term (like IMPORTANCE_WEIGHT). Default '0' so
# flag-off ranking stays byte-identical to today.
CONFIDENCE_RANKING: bool  # accessor (registry)
# Weight of the confidence term when CONFIDENCE_RANKING is on.
CONFIDENCE_WEIGHT: float  # accessor (registry)
# Which confidence representation drives ranking: 'transparent' (the stored,
# user-facing aggregate) or 'bayesian' (the Beta posterior mean kept alongside,
# for experiments). The displayed `confidence` is always the transparent value.
# RETIRED (user decision, 2C.1): no consumer; kept for importers. Env has no effect.
CONFIDENCE_MODEL: str = "transparent"

# M3_ENFORCE_AGENT_ISOLATION: when '1', a search that supplies a requesting agent
# is restricted to that agent's OWN scope='agent' (private) memories plus all
# shared scopes (org/user/session) — it can never see ANOTHER agent's private
# notes. Default '0' so behavior stays byte-identical to today (scope filtering
# remains caller-applied/advisory unless a caller opts in per-request). This is
# the SQL-layer enforcement of the scope model documented in MULTI_AGENT.md.
# Enforcement also activates per-request whenever a `requesting_agent` is passed,
# independent of this flag (see memory_search_scored_impl).
ENFORCE_AGENT_ISOLATION: bool  # accessor (registry)
# When '1', allow the daily maintenance pass to nudge agent trust from observed
# contradiction/corroboration. Default '0' — explicit agent_set_trust only.
# RETIRED (user decision, 2C.1): no consumer; kept for importers. Env has no effect.
TRUST_AUTOTUNE: bool = False
# When '1', allow the background job to run autonomous episodic->semantic
# belief consolidation. Default '0' — manual/curator-triggered only.
CONSOLIDATION_AUTO: bool  # accessor (registry)
# When '1', a near-identical re-write (cosine >= CORROBORATION_THRESHOLD AND same
# content) records a `corroborates` event + bumps the existing memory's
# corroboration_count/confidence instead of creating an orphan duplicate row.
# Default '0' — write behavior is unchanged until explicitly enabled.
CORROBORATION: bool  # accessor (registry)
# Cosine floor for treating a high-similarity, same-content write as
# corroboration (vs. CONTRADICTION_THRESHOLD for different-content). Higher than
# the contradiction threshold so only true near-duplicates corroborate.
CORROBORATION_THRESHOLD: float  # accessor (registry)

# Trim-by-elbow (MMR post-filter) params. Pre-Phase-7+8 the safety knobs
# defaulted to 20/8/0.05 (scale-aware, prevented the "1-result collapse"
# at large pools); the refactor lowered them to 5/3/0.08, which made the
# trimmer over-aggressive on small pools. Restored.
ELBOW_MIN_INPUT: int  # accessor (registry)
ELBOW_MIN_RETURN: int  # accessor (registry)
ELBOW_ABS_THRESHOLD: float  # accessor (registry)

# Routed-expansion params
# Expansion-displacement guard. Engine invariant, not a per-call tuning knob.
# Pre-Phase-7+8 default was 1.75x; the refactor accidentally changed this to
# 0.05 which is <= 1.0, disabling the guard entirely. Restored.
EXPANSION_DISPLACEMENT_MARGIN: float  # accessor (registry)
EXPANSION_PROTECTED_RANKS: int  # accessor (registry)

# Entity stoplist (case-insensitive) for BFS seeding/expansion
ENTITY_SEED_STOPLIST: frozenset[str]  # accessor (registry)

# ──────────────────────────────────────────────────────────────────────────────
# Ingestion emitters
# ──────────────────────────────────────────────────────────────────────────────
INGEST_WINDOW_CHUNKS: bool  # accessor (registry)
INGEST_GIST_ROWS: bool  # accessor (registry)
INGEST_EVENT_ROWS: bool  # accessor (registry)

INGEST_WINDOW_SIZE: int  # accessor (registry)
INGEST_GIST_MIN_TURNS: int  # accessor (registry)
INGEST_GIST_STRIDE: int  # accessor (registry)

# ──────────────────────────────────────────────────────────────────────────────
# Query Routing
# ──────────────────────────────────────────────────────────────────────────────
QUERY_TYPE_ROUTING: bool  # accessor (registry)
INTENT_ROUTING: bool  # accessor (registry)
INTENT_USER_FACT_BOOST: float  # accessor (registry)
# Additive boost applied to a `procedure`-type row when the query intent is
# 'procedural' ("how do I X"). Mirrors INTENT_USER_FACT_BOOST exactly — gated by
# M3_INTENT_ROUTING, and off (intent != 'procedural') ⇒ byte-identical ranking.
INTENT_PROCEDURAL_BOOST: float  # accessor (registry)

# ──────────────────────────────────────────────────────────────────────────────
# Fact Enrichment & Entities
# ──────────────────────────────────────────────────────────────────────────────
ENABLE_FACT_ENRICHED: bool  # accessor (registry)
FACT_ENRICH_CONCURRENCY: int  # accessor (registry)
FACT_ENRICH_MAX_ATTEMPTS: int  # accessor (registry)

ENABLE_ENTITY_GRAPH: bool  # accessor (registry)
ENTITY_EXTRACT_CONCURRENCY: int  # accessor (registry)
ENTITY_EXTRACT_MAX_ATTEMPTS: int  # accessor (registry)

ENTITY_RESOLVE_FUZZY_MIN: float  # accessor (registry)
ENTITY_RESOLVE_COSINE_MIN: float  # accessor (registry)

# ──────────────────────────────────────────────────────────────────────────────
# Misc
# ──────────────────────────────────────────────────────────────────────────────
VALID_SCOPES: set[str] = {"user", "session", "agent", "org"}

# ──────────────────────────────────────────────────────────────────────────────
# Entity vocab bootstrap defaults (mirrored in config/lists/entity_graph_default.yaml)
# ──────────────────────────────────────────────────────────────────────────────
_DEFAULT_VALID_ENTITY_TYPES: frozenset[str] = frozenset({
    # Human-life active (v2)
    "person", "place", "organization", "event", "date",
    "quantity", "preference", "product", "topic",
    # Human-life legacy (preserved-but-deprecated)
    "legacy_concept", "legacy_object",
    # Technical: homelab infrastructure (m3)
    "host", "container", "service", "device", "ip_address", "vlan",
    "port", "mac_address", "endpoint_url", "firewall_rule",
    # Technical: code + software engineering (m3)
    "file_path", "function", "class_or_table", "cli_flag", "env_var",
    "module", "commit_or_branch", "migration",
    # Technical: benchmark + ML (m3)
    "benchmark", "model", "variant", "metric", "dataset_field",
    "bench_artifact", "task_category", "bench_run_id",
    # Technical: memory-system primitives (m3)
    "memory_id", "memory_type", "task_id",
    # Technical: misc (m3)
    "protocol", "datetime",
})
_DEFAULT_VALID_ENTITY_PREDICATES: frozenset[str] = frozenset({
    # Cross-domain: provenance/aliasing
    "mentions", "same_as", "source_of",
    # Cross-domain: change
    "supersedes",
    # Human-life Layer 2: stable person attributes
    "located_in", "works_at", "family_of", "knows", "prefers", "owns",
    # Human-life Layer 3: event/object attributes
    "has_participant", "has_location", "has_time", "has_quantity",
    # Human-life legacy (preserved-but-deprecated)
    "before", "after",
    # Technical: infrastructure topology (m3)
    "runs_on", "hosts", "listens_on", "assigned_ip", "on_vlan",
    "fails_over_to",
    # Technical: code structure (m3)
    "defined_in", "imports", "calls",
    # Technical: provenance (m3)
    "references", "introduced_in", "deprecates",
    # Technical: bench/ML (m3)
    "measured_on", "uses_model", "judged_by", "produced_artifact",
    "affects_category",
    # Technical: human signals (m3)
    "authorizes_budget",
})

DEFAULT_ENTITY_VOCAB_YAML: Path = (
    Path(BASE_DIR) / "config" / "lists" / "entity_graph_default.yaml"
)
# Env override: when set, load_entity_vocab(None) reads this YAML instead of
# DEFAULT_ENTITY_VOCAB_YAML.
_ENV_ENTITY_VOCAB_YAML: str | None  # accessor (registry)


# ──────────────────────────────────────────────────────────────────────────────
# Reranker (lazy-loaded in memory_core; this is just the default model name)
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_RERANK_MODEL: str  # accessor (registry)


# ──────────────────────────────────────────────────────────────────────────────
# Change-agent
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_CHANGE_AGENT: str = "unknown"

AUTO_RELATED_LINK: bool  # accessor (registry)
AUTO_RELATED_LINK_SCOPE_BY_VARIANT: bool  # accessor (registry)

# ──────────────────────────────────────────────────────────────────────────────
# Tier 4 Cloud Enclave & Failover Configurations (Milestone 3)
# ──────────────────────────────────────────────────────────────────────────────
M3_ALLOW_CLOUD_FALLBACK: bool  # accessor (registry)
M3_CLOUD_ENCLAVE_URL: str | None  # accessor (registry)
M3_CLOUD_AUTH_TOKEN_KEYRING: str | None  # accessor (registry)
# RETIRED (user decision, 2C.1): no consumer; kept for importers. Env has no effect.
M3_CLOUD_MINIMIZATION_LEVEL: str = "standard"

# Circuit Breaker for Tier 4 Cloud Enclave
EMBED_BREAKER_CLOUD_THRESHOLD: int  # accessor (registry)
EMBED_BREAKER_CLOUD_RESET_SECS: float  # accessor (registry)


# ──────────────────────────────────────────────────────────────────────────────
# Registry-backed names: read at CALL time (plan 2D.1)
# ──────────────────────────────────────────────────────────────────────────────
# The names annotated "# accessor (registry)" above have no module binding; this
# table computes each from the config snapshot when it is read, so m3_config.toml,
# a reload and the m3_settings test fixture all reach `config.X`. Keys that are
# not reloadable return the value frozen at first load (settings.py), so for them
# a read is what the import-time constant was, at ~250 ns instead of ~16 (§8:
# none is read per row). A module __getattr__ does not serve a bare-name lookup,
# so code in THIS module reaches these names through _get().
_ACCESSORS: dict[str, Callable[[], Any]] = {
    "EMBED_MODEL": lambda: _setting("embed.model"),
    "EMBED_DIM": lambda: _setting("embed.dim"),
    "EMBED_REQUIRE_UNIT_NORM": lambda: _setting("embed.require_unit_norm"),
    "EMBED_NORM_TOL": lambda: _setting("embed.norm_tolerance"),
    "EMBED_SPACE_TAG": lambda: (_setting("embed.space_tag") or "").strip() or _get("EMBED_MODEL"),
    "EMBED_FALLBACK_MODEL_TAG": lambda: (_setting("embed.fallback_model_tag") or "").strip() or _get("EMBED_MODEL"),
    "EMBED_COMPATIBLE_MODELS": lambda: tuple(_setting("embed.compatible_models")),
    "EMBED_SEARCH_DEADLINE_S": lambda: _setting("embed.search_deadline_s"),
    "ORIGIN_DEVICE": lambda: _setting("identity.origin_device") or os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or platform.node(),
    "DB_BACKEND": lambda: _setting("db.backend"),
    "EMBED_BREAKER_EMBEDDED_THRESHOLD": lambda: _setting("embed.breaker.embedded_threshold"),
    "EMBED_BREAKER_EMBEDDED_RESET_SECS": lambda: _setting("embed.breaker.embedded_reset_s"),
    "EMBED_BREAKER_CPU_FALLBACK_THRESHOLD": lambda: _setting("embed.breaker.cpu_fallback_threshold"),
    "EMBED_BREAKER_CPU_FALLBACK_RESET_SECS": lambda: _setting("embed.breaker.cpu_fallback_reset_s"),
    "EMBED_BREAKER_PRIMARY_THRESHOLD": lambda: _setting("embed.breaker.primary_threshold"),
    "EMBED_BREAKER_PRIMARY_RESET_SECS": lambda: _setting("embed.breaker.primary_reset_s"),
    "DEDUP_LIMIT": lambda: _setting("memory.dedup.limit"),
    "DEDUP_THRESHOLD": lambda: _setting("memory.dedup.threshold"),
    "CONTRADICTION_THRESHOLD": lambda: _setting("memory.contradiction.threshold"),
    "SUPERSEDES_PENALTY": lambda: _setting("search.supersedes_penalty"),
    "CONTRADICTION_TITLE_GATE": lambda: _setting("memory.contradiction.title_gate").lower(),
    "CONTRADICTION_TYPE_EXCLUSIONS": lambda: frozenset(t.lower() for t in _setting("memory.contradiction.type_exclusions")),
    "SEARCH_ROW_CAP": lambda: _setting("search.row_cap"),
    "LLM_TIMEOUT": lambda: _setting("llm.timeout_s"),
    "SPEAKER_IN_TITLE": lambda: _setting("search.speaker_in_title"),
    "SHORT_TURN_THRESHOLD": lambda: _setting("search.short_turn_threshold"),
    "TITLE_MATCH_BOOST": lambda: _setting("search.title_match_boost"),
    "IMPORTANCE_WEIGHT": lambda: _setting("search.importance_weight"),
    "CONFIDENCE_RANKING": lambda: _setting("search.confidence_ranking"),
    "CONFIDENCE_WEIGHT": lambda: _setting("search.confidence_weight"),
    "ENFORCE_AGENT_ISOLATION": lambda: _setting("memory.enforce_agent_isolation"),
    "CONSOLIDATION_AUTO": lambda: _setting("autonomy.consolidation_auto"),
    "CORROBORATION": lambda: _setting("memory.corroboration.enabled"),
    "CORROBORATION_THRESHOLD": lambda: _setting("memory.corroboration.threshold"),
    "ELBOW_MIN_INPUT": lambda: _setting("search.elbow_min_input"),
    "ELBOW_MIN_RETURN": lambda: _setting("search.elbow_min_return"),
    "ELBOW_ABS_THRESHOLD": lambda: _setting("search.elbow_abs_threshold"),
    "EXPANSION_DISPLACEMENT_MARGIN": lambda: _setting("search.expansion_displacement_margin"),
    "EXPANSION_PROTECTED_RANKS": lambda: _setting("search.expansion_protected_ranks"),
    "ENTITY_SEED_STOPLIST": lambda: frozenset(w.lower() for w in _setting("entity.seed_stoplist")),
    "INGEST_WINDOW_CHUNKS": lambda: _setting("ingest.window_chunks"),
    "INGEST_GIST_ROWS": lambda: _setting("ingest.gist_rows"),
    "INGEST_EVENT_ROWS": lambda: _setting("ingest.event_rows"),
    "INGEST_WINDOW_SIZE": lambda: _setting("ingest.window_size"),
    "INGEST_GIST_MIN_TURNS": lambda: _setting("ingest.gist_min_turns"),
    "INGEST_GIST_STRIDE": lambda: _setting("ingest.gist_stride"),
    "QUERY_TYPE_ROUTING": lambda: _setting("search.query_type_routing"),
    "INTENT_ROUTING": lambda: _setting("search.intent_routing"),
    "INTENT_USER_FACT_BOOST": lambda: _setting("search.intent_user_fact_boost"),
    "INTENT_PROCEDURAL_BOOST": lambda: _setting("search.intent_procedural_boost"),
    "ENABLE_FACT_ENRICHED": lambda: _setting("memory.fact_enriched"),
    "FACT_ENRICH_CONCURRENCY": lambda: _setting("memory.fact_enrich_concurrency"),
    "FACT_ENRICH_MAX_ATTEMPTS": lambda: _setting("memory.fact_enrich_max_attempts"),
    "ENABLE_ENTITY_GRAPH": lambda: _setting("memory.entity_graph"),
    "ENTITY_EXTRACT_CONCURRENCY": lambda: _setting("entity.extract_concurrency"),
    "ENTITY_EXTRACT_MAX_ATTEMPTS": lambda: _setting("entity.extract_max_attempts"),
    "ENTITY_RESOLVE_FUZZY_MIN": lambda: _setting("entity.resolve_fuzzy_min"),
    "ENTITY_RESOLVE_COSINE_MIN": lambda: _setting("entity.resolve_cosine_min"),
    "_ENV_ENTITY_VOCAB_YAML": lambda: str(_setting("entity.vocab_yaml")) if _setting("entity.vocab_yaml") is not None else None,
    "DEFAULT_RERANK_MODEL": lambda: _setting("search.rerank_model"),
    "AUTO_RELATED_LINK": lambda: _setting("memory.auto_related_link"),
    "AUTO_RELATED_LINK_SCOPE_BY_VARIANT": lambda: _setting("memory.auto_related_link_scope_by_variant"),
    "M3_ALLOW_CLOUD_FALLBACK": lambda: _setting("cloud.allow_fallback"),
    "M3_CLOUD_ENCLAVE_URL": lambda: (_setting("cloud.enclave_url") or "").strip() or None,
    "M3_CLOUD_AUTH_TOKEN_KEYRING": lambda: (_setting("cloud.auth_token_keyring") or "").strip() or None,
    "EMBED_BREAKER_CLOUD_THRESHOLD": lambda: _setting("embed.breaker.cloud_threshold"),
    "EMBED_BREAKER_CLOUD_RESET_SECS": lambda: _setting("embed.breaker.cloud_reset_s"),
}

#: The names served by __getattr__; memory_core's shim never caches them.
SETTING_NAMES: frozenset[str] = frozenset(_ACCESSORS)


def _get(name: str) -> Any:
    return _ACCESSORS[name]()


def __getattr__(name: str) -> Any:
    try:
        return _ACCESSORS[name]()
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


def __dir__() -> list[str]:
    return sorted({*globals(), *_ACCESSORS})
