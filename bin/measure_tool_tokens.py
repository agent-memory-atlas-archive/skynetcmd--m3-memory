#!/usr/bin/env python3
"""measure_tool_tokens.py — quantify token cost of MCP tool schemas.

Usage:
    python bin/measure_tool_tokens.py

Reports the tokenized size of:
  - Full repertoire (every tool the proxy can dispatch)
  - Lazy-mode startup set (tools_config.DEFAULT_STARTUP_TOOLS — what an agent
    pays at session start with the shipped default)
  - Per-domain cost (what `tools_load_domain(domain=…)` adds on demand)

Uses tiktoken if available (matches OpenAI/Claude tokenization closely);
falls back to a 4-chars-per-token approximation otherwise.

Run this whenever the catalog grows or descriptions change so the numbers
in CLAUDE.md / GEMINI.md / README.md / docs/* stay honest.
"""

from __future__ import annotations

import json
import os
import sys
from typing import cast

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))

import mcp_proxy  # noqa: E402

try:
    import tiktoken

    _enc = tiktoken.get_encoding("cl100k_base")

    def tok(s: str) -> int:
        return len(_enc.encode(s))

    METHOD = "tiktoken cl100k_base"
except ImportError:

    def tok(s: str) -> int:
        return max(1, len(s) // 4)

    METHOD = "approximate (1 token ≈ 4 chars)"


def schema_tokens(tools: list) -> int:
    """Proxy-shape cost: the OpenAI function-calling envelope, as mcp_proxy sends it."""
    return sum(tok(json.dumps(t, separators=(",", ":"))) for t in tools)


def mcp_schema_tokens(names: list[str]) -> int:
    """MCP-shape cost: what an MCP client actually pays at session init.

    The wire shape is {name, description, inputSchema, annotations} — NO OpenAI
    {"type":"function","function":{...}} envelope. That envelope is ~5 tokens per
    tool, so counting it here attributed the proxy's framing to every MCP client
    (45 tokens high on the 9-tool startup set, ~575 on the full 115).
    """
    import mcp_tool_catalog
    from catalog.annotations import tool_annotations

    by_name = {t.name: t for t in mcp_tool_catalog.MCP_TOOLS}
    total = 0
    for n in names:
        spec = by_name.get(n)
        if spec is None:
            continue
        total += tok(
            json.dumps(
                {
                    "name": spec.name,
                    "description": spec.description or "",
                    "inputSchema": spec.parameters or {},
                    "annotations": tool_annotations(spec),
                },
                separators=(",", ":"),
            )
        )
    return total


TYPICAL_DOMAINS = ("memory", "admin")  # README's "typical active session"


def measure() -> dict:
    """The figures README.md and memory_bridge quote, all on the MCP wire: eager
    (every tool), lazy (startup set), and typical (startup set plus the
    TYPICAL_DOMAINS a session loads). The one owner; tests/test_tool_token_budget
    compares the published figures to it."""
    import mcp_tool_catalog as cat
    import tool_domains as td

    # MCP_TOOLS, the list memory_bridge registers: TOOLS also holds CLI-only
    # specs (chatlog_set_redaction), which counted one tool too many.
    all_names = [t.name for t in cat.MCP_TOOLS]
    startup = sorted(td.ESSENTIAL_TOOL_NAMES & set(all_names))
    typical = set(startup)
    for domain in TYPICAL_DOMAINS:
        typical |= set(td.domain_tool_names(all_names, domain))
    return {
        "method": METHOD,
        "eager_tools": len(all_names),
        "eager_tokens": mcp_schema_tokens(all_names),
        "lazy_tools": len(startup),
        "lazy_tokens": mcp_schema_tokens(startup),
        "typical_tools": len(typical),
        "typical_tokens": mcp_schema_tokens(sorted(typical)),
    }


def main() -> int:
    import tool_domains as td

    # Full repertoire: every tool the proxy can dispatch (protocol + debug +
    # the whole catalog, destructive tools included), independent of lazy-mode
    # gating and of how this process's proxy was configured.
    catalog = mcp_proxy.catalog_schemas(include_destructive=True)[0]
    full = mcp_proxy.PROTOCOL_TOOLS + mcp_proxy.DEBUG_TOOLS + catalog

    # Schemas are OpenAI function-calling shape: {"type":"function","function":{"name":…}}.
    def _name(t: dict) -> str:
        return cast("str", t.get("function", {}).get("name") or t.get("name", ""))

    by_name = {_name(t): t for t in full if _name(t)}

    # The shipped startup set, from its owner (tools_config via tool_domains).
    # Intersect with the real schemas so a name without one cannot inflate it.
    startup_names = sorted(td.ESSENTIAL_TOOL_NAMES)
    essentials = [by_name[n] for n in startup_names if n in by_name]

    all_names = [_name(t) for t in full if _name(t)]
    m = measure()
    print(
        f"Typical session (lazy + memory + admin, MCP wire): {m['typical_tools']} tools, {m['typical_tokens']} tokens"
    )
    rows: list[tuple[str, int, int]] = [
        ("Full repertoire (MCP wire)", len(full), mcp_schema_tokens(all_names)),
        ("Lazy startup (MCP wire)", len(essentials), mcp_schema_tokens(startup_names)),
        ("Full repertoire (proxy envelope)", len(full), schema_tokens(full)),
        ("Lazy startup (proxy envelope)", len(essentials), schema_tokens(essentials)),
    ]

    # Per-domain on-demand cost: what tools_load_domain(domain) adds, counting
    # only the not-already-essential tools in that domain.
    catalog_names = [_name(t) for t in full if _name(t)]
    for domain in sorted(td.DOMAIN_DESCRIPTIONS):
        names = [
            n for n in td.domain_tool_names(catalog_names, domain) if n not in td.ESSENTIAL_TOOL_NAMES and n in by_name
        ]
        if not names:
            continue
        tools = [by_name[n] for n in names]
        rows.append((f"  + domain {domain}", len(tools), schema_tokens(tools)))

    print(f"Token counter: {METHOD}")
    print(f"{'-' * 60}")
    print(f"{'configuration':36s}  {'tools':>6s}  {'tokens':>8s}")
    print(f"{'-' * 60}")
    for name, n_tools, n_tokens in rows:
        print(f"{name:36s}  {n_tools:6d}  {n_tokens:8d}")
    print(f"{'-' * 60}")
    full_tokens = rows[0][2]
    lazy_tokens = rows[1][2]  # MCP-wire lazy startup
    saved = full_tokens - lazy_tokens
    pct = 100.0 * saved / max(1, full_tokens)
    print(f"Lazy-startup savings vs. full injection: {saved} tokens ({pct:.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
