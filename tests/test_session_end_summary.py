"""A session's summary starts when the session ends, not on every turn.

bin/hooks/session_end.py (Claude Code, Codex: SessionEnd budgets of 1.5 s and 3 s)
starts the session's last capture detached, with variant ``session_end``; a
session-end capture starts the summary (session_recall.ended_variant gates it).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
for p in (str(_BIN), str(_BIN / "hooks")):
    if p not in sys.path:
        sys.path.insert(0, p)

import session_end  # noqa: E402
import session_recall  # noqa: E402


@pytest.fixture
def spawned(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    class _Popen:
        def __init__(self, argv, **kwargs):
            calls.append(list(argv))

    import m3_core.paths

    monkeypatch.setattr(session_end.subprocess, "Popen", _Popen)
    monkeypatch.setattr(m3_core.paths, "get_m3_logs_root", lambda: str(tmp_path / "logs"))
    return calls


def test_session_end_starts_the_final_capture_marked_session_end(spawned, tmp_path):
    why_not = session_end.start_final_capture(
        "claude", {"session_id": "s-1", "transcript_path": "/t/s-1.jsonl", "cwd": "/repo"}
    )
    assert why_not is None
    (argv,) = spawned
    assert argv[argv.index("--format") + 1] == "claude-code"
    assert argv[argv.index("--variant") + 1] == "session_end"
    assert argv[argv.index("--session-id") + 1] == "s-1"
    assert argv[argv.index("--cwd") + 1] == "/repo"
    assert os.path.isfile(tmp_path / "logs" / "session_recall.log")


def test_session_end_without_a_transcript_says_so_and_never_fails_the_exit(spawned, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO('{"session_id": "s-2"}'))
    assert session_end.main(["--agent", "codex"]) == 0
    assert spawned == []
    err = capsys.readouterr().err
    assert "did not start" in err and "inspect" in err


@pytest.mark.parametrize(
    ("variant", "ended", "ready"),
    [
        ("session_end", True, True),
        ("session_end_exit", True, True),
        ("stop", False, False),
        ("after_model", False, False),
        ("pre_compact", False, True),  # a compaction readies the session but is not its end
        (None, False, False),
    ],
)
def test_only_a_session_end_starts_the_summary(variant, ended, ready):
    assert session_recall.ended_variant(variant) is ended
    assert session_recall.ready_variant(variant) is ready


def test_background_summary_is_not_started_when_recall_is_off(monkeypatch):
    monkeypatch.setattr(session_recall, "recall_settings", lambda: {"enabled": False})
    assert session_recall.start_background_summary("c-1") == "recall.enabled is false"
    assert session_recall.start_background_summary("") == "no conversation id"


def _backlog(*ready_minutes_ago, now):
    from datetime import timedelta

    pending = [
        session_recall.Pending(session_recall.Session(f"c{i}", now), "quiet", None, now - timedelta(minutes=m))
        for i, m in enumerate(ready_minutes_ago)
    ]
    return session_recall.Backlog(pending=pending, sessions=len(pending), covered=0, waiting=0)


def test_unsummarized_line_speaks_only_when_a_ready_session_is_late(monkeypatch):
    from datetime import UTC, datetime

    now = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)
    monkeypatch.setattr(session_recall, "recall_settings", lambda: {"enabled": True})

    monkeypatch.setattr(session_recall, "find_backlog", lambda now, settings: _backlog(now=now))
    assert session_recall.unsummarized_line(now) == ""  # nothing waiting

    in_flight = session_recall.BEHIND_MINUTES - 1
    monkeypatch.setattr(session_recall, "find_backlog", lambda now, settings: _backlog(in_flight, now=now))
    assert session_recall.unsummarized_line(now) == ""  # a summary on its way is not a problem

    late = session_recall.BEHIND_MINUTES + 120
    monkeypatch.setattr(session_recall, "find_backlog", lambda now, settings: _backlog(late, in_flight, now=now))
    line = session_recall.unsummarized_line(now)
    assert line.startswith("m3 recall: 1 recent session not in the menu") and "inspect: m3 doctor" in line

    monkeypatch.setattr(session_recall, "recall_settings", lambda: {"enabled": False})
    assert session_recall.unsummarized_line(now) == ""  # recall off: quiet


def test_recent_background_failures_counts_only_recent_errors(monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta

    import m3_core.paths

    now = datetime(2026, 10, 18, 21, 0, tzinfo=UTC)
    monkeypatch.setattr(m3_core.paths, "get_m3_logs_root", lambda: str(tmp_path))
    assert session_recall.recent_background_failures(24, now=now)[0] == 0  # no log yet: quiet

    recent_local = (now - timedelta(hours=2)).astimezone().strftime("%Y-%m-%d %H:%M:%S") + ",123"
    lines = [
        (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        + " ERROR session_recall: observed: background summary failed",
        f"{recent_local} - chatlog_ingest - ERROR - could not write a turn",
        (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        + " INFO session_recall: background summary: written:model",
        (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ") + " ERROR session_recall: an old failure",
        '{"written": 2}',
    ]
    (tmp_path / session_recall.BACKGROUND_LOG).write_text("\n".join(lines) + "\n", encoding="utf-8")
    count, newest, path = session_recall.recent_background_failures(24, now=now)
    assert count == 2, (count, newest)
    assert "could not write a turn" in newest and path.endswith(session_recall.BACKGROUND_LOG)
