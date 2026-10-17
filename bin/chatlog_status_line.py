"""chatlog_status_line.py — anomaly-only status line generator.

Keystroke-fast: reads state file only, no DB. Prints one tag or nothing.
Exit 0 always.

Shows highest-severity anomaly when multiple fire.
Order: regex_errors > silent_hook > spill_quarantined > spill > queue_backpressure > embed_backlog.

Respects env:
- CHATLOG_STATUSLINE=off → no output
- CHATLOG_STATUSLINE_ASCII=1 → use [!] instead of ⚠
"""

from __future__ import annotations

import os
import sys
from typing import Any

from m3_core.config import settings as _m3cfg

try:
    import chatlog_config
except ImportError:
    sys.path.insert(0, os.path.dirname(__file__))
    import chatlog_config


def _load_state_file() -> dict[str, Any]:
    # chatlog_config.read_state is the one reader: a copy here read a corrupt
    # state file as {} without a word, so status showed "no writes" silently.
    return chatlog_config.read_state()


def _humanize_bytes(b: int) -> str:
    if b < 1024:
        return f"{b}B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f}KB"
    if b < 1024 * 1024 * 1024:
        return f"{b / (1024 * 1024):.1f}MB"
    return f"{b / (1024 * 1024 * 1024):.1f}GB"


def chatlog_status_line() -> str:
    if not _m3cfg.setting("chatlog.statusline"):  # M3_CHATLOG_STATUSLINE=off
        return ""

    state = _load_state_file()
    config = chatlog_config.resolve_config()

    hooks_enabled = any(h.enabled for h in config.host_agents.values())
    if not state and not hooks_enabled:
        return ""

    warning = ""

    if config.redaction.enabled:
        regex_errors = state.get("redaction", {}).get("regex_errors", [])
        if regex_errors:
            warning = "chatlog: redaction regex err"

    if not warning:
        for hook_name, hook_spec in config.host_agents.items():
            if hook_spec.enabled:
                hook_state = state.get("hooks", {}).get(hook_name, {})
                hook_last_write_ms = hook_state.get("last_write_ms_ago")
                if hook_last_write_ms is not None and hook_last_write_ms > 86_400_000:
                    warning = f"chatlog: {hook_name} silent 24h+"
                    break

    if not warning:
        q_files = (state.get("spill") or {}).get("quarantine_files") or 0
        if q_files:
            warning = f"chatlog: {q_files} spill file(s) quarantined"

    if not warning:
        spill = state.get("spill", {})
        spill_bytes = spill.get("bytes", 0)
        spill_oldest_ms = spill.get("oldest_ms_ago")
        if spill_bytes > 0 and spill_oldest_ms is not None and spill_oldest_ms > 3_600_000:
            warning = f"chatlog: spill={_humanize_bytes(spill_bytes)}"

    if not warning:
        # The writer records state["queue_depth"] after each flush.
        depth = state.get("queue_depth", 0)
        max_depth = config.queue_max_depth
        if isinstance(depth, int) and max_depth > 0 and depth / max_depth > 0.8:
            warning = f"chatlog: queue {depth}/{max_depth}"

    if not warning:
        # The sweeper records state["embed_backlog"] (None = its count failed).
        embed_backlog = state.get("embed_backlog")
        if isinstance(embed_backlog, int) and embed_backlog > 10_000:
            warning = f"chatlog: embed backlog {embed_backlog}"

    if not warning:
        return ""

    prefix = "[!]" if _m3cfg.setting("chatlog.statusline_ascii") else "⚠"
    return f"{prefix} {warning}"


def main():
    try:
        output = chatlog_status_line()
        if output:
            print(output)
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
