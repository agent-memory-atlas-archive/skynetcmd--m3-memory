"""The SQLite/PG migration runners fail loud and leave stores as they found them.

- restore refuses while m3 writers hold the store, moves the original aside under
  a unique name, and moves it BACK when the restore fails;
- `down` names exactly one store (no default "all");
- status / plan / backup never create a store or its schema_versions table;
- `up` stops at the first version with no up file instead of applying over a gap;
- migrate_pg refuses a DSN naming the stored warehouse database and never blocks
  on input() without a console;
- memory.db: a failed migration child raises (the store is not marked ready), the
  child always gets an explicit --target, and a failed history insert on the
  caller's connection keeps the caller's own write.

Hermetic: temp SQLite files only; no real DSN, no real m3 process.
"""

from __future__ import annotations

import importlib
import logging
import os
import sqlite3
import subprocess
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import migrate_memory
import migrate_pg
import pytest


def _target(db, migrations_dir="."):
    return migrate_memory.MigrationTarget(name="main", db_path=str(db), migrations_dir=str(migrations_dir))


def _make_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    conn.commit()
    conn.close()


def _rows(path):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT v FROM t ORDER BY v")]
    finally:
        conn.close()


# ── restore ──────────────────────────────────────────────────────────────────


def test_restore_refuses_while_a_writer_holds_the_store(tmp_path, monkeypatch, caplog):
    live, backup = tmp_path / "agent_memory.db", tmp_path / "backup.db"
    _make_db(live, ["live"])
    _make_db(backup, ["old"])
    monkeypatch.setattr(migrate_memory, "_live_store_writers", lambda: ["mcp (pid 4242)"])
    with caplog.at_level(logging.ERROR, logger="migrate_memory"), pytest.raises(SystemExit) as exc:
        migrate_memory.restore_backup(str(backup), _target(live))
    assert exc.value.code == 1
    assert _rows(live) == ["live"], "the live store was touched although a writer held it"
    assert "mcp (pid 4242)" in caplog.text and "m3 stop" in caplog.text
    assert not list(tmp_path.glob("*.pre-restore*"))


def test_restore_refuses_when_the_writer_probe_fails(tmp_path, monkeypatch):
    live, backup = tmp_path / "agent_memory.db", tmp_path / "backup.db"
    _make_db(live, ["live"])
    _make_db(backup, ["old"])

    def _broken():
        raise OSError("psutil unavailable")

    monkeypatch.setattr(migrate_memory, "_live_store_writers", _broken)
    with pytest.raises(SystemExit):
        migrate_memory.restore_backup(str(backup), _target(live))
    assert _rows(live) == ["live"]


def test_a_failed_restore_moves_the_original_back(tmp_path, monkeypatch, caplog):
    live, backup = tmp_path / "agent_memory.db", tmp_path / "backup.db"
    _make_db(live, ["live"])
    backup.write_bytes(b"this is not a sqlite database" * 100)
    monkeypatch.setattr(migrate_memory, "_live_store_writers", list)
    with caplog.at_level(logging.ERROR, logger="migrate_memory"), pytest.raises(SystemExit):
        migrate_memory.restore_backup(str(backup), _target(live))
    assert _rows(live) == ["live"], "the original store was not put back after a failed restore"
    assert not list(tmp_path.glob("*.pre-restore*")), "the sidecar was left behind after moving back"
    assert "moved back" in caplog.text


def test_two_restores_keep_two_distinct_sidecars(tmp_path, monkeypatch, caplog):
    live, backup = tmp_path / "agent_memory.db", tmp_path / "backup.db"
    _make_db(live, ["live"])
    _make_db(backup, ["old"])
    monkeypatch.setattr(migrate_memory, "_live_store_writers", list)
    with caplog.at_level(logging.INFO, logger="migrate_memory"):
        migrate_memory.restore_backup(str(backup), _target(live))
        migrate_memory.restore_backup(str(backup), _target(live))
    assert _rows(live) == ["old"]
    sidecars = sorted(p.name for p in tmp_path.glob("agent_memory.db.pre-restore.*"))
    assert len(sidecars) == 2, sidecars
    assert "previous store kept at" in caplog.text


