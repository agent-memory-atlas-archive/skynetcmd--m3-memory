"""catalog.tools_diagnostics — diagnostics-domain ToolSpec entries (4).

Split out of mcp_tool_catalog.py's flat TOOLS list. Entries copied verbatim
(name, description, parameters, impl, validators, flags unchanged). Domain
assignment per tool_domains.domain_of_tool().
"""

from __future__ import annotations

from m3_core.search_modes import PUBLIC_SEARCH_MODES

from .lazy import LazyImpl, LazyModuleProxy
from .spec import ToolSpec

memory_core = LazyModuleProxy("memory_core")


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="search_profile",
        description=(
            "Run one real memory search and report where its time went, per stage: "
            "embed (query vector), hybrid_score, rank, mmr, rerank, expand, "
            "fts_fallback, db (driver calls), total, and unattributed (inline work "
            "outside every stage). Use it to see whether the embedder, the database "
            "(e.g. a remote PostgreSQL) or ranking is the bottleneck. Timing is "
            "local to the search functions (Python 3.12 sys.monitoring) and exists "
            "only during this call. Returns {query, hits, elapsed_ms, "
            "unattributed_ms, stages: {name: {calls, wall_ms, self_ms, max_ms, errors}}}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query to profile."},
                "k": {"type": "integer", "description": "Results to retrieve.", "default": 8},
                "search_mode": {
                    "type": "string",
                    "enum": list(PUBLIC_SEARCH_MODES),
                    "description": "Retrieval mode.",
                    "default": "hybrid",
                },
                "user_id": {"type": "string", "description": "Tenant filter (optional).", "default": ""},
                "scope": {"type": "string", "description": "Scope filter (optional).", "default": ""},
            },
            "required": ["query"],
        },
        impl=LazyImpl("memory.search_profile", "search_profile_impl"),
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="embedder_status",
        description="Check the status of the local sovereign embedder server (default port 8082, override via M3_EMBED_FALLBACK_URL).",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        impl=memory_core.embedder_status_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_doctor",
        description=(
            "Self-service diagnostic for the m3-memory embedding cascade. "
            "Probes tier-1 (in-proc GGUF), tier-2 (m3-embed-server :8082), "
            "DB integrity, and end-to-end embed roundtrip — all concurrently "
            "with bounded 2s per-probe timeouts. Returns a structured dict "
            "with status ('healthy' | 'degraded' | 'broken'), per-tier "
            "details, issues, and actionable recommendations. Use this when "
            "memory_search hangs, embeddings look wrong, or you're standing "
            "up a new deployment."
        ),
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        impl=memory_core.memory_doctor_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_doctor_fix",
        description=(
            "Run the m3-memory self-repair mode (m3 doctor --fix). "
            "Attempts to auto-fix the most common deployment issues in order: "
            "(1) apply pending SQLite migrations, "
            "(2) rebuild the FTS5 full-text index, "
            "(3) embed-backfill items that are missing vector embeddings (capped at 500/run), "
            "(4) rebuild the m3_system_cohesion table if absent. "
            "Set dry_run=True to see what *would* be done without making any changes. "
            "Returns a structured dict with per-action outcomes and a summary status "
            "('ok' | 'partial' | 'nothing_to_do' | 'failed')."
        ),
        parameters={
            "type": "object",
            "properties": {
                "dry_run": {
                    "type": "boolean",
                    "description": ("If true, report what would be done without writing anything. Default: false."),
                    "default": False,
                },
            },
            "required": [],
        },
        impl=memory_core.memory_doctor_fix_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
]
