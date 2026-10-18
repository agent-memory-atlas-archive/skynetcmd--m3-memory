#!/usr/bin/env python3
"""SessionEnd hook for agents that give it seconds: start the session's last
capture in the background and return at once.

    <m3 python> <payload>/bin/hooks/session_end.py --agent claude|codex

Claude Code gives SessionEnd hooks a shared 1.5 s budget and Codex caps them at
3 s; an ingest takes longer. This hook starts chatlog_ingest detached with
variant ``session_end``: it stores any turns Stop has not, and a session-end
capture starts the session's summary (session_recall.start_background_summary).
The children's output goes to session_recall.background_log().

Input (stdin JSON): ``session_id``, ``transcript_path``, ``cwd``, ``reason``.
Never blocks the agent's exit: a problem is printed to stderr (Claude Code shows
it) and the hook exits 0.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parents[1]
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

# The hook's --agent value -> chatlog_ingest's --format for that agent.
FORMATS = {"claude": "claude-code", "codex": "codex"}


def start_final_capture(agent: str, envelope: dict) -> str | None:
    """Start the detached capture; None when started, else why not."""
    import session_recall
    from m3_core.seams import detached_spawn_kwargs

    transcript = str(envelope.get("transcript_path") or "")
    session_id = str(envelope.get("session_id") or "")
    if not transcript or not session_id:
        return f"{agent} sent no transcript_path or session_id; the session's final capture and summary did not start"
    from m3_memory.hook_launchers import ISOLATE_FLAGS

    argv = [
        sys.executable,
        *ISOLATE_FLAGS,
        str(_BIN / "chatlog_ingest.py"),
        "--format",
        FORMATS[agent],
        "--transcript-path",
        transcript,
        "--session-id",
        session_id,
        "--variant",
        "session_end",
    ]
    cwd = str(envelope.get("cwd") or "")
    if cwd:
        argv += ["--cwd", cwd]
    with session_recall.background_log() as out:
        subprocess.Popen(  # noqa: S603 -- this interpreter and m3's own ingest, fixed argv
            argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out, **detached_spawn_kwargs()
        )
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="m3 session-end hook (one line in each agent's config).")
    ap.add_argument("--agent", required=True, choices=sorted(FORMATS))
    args = ap.parse_args(argv)
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        envelope = json.loads(raw) if raw.strip() else {}
        why_not = start_final_capture(args.agent, envelope if isinstance(envelope, dict) else {})
    except Exception as e:  # noqa: BLE001 -- an exit hook must not fail the agent's exit
        why_not = f"could not start the final capture ({type(e).__name__}: {e})"
    if why_not:
        print(
            f"m3: {why_not}. The session stays out of the recall menu until a summary pass reaches it "
            "(the cognitive loop's, when it runs). inspect: m3 doctor",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
