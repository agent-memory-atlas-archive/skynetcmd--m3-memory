#!/usr/bin/env python3
"""Every agent's SessionStart hook: chat-capture status and the m3 version.

One line in each agent's own config runs it, so the config holds no logic:

    <m3 python> <payload>/bin/hooks/session_start.py --agent claude|codex|gemini

Claude Code, Codex and Gemini CLI read the same output, one JSON object whose
``systemMessage`` is shown to the person:

- chat capture (bin/hooks/chatlog/session_start_capture_check.py);
- the running version against the newest (m3_core.version_check reads a record
  refreshed at most daily; this hook never waits on the network).

This is also where the approved session-recall menu (m3 ``5a448be5``, plan
2026.10.12-SESSION_RECALL_DESIGN.md part C) is delivered, as
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

AGENTS = ("claude", "codex", "gemini")


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


def build(agent: str, envelope: dict) -> dict:
    """The hook's output for ``agent`` given the host's stdin ``envelope``."""
    return {"systemMessage": "\n".join((_capture_line(), _version_line()))}


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