# ── down requires one target ─────────────────────────────────────────────────


def test_down_without_target_is_refused_by_the_parser():
    with pytest.raises(SystemExit):
        migrate_memory.build_parser().parse_args(["down", "--to", "3"])


def test_down_target_all_is_refused_by_the_parser():
    with pytest.raises(SystemExit):
        migrate_memory.build_parser().parse_args(["down", "--to", "3", "--target", "all"])


def test_down_with_one_target_parses():
    args = migrate_memory.build_parser().parse_args(["down", "--to", "3", "--target", "chatlog"])
    assert args.target == "chatlog"


# ── read-only commands leave nothing behind ──────────────────────────────────


def _migs_dir(tmp_path, versions, *, drop_up=()):
    d = tmp_path / "migs"
    d.mkdir()
    for v in versions:
        if v not in drop_up:
            (d / f"{v:03d}_m{v}.up.sql").write_text(f"CREATE TABLE t{v} (x INTEGER);\n", encoding="utf-8")
        (d / f"{v:03d}_m{v}.down.sql").write_text(f"DROP TABLE t{v};\n", encoding="utf-8")
    return d


@pytest.mark.parametrize("cmd", ["status", "plan", "backup"])
def test_read_only_commands_never_create_the_store(tmp_path, monkeypatch, cmd):
    db = tmp_path / "engine" / "agent_memory.db"
    migs = _migs_dir(tmp_path, [1, 2])
    monkeypatch.setattr(migrate_memory, "targets", lambda _sel: [_target(db, migs)])
    args = SimpleNamespace(target="all", to=None, out=str(tmp_path / "bk"), yes=True)
    getattr(migrate_memory, f"cmd_{cmd}")(args)
    assert not db.exists(), f"`{cmd}` created the store"
    assert not db.parent.exists(), f"`{cmd}` created the store's directory"


@pytest.mark.parametrize("cmd", ["status", "plan", "backup"])
def test_read_only_commands_never_add_schema_versions(tmp_path, monkeypatch, cmd):
    db = tmp_path / "agent_memory.db"
    _make_db(db, ["x"])
    migs = _migs_dir(tmp_path, [1])
    monkeypatch.setattr(migrate_memory, "targets", lambda _sel: [_target(db, migs)])
    args = SimpleNamespace(target="all", to=None, out=str(tmp_path / "bk"), yes=True)
    getattr(migrate_memory, f"cmd_{cmd}")(args)
    conn = sqlite3.connect(db)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "schema_versions" not in names, f"`{cmd}` wrote schema_versions into the store"


# ── up stops at a gap ────────────────────────────────────────────────────────


def test_up_stops_at_the_first_missing_up_file(tmp_path, monkeypatch, caplog):
    db = tmp_path / "agent_memory.db"
    migs = _migs_dir(tmp_path, [1, 2, 3], drop_up=(2,))
    monkeypatch.setattr(migrate_memory, "targets", lambda _sel: [_target(db, migs)])
    monkeypatch.setattr(migrate_memory, "prompt_backup_dir", lambda _y: str(tmp_path / "bk"))
    args = SimpleNamespace(target="main", to=None, yes=True, dry_run=False)
    with caplog.at_level(logging.ERROR, logger="migrate_memory"), pytest.raises(SystemExit) as exc:
        migrate_memory.cmd_up(args)
    assert exc.value.code == 1
    assert migrate_memory.read_applied_versions(str(db)) == {1}, "a version past the gap was applied"
    assert "NOT applied: [2, 3]" in caplog.text


def test_apply_pending_raises_at_a_gap(tmp_path):
    migs = _migs_dir(tmp_path, [1, 2, 3], drop_up=(2,))
    conn = sqlite3.connect(tmp_path / "d.db")
    try:
        with pytest.raises(FileNotFoundError, match="v002"):
            migrate_memory.apply_pending(conn, str(migs))
        assert set(migrate_memory.get_applied_versions(conn)) == {1}
    finally:
        conn.close()


