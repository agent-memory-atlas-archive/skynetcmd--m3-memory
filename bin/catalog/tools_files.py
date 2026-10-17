"""catalog.tools_files — files-domain ToolSpec entries (26).

Split out of mcp_tool_catalog.py's flat TOOLS list. Entries copied verbatim
(name, description, parameters, impl, validators, flags unchanged). Domain
assignment per tool_domains.domain_of_tool().
"""

from __future__ import annotations

from .lazy import LazyModuleProxy
from .spec import ToolSpec

_files_tools = LazyModuleProxy("files_memory.tools")


TOOLS: list[ToolSpec] = [
    # ───────────────────────────────────────────────────────────────────────────
    # files-memory tools (FILE_INGESTION_PLAN.md phases 1-4).
    # All sync; the package's LLM and embed calls block (the embed cascade
    # uses asyncio.run internally, no need to mark these async here).
    # ───────────────────────────────────────────────────────────────────────────
    ToolSpec(
        name="files_ingest",
        description=(
            "Walk a directory and ingest supported files into files.db. "
            "Idempotent: same content_sha256 -> no-op; changed content -> "
            "new file_node version supersedes prior. Use extract_mode to "
            "opt into fact extraction; use original_path (or a "
            "<path>.m3meta.json sidecar) to point search results at a "
            "source-of-truth file when the ingested file is a conversion."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory (or single file) to ingest."},
                "include": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Glob patterns; only matching files are ingested.",
                    "default": None,
                },
                "exclude": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Glob patterns; matching files are skipped.",
                    "default": None,
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Max recursion depth (0 = root only).",
                    "default": None,
                },
                "corpus": {
                    "type": "string",
                    "description": "Corpus tag (default: the configured files.default_corpus).",
                    "default": None,
                },
                "dry_run": {"type": "boolean", "description": "Walk + count without writing.", "default": False},
                "force_size": {"type": "boolean", "description": "Bypass the per-file size cap.", "default": False},
                "record_noops": {
                    "type": "boolean",
                    "description": "Write 'unchanged_skipped' rows for audit.",
                    "default": False,
                },
                "extract_mode": {
                    "type": "string",
                    "enum": ["none", "inline", "queue"],
                    "description": "Fact-extraction mode.",
                    "default": None,
                },
                "original_path": {
                    "type": "string",
                    "description": "Pointer to source artifact when single-file ingest is a conversion.",
                    "default": None,
                },
            },
            "required": ["path"],
        },
        impl=_files_tools.files_ingest_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_search",
        description=(
            "Hybrid FTS5 + vector search over file-ingestion leaves. "
            "Default: current versions only. Set include_history=True for "
            "time-travel queries. Use `corpora` for fan-out across multiple corpora."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text query."},
                "limit": {"type": "integer", "description": "Max hits returned.", "default": 10},
                "corpus": {"type": "string", "description": "Single-corpus scope filter.", "default": None},
                "corpora": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Fan-out across these corpora; overrides corpus.",
                    "default": None,
                },
                "filetype": {
                    "type": "string",
                    "description": "Filter to one filetype ('markdown', 'pdf', ...).",
                    "default": None,
                },
                "include_history": {
                    "type": "boolean",
                    "description": "Include superseded leaves and file_nodes.",
                    "default": False,
                },
            },
            "required": ["query"],
        },
        # Sync: dispatch runs a sync impl off-loop via asyncio.to_thread under the
        # same resolved timeout (catalog.dispatch._run_impl_bounded), so the
        # 2026-07-01 hang stays bounded without a wrapper hiding the signature.
        impl=_files_tools.files_search_impl,
        is_async=False,
        timeout_s=120.0,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_index",
        description=(
            "Return file-level summaries for triage (wiki-index primitive). "
            "Cheap-first retrieval -- no leaf content. Use BEFORE files_search "
            "to decide which files are worth deep-reading."
        ),
        parameters={
            "type": "object",
            "properties": {
                "corpus": {
                    "type": "string",
                    "description": "Single-corpus scope filter (omit for all).",
                    "default": None,
                },
                "corpora": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Fan-out across these corpora; overrides corpus.",
                    "default": None,
                },
                "filetype": {
                    "type": "string",
                    "description": "Filter to one filetype ('markdown', 'pdf', ...).",
                    "default": None,
                },
                "directory": {
                    "type": "string",
                    "description": "Only files whose absolute path starts with this directory.",
                    "default": None,
                },
                "filename_glob": {
                    "type": "string",
                    "description": "Glob over the filename, e.g. '*.md' or 'README*'.",
                    "default": None,
                },
                "include_history": {
                    "type": "boolean",
                    "description": "Include superseded file_nodes.",
                    "default": False,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max entries, newest date_modified first.",
                    "default": 500,
                },
            },
            "required": [],
        },
        impl=_files_tools.files_index_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_get",
        description=(
            "Fetch one record by UUID. Tries file_nodes then leaves; returns the row plus "
            "_kind ('file_node'|'leaf') and original_path, or null if no match."
        ),
        parameters={
            "type": "object",
            "properties": {"uuid": {"type": "string", "description": "UUID of the file_node or leaf."}},
            "required": ["uuid"],
        },
        impl=_files_tools.files_get_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_stats",
        description="Corpus-level counters: file_nodes, leaves, embed coverage, by-filetype.",
        parameters={
            "type": "object",
            "properties": {
                "corpus": {
                    "type": "string",
                    "description": "Limit counters to one corpus (omit for all).",
                    "default": None,
                }
            },
            "required": [],
        },
        impl=_files_tools.files_stats_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_health",
        description=(
            "files.db integrity check: returns ok, sqlite_integrity, fts5_in_sync (with "
            "fts5_drift counts), orphan_leaves and orphan_runs. Read-only unless "
            "rebuild=True, which rebuilds the FTS5 index only when it is out of sync and "
            "re-checks (fts_rebuilt=True). SQLite-only; reports skipped on PostgreSQL."
        ),
        parameters={
            "type": "object",
            "properties": {
                "rebuild": {
                    "type": "boolean",
                    "description": "Rebuild the FTS5 index if out of sync; false only reports.",
                    "default": False,
                }
            },
            "required": [],
        },
        impl=_files_tools.files_health_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_extract_pending",
        description=(
            "Drain leaves with extraction_status='pending' through the LLM "
            "fact extractor. Used after a queue-mode ingest. Safe to call "
            "repeatedly."
        ),
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max pending leaves to process this pass.", "default": 100}
            },
            "required": [],
        },
        impl=_files_tools.files_extract_pending_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_promote",
        description=(
            "Promote (ascend) a fact / leaf / file_summary from files.db "
            "to memory.db. Source stays untouched; copy lands in memory.db "
            "with a metadata back-pointer. Idempotent."
        ),
        parameters={
            "type": "object",
            "properties": {
                "source_uuid": {"type": "string", "description": "UUID of the fact, leaf or file_node to promote."},
                "reason": {
                    "type": "string",
                    "description": "Free-text rationale, stored on the promotion record.",
                    "default": "",
                },
                "mapped_type": {
                    "type": "string",
                    "description": "Override memory.db type (fact|knowledge|reference|...).",
                    "default": None,
                },
                "scope": {
                    "type": "string",
                    "description": "Isolation scope of the new memory (default: configured promotion scope).",
                    "default": None,
                },
                "importance": {"type": "number", "description": "0.0-1.0 relevance of the new memory.", "default": 0.6},
            },
            "required": ["source_uuid"],
        },
        impl=_files_tools.files_promote_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_promotion_list",
        description=(
            "List existing promotions. source_superseded=True surfaces "
            "promotions whose source file has since been superseded -- "
            "candidates for review."
        ),
        parameters={
            "type": "object",
            "properties": {
                "source_file_node": {
                    "type": "string",
                    "description": "Only promotions whose source belongs to this file_node UUID.",
                    "default": None,
                },
                "source_superseded": {
                    "type": "boolean",
                    "description": "True: source file superseded only; false: current only; omit for both.",
                    "default": None,
                },
                "limit": {"type": "integer", "description": "Max rows, newest promotion first.", "default": 100},
            },
            "required": [],
        },
        impl=_files_tools.files_promotion_list_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_promotable",
        description=(
            "List top promotion candidates by usage-weighted heuristic "
            "score. Suggestion-only; use files_promote to actually ascend any."
        ),
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max candidates returned.", "default": 20},
                "min_score": {
                    "type": "number",
                    "description": "Score floor; scores run roughly 0-2 (hits x confidence x recency).",
                    "default": 0.30,
                },
                "corpus": {"type": "string", "description": "Single-corpus scope filter.", "default": None},
                "include_already_promoted": {
                    "type": "boolean",
                    "description": "Also list facts that already have a promotion.",
                    "default": False,
                },
            },
            "required": [],
        },
        impl=_files_tools.files_promotable_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_dedup",
        description=(
            "Scan leaf embeddings for near-duplicates above cosine threshold. "
            "Detection only -- pairs land in semantic_dedup_candidates for "
            "human review."
        ),
        parameters={
            "type": "object",
            "properties": {
                "threshold": {
                    "type": "number",
                    "description": "Cosine floor (0.0-1.0) for flagging a pair.",
                    "default": 0.92,
                },
                "max_pairs": {
                    "type": "integer",
                    "description": "Stop after recording this many new pairs.",
                    "default": 500,
                },
                "leaf_limit": {
                    "type": "integer",
                    "description": "Max leaves scanned, newest first.",
                    "default": 10000,
                },
                "corpus": {"type": "string", "description": "Single-corpus scope filter.", "default": None},
                "include_already_detected": {
                    "type": "boolean",
                    "description": "Re-surface pairs that already have an unreviewed candidate row.",
                    "default": False,
                },
            },
            "required": [],
        },
        impl=_files_tools.files_dedup_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_dedup_list",
        description="List near-duplicate candidate pairs with text snippets and paths.",
        parameters={
            "type": "object",
            "properties": {
                "reviewed": {
                    "type": "boolean",
                    "description": "False: unreviewed only; true: reviewed only; null: both.",
                    "default": False,
                },
                "limit": {"type": "integer", "description": "Max pairs returned.", "default": 100},
                "min_cosine": {"type": "number", "description": "Only pairs with cosine >= this.", "default": None},
            },
            "required": [],
        },
        impl=_files_tools.files_dedup_list_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_dedup_review",
        description=("Record a review decision on a near-duplicate candidate: 'kept' | 'merged' | 'ignored'."),
        parameters={
            "type": "object",
            "properties": {
                "candidate_uuid": {"type": "string", "description": "Candidate pair UUID from files_dedup_list."},
                "action": {
                    "type": "string",
                    "enum": ["kept", "merged", "ignored"],
                    "description": "Decision; 'merged' records intent only, no leaves are merged.",
                },
                "note": {"type": "string", "description": "Free-text rationale saved with the review.", "default": ""},
            },
            "required": ["candidate_uuid", "action"],
        },
        impl=_files_tools.files_dedup_review_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_entity_coalesce",
        description=(
            "Detect provisional-entity coalescing candidates (quarantine noise + "
            "flag near-duplicate entities). Detection only -- never merges, never "
            "auto-applies; candidates land in entity_coalesce_candidates for "
            "review. dry_run=True estimates without writing or embedding."
        ),
        parameters={
            "type": "object",
            "properties": {
                "max_pairs": {
                    "type": "integer",
                    "description": "Stop after recording this many candidate pairs.",
                    "default": 1000,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Estimate counts without writing or embedding.",
                    "default": False,
                },
                "corpus": {
                    "type": "string",
                    "description": "Accepted but currently ignored; detection scans all entities.",
                    "default": None,
                },
            },
            "required": [],
        },
        impl=_files_tools.files_entity_coalesce_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_entity_coalesce_list",
        description="List entity-coalescing candidate pairs (name + score + band).",
        parameters={
            "type": "object",
            "properties": {
                "reviewed": {
                    "type": "boolean",
                    "description": "False: unreviewed only; true: reviewed only; null: both.",
                    "default": False,
                },
                "limit": {"type": "integer", "description": "Max pairs, highest cosine first.", "default": 100},
                "min_cosine": {"type": "number", "description": "Only pairs with cosine >= this.", "default": None},
            },
            "required": [],
        },
        impl=_files_tools.files_entity_coalesce_list_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_entity_coalesce_review",
        description=(
            "Record entity-coalescing review decisions in BULK: a list of "
            "{uuid, action} where action is 'merge' | 'related' | 'reject' | "
            "'defer'. Records intent only; materialize with "
            "files_entity_coalesce_apply."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reviews": {
                    "type": "array",
                    "description": "List of {uuid, action}; action is merge|related|reject|defer.",
                },
                "note": {
                    "type": "string",
                    "description": "Free-text rationale saved on every reviewed candidate.",
                    "default": "",
                },
            },
            "required": ["reviews"],
        },
        impl=_files_tools.files_entity_coalesce_review_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_entity_coalesce_apply",
        description=(
            "Apply the reversible same_as/cluster overlay. Union of explicit "
            "candidate_uuids (reviewed 'merge' or 'unapplied' tombstone) and -- "
            "if include_auto_merge -- the LATEST run's 'merge' band (or "
            "resolution_run). Members are never deleted; reverse with "
            "files_entity_coalesce_unapply. WRITES the core graph: a real apply "
            "MUST pass confirm=True; dry_run=True previews without writing."
        ),
        parameters={
            "type": "object",
            "properties": {
                "candidate_uuids": {
                    "type": "array",
                    "description": "Candidate UUIDs to apply; each must be reviewed 'merge' or 'unapplied'.",
                    "default": None,
                },
                "include_auto_merge": {
                    "type": "boolean",
                    "description": "Also apply unreviewed 'merge'-band candidates of one detection run.",
                    "default": False,
                },
                "resolution_run": {
                    "type": "string",
                    "description": "Detection run_id for include_auto_merge (default: latest run).",
                    "default": None,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview counts without writing; no confirm needed.",
                    "default": False,
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true for a real (non-dry-run) apply.",
                    "default": False,
                },
            },
            "required": [],
        },
        impl=_files_tools.files_entity_coalesce_apply_impl,
        is_async=False,
        validators=(),
        # Reversible by construction (writes a same_as/cluster overlay, never
        # deletes members; unapply fully restores). Not destructive per §6, so
        # not gated here — the impl's own confirm=True guard is the safety.
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_entity_coalesce_unapply",
        description=(
            "Reverse one coalescence cluster (drop edges, clear flags, strip "
            "aliases, tombstone the candidate so auto-merge won't resurrect it). "
            "Members are never deleted; re-apply via files_entity_coalesce_apply "
            "with candidate_uuids."
        ),
        parameters={
            "type": "object",
            "properties": {
                "cluster_id": {
                    "type": "string",
                    "description": "Cluster to reverse, as listed in clusters_touched by the apply.",
                },
            },
            "required": ["cluster_id"],
        },
        impl=_files_tools.files_entity_coalesce_unapply_impl,
        is_async=False,
        validators=(),
        # Pure undo — removes only overlay edges/flags, restores state, deletes
        # no member data. Never gated.
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_staleness_review",
        description=(
            "Compare filesystem against files.db. Surfaces stale, "
            "touched-only, missing, new, failed-extraction, "
            "drifted-promotion files, and rename candidates. Report-only."
        ),
        parameters={
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Restrict the review to files under this directory (default: all known files).",
                    "default": None,
                },
                "corpus": {"type": "string", "description": "Single-corpus scope filter.", "default": None},
                "rehash": {
                    "type": "boolean",
                    "description": "Re-hash changed files (accurate); false uses mtime only (cheaper, flags touches).",
                    "default": True,
                },
                "limit": {"type": "integer", "description": "Max entries returned per category list.", "default": 200},
            },
            "required": [],
        },
        impl=_files_tools.files_staleness_review_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_link_rename",
        description=(
            "Re-point an existing file_node at a new path (rename / move). "
            "NOT a supersession -- content stays identical. Use this only "
            "when staleness review surfaces a rename candidate."
        ),
        parameters={
            "type": "object",
            "properties": {
                "missing_file_node_uuid": {
                    "type": "string",
                    "description": "UUID of the file_node whose file moved (from a rename candidate).",
                },
                "new_path": {"type": "string", "description": "New on-disk location of the same content."},
                "expect_sha256": {
                    "type": "string",
                    "description": "If set, the file at new_path must have this sha256 or the call fails.",
                    "default": None,
                },
            },
            "required": ["missing_file_node_uuid", "new_path"],
        },
        impl=_files_tools.files_link_rename_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_corpus_create",
        description=(
            "Register a new corpus with optional default overrides. "
            "`default=True` marks this corpus as the installation's "
            "default (clears the flag on any prior default in the same "
            "transaction)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "corpus_id": {
                    "type": "string",
                    "description": "New corpus ID; must be non-empty and not already exist.",
                },
                "description": {
                    "type": "string",
                    "description": "Human-readable label for the corpus.",
                    "default": None,
                },
                "extract_mode": {
                    "type": "string",
                    "enum": ["none", "inline", "queue"],
                    "description": "Extraction mode stored in settings; files_ingest does not read it yet.",
                    "default": None,
                },
                "scope": {
                    "type": "string",
                    "description": "Promotion scope stored in settings; files_promote does not read it yet.",
                    "default": None,
                },
                "default": {
                    "type": "boolean",
                    "description": "Flag as the default corpus, clearing any prior flag (stored; ingest does not read it yet).",
                    "default": False,
                },
            },
            "required": ["corpus_id"],
        },
        impl=_files_tools.files_corpus_create_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_corpus_list",
        description=(
            "List every corpus (registered, or seen on ingested files) with corpus_id, settings, "
            "current file_node_count and leaf_count, created_at and is_default."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        impl=_files_tools.files_corpus_list_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_corpus_get",
        description=(
            "Fetch one corpus: settings, current file_node_count and leaf_count, created_at, "
            "is_default. Null if it has neither settings nor files."
        ),
        parameters={
            "type": "object",
            "properties": {"corpus_id": {"type": "string", "description": "Corpus ID to fetch."}},
            "required": ["corpus_id"],
        },
        impl=_files_tools.files_corpus_get_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_corpus_set",
        description=(
            "Update settings for an existing corpus. None args are no-ops. Creates the corpus_settings row if absent."
        ),
        parameters={
            "type": "object",
            "properties": {
                "corpus_id": {"type": "string", "description": "Corpus ID to update."},
                "description": {"type": "string", "description": "New human-readable label.", "default": None},
                "extract_mode": {
                    "type": "string",
                    "enum": ["none", "inline", "queue"],
                    "description": "Extraction mode stored in settings; files_ingest does not read it yet.",
                    "default": None,
                },
                "scope": {
                    "type": "string",
                    "description": "Promotion scope stored in settings; files_promote does not read it yet.",
                    "default": None,
                },
                "default": {
                    "type": "boolean",
                    "description": "True flags as default (clearing others); false clears this corpus's flag.",
                    "default": None,
                },
                "retention_days": {
                    "type": "integer",
                    "description": "Advisory retention in days; stored only, nothing is pruned.",
                    "default": None,
                },
            },
            "required": ["corpus_id"],
        },
        impl=_files_tools.files_corpus_set_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_corpus_delete",
        description=(
            "Delete a corpus's settings row. Cascade=True also deletes "
            "every file_node in the corpus -- DESTRUCTIVE. Without "
            "cascade, refuses when the corpus has file_nodes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "corpus_id": {"type": "string", "description": "Corpus ID to delete."},
                "cascade": {
                    "type": "boolean",
                    "description": "Also delete all its files, leaves, facts and links; false refuses if files exist.",
                    "default": False,
                },
            },
            "required": ["corpus_id"],
        },
        impl=_files_tools.files_corpus_delete_impl,
        is_async=False,
        validators=(),
        # DESTRUCTIVE (cascade deletes every file_node/leaf/fact/embedding in the
        # corpus). Gate behind mcp.allow_destructive like memory_delete /
        # gdpr_forget rather than leaving it default-allowed (§6). Also corrects
        # the m3_index advertisement to destructive:True.
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_watch_once",
        description=(
            "Single-pass staleness check + notification dispatch. Suitable "
            "for cron / scheduled runners. Notifications are emitted via "
            "the memory.db notifications inbox; cooldown suppresses "
            "duplicates within the window."
        ),
        parameters={
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Restrict the review to files under this directory (default: all known files).",
                    "default": None,
                },
                "corpus": {"type": "string", "description": "Single-corpus scope filter.", "default": None},
                "agent_id": {
                    "type": "string",
                    "description": "Agent whose notifications inbox receives the events.",
                    "default": "files_memory.watch",
                },
                "cooldown_seconds": {
                    "type": "number",
                    "description": "Skip re-notifying the same file and event kind within this window.",
                    "default": 3600.0,
                },
            },
            "required": [],
        },
        impl=_files_tools.files_watch_once_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
]
