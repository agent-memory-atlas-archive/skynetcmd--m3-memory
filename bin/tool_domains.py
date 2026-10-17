"""Domain-grouped lazy loading for the MCP tool catalog.

Problem this solves
-------------------
m3 ships 100+ MCP tools (`mcp_tool_catalog.MCP_TOOLS`). Their JSON schemas serialize
to ~30,700 tokens on the MCP wire. Paid up-front at session init, that is
~15 % of a 200K context window spent on schemas the agent may never touch —
for every client (Claude Code, Gemini CLI, OpenCode, OpenClaw, claude.ai
connector). Lazy mode brings it to ~3,900 tokens (~2 %).

Approach
--------
Group the tools into ~8 domains (memory, chatlog, files, entity, agent,
tasks, conversations, admin). At MCP startup, expose only a small
"essentials" set (search + write of the main stores) plus a
`tools_load_domain` meta-tool. When the agent calls
`tools_load_domain(domain="files")` we register that domain's tools and
either:

  * Emit `notifications/tools/list_changed` (MCP protocol path) — clients
    that advertise `tools.listChanged` re-fetch the catalog and see the new
    tools natively.
  * Return the schemas as JSON in the tool-call result (fallback) — clients
    that don't support `listChanged` (Gemini CLI today, see issue #13850)
    still get the schemas in-band; the agent can pass arbitrary JSON to
    `mcp_tool_call` style bridges that accept dynamic tool names.

Disable lazy mode with `M3_TOOLS_LAZY=0` to restore the legacy "all
tools at startup" behavior.

Domain assignment is derived from the tool name prefix — see
`domain_of_tool()` below. This avoids touching every ToolSpec, and new
tools land in the right domain automatically as long as they follow the
existing naming convention.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

import tools_config as _tools_config

# ── Domain partition ─────────────────────────────────────────────────────────

# Order matters: first prefix match wins. Longer/more-specific prefixes go
# before short ones. The "admin" domain is the catch-all for cross-cutting
# system tools (notifications, GDPR, embedder mgmt, etc.).
_DOMAIN_PREFIXES: list[tuple[str, str]] = [
    # System-diagnostic tools — separate from admin (which is GDPR /
    # notifications) so users grouping "what's wrong with
    # my install" can load just this bucket.
    ("search_profile", "diagnostics"),
    ("memory_doctor", "diagnostics"),
    ("embedder_status", "diagnostics"),
    ("chatlog_status", "diagnostics"),
    ("files_health", "diagnostics"),
    # Core domain catch-alls (first prefix match wins)
    ("memory", "memory"),
    ("curate_memory", "memory"),
    ("curate_chatlog", "chatlog"),
    ("chatlog", "chatlog"),
    ("files", "files"),
    ("entity", "entity"),
    ("agent", "agent"),
    ("task", "tasks"),
    ("conversation", "conversations"),
    # dispatcher / meta tools (like tools_*, cross-cutting) — route to admin
    ("m3_call", "admin"),
    ("m3_index", "admin"),
    ("m3_help_capabilities", "admin"),
    # cross-cutting / system tools
    ("notify", "admin"),
    ("notifications", "admin"),
    ("enrich", "admin"),
    ("extract", "admin"),
    ("gdpr", "admin"),
    ("embedder", "diagnostics"),
]


# The shipped startup set. OWNED by tools_config.DEFAULT_STARTUP_TOOLS, where
# the measurement behind it lives; this is a derived view for callers that ask
# "is this one of the shipped essentials?". A second literal list here drifted
# from the one the doctor reported (2026-10-17). The bridge registers the
# RESOLVED set (tools_config.resolve_startup_tools), which a user can change.
#
# Gating a tool is not making it unreachable: MCP clients reach any catalog tool
# by name through m3_call, and scripts use the CLI (`m3 memory ...`), which is
# the dominant surface (1,114 CLI calls vs 163 direct MCP calls since 2026-09-17).
ESSENTIAL_TOOL_NAMES: frozenset[str] = frozenset(_tools_config.DEFAULT_STARTUP_TOOLS)


# Human-readable one-liner per domain, used by `tools_list_domains` so the
# agent knows what each domain is for without expanding it first. Keep these
# short — they're paid at session start.
DOMAIN_DESCRIPTIONS: dict[str, str] = {
    "memory": "Curated long-term memory: write/search/graph/dedup/retention.",
    "chatlog": "Captured agent-conversation turns with promotion + cost reports.",
    "files": "Directory ingestion, file-level supersession, hybrid search over docs.",
    "entity": "Knowledge-graph entities — search, fetch, list mentions.",
    "agent": "Multi-agent registration, heartbeat, presence.",
    "tasks": "Task creation, assignment, tree, results.",
    "conversations": "Conversation start/append/search/summarize.",
    "admin": "Notifications, enrichment, GDPR.",
    "diagnostics": "Self-service health probes: embedder_status, memory_doctor (run when search hangs or embeds look wrong), search_profile (where a search spends its time).",
}


def domain_of_tool(tool_name: str) -> str:
    """Return the domain for a given tool name, or 'admin' if unmatched."""
    for prefix, domain in _DOMAIN_PREFIXES:
        if tool_name == prefix or tool_name.startswith(prefix + "_") or tool_name.startswith(prefix):
            return domain
    return "admin"


def group_by_domain(tool_names: Iterable[str]) -> dict[str, list[str]]:
    """Group an iterable of tool names by domain. Returns {domain: [names...]}."""
    out: dict[str, list[str]] = defaultdict(list)
    for n in tool_names:
        out[domain_of_tool(n)].append(n)
    return dict(out)


def is_essential(tool_name: str) -> bool:
    """Is this tool always exposed at MCP startup?"""
    return tool_name in ESSENTIAL_TOOL_NAMES


def domain_tool_names(all_tool_names: Iterable[str], domain: str) -> list[str]:
    """All tool names belonging to a given domain, sorted."""
    return sorted(n for n in all_tool_names if domain_of_tool(n) == domain)
