#!/usr/bin/env python3
"""Every agent's SessionStart hook: chat-capture status and the m3 version.

One line in each agent's own config runs it, so the config holds no logic:

    <m3 python> <payload>/bin/hooks/session_start.py --agent claude|codex|gemini|antigravity

Claude Code, Codex and Gemini CLI read the same output, one JSON object whose
``systemMessage`` is shown to the person (Antigravity reads ``injectSteps``
instead; ``render`` writes each agent's shape):

- chat capture (bin/hooks/chatlog/session_start_capture_check.py);
- the running version against the newest (m3_core.version_check reads a record
  refreshed at most daily; this hook never waits on the network).

This is also where the approved session-recall menu (session_recall.recall_menu_text) is delivered, as
``hookSpecificOutput.additionalContext`` on a fresh start only.

Never raises and always prints valid JSON (Gemini rejects any other stdout): a
part that fails is named in the status line, the others still report.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parents[1]
for _p in (str(_BIN), str(_BIN / "hooks" / "chatlog")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

AGENTS = ("claude", "codex", "gemini", "antigravity")


def _capture_line() -> str:
    try:
        from session_start_capture_check import capture_status

        return capture_status()[0]
    except Exception as e:  # noqa: BLE001 -- one part failing must not hide the others
        return f"\U0001f6a8 m3 chatlog check could not run ({type(e).__name__}: {e}). inspect: m3 chatlog doctor"


def _version_line() -> str:
    try:
        from m3_core.version_check import status_line

        return status_line()
    except Exception as e:  # noqa: BLE001
        return f"m3 version unknown ({type(e).__name__}: {e}). inspect: m3 doctor"


# Recall is offered on a fresh start or a clear; resume, compact and fork
# sessions already carry their context.
RECALL_SOURCES = ("startup", "clear", "")


def _recall_menu(agent: str, cwd: str) -> str:
    try:
        from session_recall import recall_menu_text

        return str(recall_menu_text(agent, cwd=cwd) or "")
    except Exception as e:  # noqa: BLE001
        return f"m3 recall unavailable: {type(e).__name__}: {e}; inspect: `m3 doctor`"


def _unsummarized_line() -> str:
    try:
        from session_recall import unsummarized_line

        return unsummarized_line()
    except Exception as e:  # noqa: BLE001 -- one part failing must not hide the others
        return f"m3 recall could not check for unsummarized sessions ({type(e).__name__}: {e}). inspect: m3 doctor"


def _cwd(envelope: dict) -> str:
    # Antigravity names its workspaces (``workspacePaths``) instead of a cwd.
    paths = envelope.get("workspacePaths")
    if isinstance(paths, list) and paths:
        return str(paths[0])
    return str(envelope.get("cwd") or "")


def build(agent: str, envelope: dict) -> dict:
    """The hook's output for ``agent`` given the host's stdin ``envelope``."""
    status = [_capture_line(), _version_line()]
    menu = ""
    # Antigravity's SessionStart carries no source; it fires once per new conversation.
    if agent == "antigravity" or str(envelope.get("source") or "") in RECALL_SOURCES:
        menu = _recall_menu(agent, _cwd(envelope))
        if menu.startswith("m3 recall unavailable"):
            status.append(menu)  # the person sees why there is no menu
            menu = ""
        elif not menu:
            late = _unsummarized_line()
            if late:
                status.append(late)  # sessions exist but have no summary yet
    return render(agent, "\n".join(status), menu)


def render(agent: str, status: str, menu: str) -> dict:
    """``status`` (for the person) and the recall ``menu`` (for the agent; may be
    empty) in the shape ``agent`` reads.

    Both are escaped for hidden characters: the menu quotes stored summaries
    into the agent's context, and a row stored before writes cleaned text could
    still carry one. JSON encoding is no guard here; the agent decodes it."""
    from m3_core.hidden_text import escape_for_display

    status, menu = escape_for_display(status), escape_for_display(menu)
    if agent == "antigravity":
        # https://antigravity.google/docs/hooks/ : SessionStart output is injectSteps.
        steps = [{"systemMessage": {"systemMessage": status}}]
        if menu:
            steps.append({"systemMessage": {"systemMessage": menu}})
        return {"injectSteps": steps}
    out: dict = {"systemMessage": status}
    if menu:
        out["hookSpecificOutput"] = {"hookEventName": "SessionStart", "additionalContext": menu}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="m3 session-start hook (one line in each agent's config).")
    ap.add_argument("--agent", required=True, choices=AGENTS)
    args = ap.parse_args(argv)
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        envelope = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        envelope = {}
    if not isinstance(envelope, dict):
        envelope = {}
    print(json.dumps(build(args.agent, envelope)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
