"""Tests for the SessionStart chatlog-capture-check hook's DB resolution.

The hook must check the SAME agent-memory DB the running MCP server writes to,
resolved via the canonical m3_sdk path logic (honoring M3_ENGINE_ROOT /
M3_MEMORY_ROOT). A prior version resolved the DB from M3_HOME/engine, which
diverges from the server's M3_ENGINE_ROOT on Homecoming-migrated installs — the
"split-brain" that produced a false "chatlog NOT writing" alarm against a stale
pre-migration copy (documented in CLAUDE.md).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

_HOOK = Path(__file__).resolve().parents[1] / "bin" / "hooks" / "chatlog" / "session_start_capture_check.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location("sscc_under_test", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resolve_db_uses_canonical_engine_root(monkeypatch, tmp_path):
    """With M3_ENGINE_ROOT set, the hook resolves to <engine_root>/agent_memory.db
    — the same path the server uses — not the repo-relative engine/ copy."""
    engine = tmp_path / "engine"
    engine.mkdir()
    monkeypatch.setenv("M3_ENGINE_ROOT", str(engine))
    monkeypatch.delenv("M3_DATABASE", raising=False)
    monkeypatch.delenv("M3_DB_PATH", raising=False)
    monkeypatch.delenv("M3_MEMORY_ROOT", raising=False)

    mod = _load_hook()
    db = mod._resolve_db()

    assert os.path.normcase(db) == os.path.normcase(str(engine / "agent_memory.db")), db
    # Must NOT fall back to the repo-relative stale copy.
    assert "hooks" not in db


def test_resolve_db_never_returns_repo_stale_when_engine_root_set(monkeypatch, tmp_path):
    """The repo-relative fallback must lose to the canonical engine root."""
    engine = tmp_path / "eng2"
    engine.mkdir()
    monkeypatch.setenv("M3_ENGINE_ROOT", str(engine))
    monkeypatch.delenv("M3_DATABASE", raising=False)

    mod = _load_hook()
    db = Path(mod._resolve_db()).resolve()

    repo_relative = (_HOOK.parents[3] / "engine" / "agent_memory.db").resolve()
    assert db != repo_relative, "resolved the stale repo copy instead of engine root"


def test_resolve_db_honors_m3_database_override(monkeypatch, tmp_path):
    """An explicit M3_DATABASE wins (canonical resolver precedence)."""
    explicit = tmp_path / "explicit.db"
    monkeypatch.setenv("M3_DATABASE", str(explicit))

    mod = _load_hook()
    db = Path(mod._resolve_db()).resolve()

    assert db == explicit.resolve(), db


# ── The verdict comes from the writer's state file, not a database file ──────
def _state(monkeypatch, tmp_path, last):
    import json

    import chatlog_config

    f = tmp_path / ".chatlog_state.json"
    if last is not None:
        f.write_text(json.dumps({"last_write_at": last}), encoding="utf-8")
    else:
        f.unlink(missing_ok=True)  # nothing recorded yet
    monkeypatch.setattr(chatlog_config, "STATE_FILE", str(f))
    return f


def _iso(minutes_ago):
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


def test_a_recent_write_reads_as_working_and_an_old_one_as_not(monkeypatch, tmp_path):
    mod = _load_hook()
    _state(monkeypatch, tmp_path, _iso(3))
    assert "WORKING" in mod._state_verdict()
    # A between-sessions gap is idle, not down: same lull predicate as status.
    _state(monkeypatch, tmp_path, _iso(90))
    said = mod._state_verdict()
    assert "idle" in said and "90 min ago" in said and "NOT writing" not in said, said
    import chatlog_status

    beyond = chatlog_status._LULL_GRACE_MIN + 30
    _state(monkeypatch, tmp_path, _iso(beyond))
    said = mod._state_verdict()
    assert "NOT writing" in said and f"{beyond} min ago" in said, said


def test_hook_and_status_share_one_lull_predicate(monkeypatch):
    """The hook must not carry its own grace constant: patching the owner moves both."""
    import chatlog_status

    mod = _load_hook()
    monkeypatch.setattr(chatlog_status, "_LULL_GRACE_MIN", 60)
    assert "NOT writing" in mod._verdict_for_age(90, _iso(90))
    monkeypatch.setattr(chatlog_status, "_LULL_GRACE_MIN", 120)
    assert "idle" in mod._verdict_for_age(90, _iso(90))


def test_file_count_path_uses_the_newest_row_age(monkeypatch, tmp_path, capsys):
    """SQLite with no state file: 0 rows in 15 min but a row 90 min old is idle."""
    import sqlite3

    mod = _load_hook()
    _state(monkeypatch, tmp_path, None)
    db = tmp_path / "chat.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE memory_items (type TEXT, created_at TEXT)")
    conn.execute(
        "INSERT INTO memory_items VALUES ('chat_log', ?)",
        (_iso(90).replace("+00:00", "Z"),),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(mod, "_candidate_dbs", lambda: [str(db)])
    mod.main()
    out = capsys.readouterr().out
    assert "idle" in out and "NOT writing" not in out, out


def test_on_postgres_no_sqlite_file_is_opened_or_created(monkeypatch, tmp_path, capsys):
    """The false alarm this fixes: on a PG primary the hook opened the SQLite file
    paths (creating empty files) and cried "status UNKNOWN" every session."""
    from conftest import select_backend

    select_backend("postgres")
    engine = tmp_path / "engine"  # the sandbox's engine root (conftest m3_sandbox)
    engine.mkdir(exist_ok=True)
    _state(monkeypatch, tmp_path, _iso(2))
    mod = _load_hook()
    mod.main()
    assert "WORKING" in capsys.readouterr().out
    assert not list(engine.glob("*.db")), "the hook created a SQLite file on PostgreSQL"
    _state(monkeypatch, tmp_path, None)
    said = mod._state_verdict()
    assert "UNKNOWN" in said and "no write yet" in said, said


def test_on_sqlite_with_no_state_yet_the_file_count_still_decides(monkeypatch, tmp_path):
    mod = _load_hook()
    _state(monkeypatch, tmp_path, None)
    assert mod._state_verdict() is None
