"""Session-recall probe: is the loop writing session summaries, how far behind is
it, and do recent turns carry their project?

The start-of-session menu lists ``session_summary`` memories, which only the
cognitive loop's summarize_sessions pass writes. No loop, or no model for it,
means an empty menu that looks like "nothing happened recently"; this probe
says which. It reads the same backlog the pass works from
(``session_recall.find_backlog``), so "behind" means exactly the sessions the
pass would summarize next.

Report-only and exit-code neutral (like the cognitive-loop probe): whether to
run the loop is the user's choice. It warns only on a measured backlog (a ready
session waiting longer than session_recall.BEHIND_MINUTES), never on an empty store.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta


def _ago(delta: timedelta) -> str:
    mins = int(delta.total_seconds() // 60)
    if mins < 60:
        return f"{mins} min"
    if mins < 48 * 60:
        return f"{mins // 60} h"
    return f"{mins // 1440} d"


def _gather(now: datetime) -> dict:
    bin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    import session_recall

    s = session_recall.recall_settings()
    out: dict = {"settings": s}
    if not s["enabled"]:
        return out
    out["backlog"] = session_recall.find_backlog(now=now, settings=s)
    out["newest"] = session_recall.newest_summary_at()
    out["coverage"] = session_recall.project_field_coverage(now - timedelta(days=int(s["window_days"])))
    return out


def _project_note(coverage: dict) -> str:
    """'' when every agent's newest turn carries its project, else which do not."""
    missing = sorted(h for h, c in coverage.items() if not c["newest_has_project"])
    if not missing:
        return ""
    return (
        f"newest turns from {', '.join(missing)} carry no project (shown as (no project); turns captured "
        "before this version never do, and a client whose hook sends no cwd cannot)"
    )


def run(brief: bool = False, now: datetime | None = None) -> int:
    """Report session recall. Always returns 0 (report-only)."""
    now = now or datetime.now(UTC)
    try:
        g = _gather(now)
    except Exception as e:  # noqa: BLE001 -- a probe must never crash the doctor
        msg = f"could not check ({type(e).__name__}: {e}); the menu may be empty. inspect: m3 chatlog status"
        if brief:
            print(f"⚠️  session recall: {msg}")
        else:
            print()
            print("=== session recall ===")
            print(f"  status   : {msg}")
        return 0

    s = g["settings"]
    if not s["enabled"]:
        line = "off (recall.enabled = false); turn on: m3 config set recall.enabled true"
        if brief:
            print(f"✅ session recall: {line}")
        else:
            print()
            print("=== session recall ===")
            print(f"  status   : {line}")
        return 0

    backlog, newest, coverage = g["backlog"], g["newest"], g["coverage"]
    oldest = backlog.oldest_ready_at
    behind: timedelta = (now - oldest) if oldest is not None else timedelta(0)
    import session_recall  # importable: _gather put bin/ on the path

    lagging = behind > timedelta(minutes=session_recall.BEHIND_MINUTES)
    newest_txt = f"newest summary {_ago(now - newest)} ago" if newest else "no summary written yet"
    note = _project_note(coverage)
    window = f"last {int(s['window_days'])} d"

    if lagging:
        verdict = (
            f"{len(backlog.pending)} session(s) wait for a summary, the oldest ready for {_ago(behind)} "
            f"({newest_txt}). The summary pass is not running or not keeping up: the start-of-session menu "
            "misses them. inspect: the cognitive loop line above, the loop log (cognitive_loop.log under "
            "the logs root), and whether a local model is loaded (recall.summary_profile = "
            f"{s['summary_profile']})"
        )
    elif backlog.pending:
        verdict = f"{len(backlog.pending)} session(s) queued for a summary, oldest ready {_ago(behind)} ({newest_txt})"
    elif backlog.sessions:
        verdict = (
            f"summaries current: {backlog.covered} of {backlog.sessions} session(s) in the {window} ({newest_txt})"
        )
    else:
        verdict = f"no chat sessions in the {window}, so nothing to summarize ({newest_txt})"

    if brief:
        glyph = "⚠️ " if lagging else "✅"
        print(f"{glyph} session recall: {verdict}" + (f" · {note}" if note else ""))
        return 0

    print()
    print("=== session recall ===")
    print(f"  sessions : {backlog.sessions} in the {window}; {backlog.covered} summarized, ")
    print(f"             {len(backlog.pending)} ready for a summary, {backlog.waiting} still active")
    print(f"  pass     : {newest_txt}; model setting recall.summary_profile = {s['summary_profile']}")
    if coverage:
        for host, c in sorted(coverage.items()):
            state = "has" if c["newest_has_project"] else "has NO"
            print(f"  project  : {host}: newest turn {state} project fields ({c['with_project']}/{c['turns']} recent)")
    else:
        print(f"  project  : no turns captured in the {window}")
    print(f"  status   : {'WARN' if lagging else 'OK'}: {verdict}")
    if note:
        print(f"  note     : {note}")
    return 0