# ── migrate_pg ───────────────────────────────────────────────────────────────


def test_migrate_pg_refuses_the_warehouse_database(monkeypatch):
    pb = importlib.import_module("memory.backends.postgres_backend")
    monkeypatch.setattr(pb, "_reject_forbidden_host", lambda _u: None)

    def _same(_u):
        raise RuntimeError("primary DSN names the warehouse database")

    monkeypatch.setattr(pb, "_reject_same_as_warehouse", _same)
    with pytest.raises(RuntimeError, match="warehouse"):
        migrate_pg._resolve_dsn("postgresql://u@wh.invalid:5432/m3")


def test_migrate_pg_confirm_declines_without_a_console(monkeypatch):
    monkeypatch.setattr(sys, "stdin", None)

    def _no_input(*_a):
        raise AssertionError("input() called with no console")

    monkeypatch.setattr("builtins.input", _no_input)
    assert migrate_pg._confirm("Apply?", False) is False
    assert migrate_pg._confirm("Apply?", True) is True


def test_migrate_pg_up_stops_at_a_gap(monkeypatch):
    applied: list[int] = []

    class _Conn:
        def close(self):
            pass

    monkeypatch.setattr(migrate_pg, "_resolve_dsn", lambda _d: "postgresql://x.invalid/db")
    monkeypatch.setattr(migrate_pg, "_connect", lambda _d: _Conn())
    monkeypatch.setattr(
        migrate_pg,
        "discover_migrations",
        lambda: {
            40: {"name": "a", "up": "a.up.sql", "down": None},
            41: {"name": "b", "up": None, "down": "b.down.sql"},
            42: {"name": "c", "up": "c.up.sql", "down": None},
        },
    )
    monkeypatch.setattr(migrate_pg, "get_applied_versions", lambda _c: [])
    monkeypatch.setattr(migrate_pg, "current_version", lambda _c: 39)
    monkeypatch.setattr(migrate_pg, "apply_migration", lambda _c, v, _p: applied.append(v))
    with pytest.raises(SystemExit, match="NOT applied: \\[41, 42\\]"):
        migrate_pg.cmd_up(SimpleNamespace(dsn=None, to=None, dry_run=False, yes=True))
    assert applied == [40]


# ── memory.db ────────────────────────────────────────────────────────────────


@pytest.fixture
def mdb(monkeypatch):
    db = importlib.import_module("memory.db")
    paths = importlib.import_module("m3_core.paths")
    monkeypatch.setattr(paths, "store_is_a_file", lambda: True)
    monkeypatch.setattr(db, "migration_lock", nullcontext)
    return db


def test_a_failed_migration_child_raises_with_store_target_and_rc(mdb, monkeypatch, tmp_path):
    calls = []

    def _fail(cmd, **_k):
        calls.append(cmd)
        raise subprocess.CalledProcessError(3, cmd, output="", stderr="boom")

    monkeypatch.setattr(mdb.subprocess, "run", _fail)
    store = str(tmp_path / "missing" / "agent_memory.db")
    with pytest.raises(RuntimeError) as exc:
        mdb._ensure_sync_tables(store)
    msg = str(exc.value)
    assert store in msg and "--target main" in msg and "exited 3" in msg
    assert calls and calls[0][-2:] == ["--target", "main"], "the child ran without an explicit --target"


def test_migration_child_output_is_captured_never_written_to_the_host(mdb, monkeypatch, tmp_path, capsys, caplog):
    """An in-process host (Hermes' memory provider) saw the migrator's ~60-line
    plan in its terminal UI: the child's output was relayed to stderr."""
    seen: list[dict] = []
    plan = "Current version: v000\nTarget version: v059\nWill apply:\n  + v001 baseline\n"

    def _run(cmd, **kw):
        seen.append(kw)
        return SimpleNamespace(stdout=plan, stderr="INFO applied v001\n", returncode=0)

    monkeypatch.setattr(mdb.subprocess, "run", _run)
    with caplog.at_level(logging.INFO, logger="memory.db"):
        mdb._ensure_sync_tables(str(tmp_path / "agent_memory.db"))
    assert seen and seen[0].get("capture_output") is True and seen[0].get("stdin") is subprocess.DEVNULL
    out = capsys.readouterr()
    assert out.out == "" and out.err == "", "the host's stdout/stderr must stay untouched"
    lines = [r.getMessage() for r in caplog.records if r.name == "memory.db"]
    assert any(m.startswith("migrated ") and " from v000 to v" in m for m in lines), lines
    assert not any("Will apply" in m for m in lines), "the plan must not be relayed through the logger either"


