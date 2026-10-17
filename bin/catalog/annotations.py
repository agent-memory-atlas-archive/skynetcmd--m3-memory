"""catalog.annotations — the MCP tool annotations each catalog tool advertises.

MCP clients read ``readOnlyHint`` / ``destructiveHint`` to decide when to ask
before a call, and registries (Glama's tool-quality score) count them. m3 sent
none until 2026-10-17, although the catalog already knew which tools are
destructive (``ToolSpec.is_destructive``).

The MCP defaults are the cautious ones: a tool without annotations is assumed to
write and to be destructive. So every annotation here is stated explicitly, and
an unlisted tool is treated as a writer (no read-only claim).

Read-only means the tool changes no memory, chat, file, task or agent state the
user can see. Retrieval tools still stamp ``access_count`` / ``last_accessed_at``
(memory.db access-stamp flush); that is bookkeeping for ranking and decay, not a
change a client should confirm, so they count as read-only.
"""

from __future__ import annotations

from typing import Any

READ_ONLY_TOOLS = frozenset(
    {
        "agent_get",
        "agent_list",
        "chatlog_cost_report",
        "chatlog_list_conversations",
        "chatlog_search",
        "chatlog_search_slim",
        "chatlog_status",
        "conversation_search",
        "embedder_status",
        "entity_get",
        "entity_mentions",
        "entity_search",
        "files_corpus_get",
        "files_corpus_list",
        "files_dedup_list",
        "files_entity_coalesce_list",
        "files_get",
        "files_promotable",
        "files_promotion_list",
        "files_search",
        "files_staleness_review",
        "files_stats",
        "gdpr_export",
        "m3_help_capabilities",
        "m3_index",
        "memory_cost_report",
        "memory_count_entities",
        "memory_count_mentions",
        "memory_doctor",
        "memory_export",
        "memory_feedback_stats",
        "memory_get",
        "memory_graph",
        "memory_history",
        "memory_inbox",
        "memory_lifecycle_summary",
        "memory_refresh_queue",
        "memory_search",
        "memory_search_multi_db",
        "memory_search_routed",
        "memory_search_scored",
        "memory_search_slim",
        "memory_suggest",
        "memory_verify",
        "notifications_peek",
        "search_profile",
        "task_get",
        "task_list",
        "task_tree",
        "tools_list_domains",
    }
)


def tool_annotations(spec: Any) -> dict[str, bool]:
    """The annotations ``spec`` advertises, as ToolAnnotations keyword arguments."""
    read_only = spec.name in READ_ONLY_TOOLS
    return {"readOnlyHint": read_only, "destructiveHint": (not read_only) and spec.is_destructive}