def test_failed_migration_carries_the_captured_tail(mdb, monkeypatch, tmp_path, capsys):
    out = "\n".join(f"line {i}" for i in range(50))

    def _fail(cmd, **_k):
        raise subprocess.CalledProcessError(1, cmd, output=out, stderr="sqlite3.OperationalError: disk I/O error")

    monkeypatch.setattr(mdb.subprocess, "run", _fail)
    with pytest.raises(RuntimeError) as exc:
        mdb._ensure_sync_tables(str(tmp_path / "agent_memory.db"))
    msg = str(exc.value)
    assert "disk I/O error" in msg and "line 49" in msg and "line 10" not in msg
    assert capsys.readouterr().err == ""


def test_lazy_init_does_not_mark_a_store_whose_migration_failed(mdb, monkeypatch, tmp_path):
    def _fail(_k):
        raise RuntimeError("schema migration FAILED")

    monkeypatch.setattr(mdb, "_ensure_sync_tables", _fail)
    key = str(tmp_path / "agent_memory.db")
    with pytest.raises(RuntimeError):
        mdb._lazy_init(key)
    assert key not in mdb._initialized_dbs


def test_classifier_failure_is_logged_and_target_stays_explicit(mdb, monkeypatch, tmp_path, caplog):
    def _boom(_p):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(migrate_memory, "_classify_db", _boom)
    seen = []
    monkeypatch.setattr(
        mdb.subprocess, "run", lambda cmd, **_k: seen.append(cmd) or SimpleNamespace(stdout="", stderr="")
    )
    with caplog.at_level(logging.WARNING, logger="memory.db"):
        mdb._ensure_sync_tables(str(tmp_path / "agent_memory.db"))
    assert seen and seen[0][-2:] == ["--target", "main"]
    assert "could not classify" in caplog.text


def test_a_failed_history_insert_keeps_the_callers_write(mdb, tmp_path, caplog):
    conn = sqlite3.connect(tmp_path / "h.db")
    conn.execute("CREATE TABLE notes (v TEXT)")
    conn.commit()
    conn.execute("INSERT INTO notes VALUES ('kept')")  # caller's write, same transaction
    with caplog.at_level(logging.WARNING, logger="memory.db"):
        mdb._record_history("mem-1", "update", db=conn)  # no memory_history table
    conn.commit()
    assert [r[0] for r in conn.execute("SELECT v FROM notes")] == ["kept"]
    conn.close()
    assert "memory_history row NOT recorded for memory mem-1" in caplog.text
    assert os.path.exists(tmp_path / "h.db")


def test_two_restores_in_one_clock_tick_keep_two_sidecars(tmp_path, monkeypatch):
    """Windows' clock can read the same microseconds twice in a row; the second
    restore then found its sidecar name taken and refused (CI, 2026-10-17)."""
    from datetime import UTC, datetime

    live, backup = tmp_path / "agent_memory.db", tmp_path / "backup.db"
    _make_db(live, ["live"])
    _make_db(backup, ["old"])
    monkeypatch.setattr(migrate_memory, "_live_store_writers", list)
    frozen = datetime(2026, 10, 17, 12, 0, 0, 123456, tzinfo=UTC)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(migrate_memory, "datetime", _Clock)
    migrate_memory.restore_backup(str(backup), _target(live))
    migrate_memory.restore_backup(str(backup), _target(live))
    sidecars = sorted(p.name for p in tmp_path.glob("agent_memory.db.pre-restore.*"))
    assert len(sidecars) == 2 and sidecars[1] == sidecars[0] + ".1", sidecars
    assert _rows(live) == ["old"]
