"""A store that is briefly unavailable does not park chat turns for good.

Spill files that failed max_drain_attempts drains in a row were quarantined,
whatever the cause. A PostgreSQL store behind a tailnet that was down for half
an hour, or a SQLite file held by another process, used up the budget and
parked healthy turns until someone moved files back by hand. Now:

- ``Dialect.is_transient_error`` (the database seam) says which failures are
  "retry as is"; those are retried every pass and not counted.
- Quarantined files get one more attempt a day.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest
from memory.backends.dialect import dialect_for
from test_chatlog_spill_lifecycle import _count, _drain, _item, _store

# ── the seam ─────────────────────────────────────────────────────────────────


def _real_busy_error(tmp_path) -> sqlite3.OperationalError:
    """A genuine SQLITE_BUSY: one connection holds an exclusive lock."""
    db = tmp_path / "busy.db"
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("CREATE TABLE t (x)")
    holder.execute("BEGIN EXCLUSIVE")
    other = sqlite3.connect(db, timeout=0)
    try:
        other.execute("INSERT INTO t VALUES (1)")
    except sqlite3.OperationalError as e:
        return e
    finally:
        other.close()
        holder.execute("ROLLBACK")
        holder.close()
    raise AssertionError("expected the second writer to be refused")


def test_sqlite_busy_is_transient_and_a_missing_table_is_not(tmp_path):
    d = dialect_for("sqlite")
    busy = _real_busy_error(tmp_path)
    assert d.is_transient_error(busy), f"code {busy.sqlite_errorcode}: {busy}"

    con = sqlite3.connect(":memory:")
    with pytest.raises(sqlite3.OperationalError) as missing:
        con.execute("SELECT * FROM nope")
    assert not d.is_transient_error(missing.value)
    # The message alone decides nothing: only the code says the file was held.
    assert not d.is_transient_error(sqlite3.OperationalError("database is locked"))


def test_a_wrapped_cause_is_read():
    d = dialect_for("sqlite")
    try:
        try:
            raise TimeoutError("connect timed out")
        except TimeoutError as inner:
            raise RuntimeError("insert failed") from inner
    except RuntimeError as outer:
        assert d.is_transient_error(outer)
    assert not d.is_transient_error(ValueError("bad row"))


def _pg_error(name: str, sqlstate: str | None, module: str = "psycopg"):
    cls = type(name, (Exception,), {"__module__": module})
    e = cls("simulated")
    e.sqlstate = sqlstate
    return e


@pytest.mark.parametrize(
    "err,transient",
    [
        (_pg_error("OperationalError", "08006"), True),  # connection failure
        (_pg_error("AdminShutdown", "57P01"), True),
        (_pg_error("TooManyConnections", "53300"), True),
        (_pg_error("SerializationFailure", "40001"), True),
        (_pg_error("DeadlockDetected", "40P01"), True),
        (_pg_error("OperationalError", None), True),  # never reached the server
        (_pg_error("PoolTimeout", None, module="psycopg_pool"), True),
        (_pg_error("UndefinedTable", "42P01"), False),
        (_pg_error("UniqueViolation", "23505"), False),
        (_pg_error("InvalidTextRepresentation", "22P02"), False),
        (_pg_error("OperationalError", None, module="somethingelse"), False),
    ],
)
def test_postgres_reads_sqlstate(err, transient):
    """Without psycopg installed: the SQLSTATE is all the dialect needs."""
    assert dialect_for("postgres").is_transient_error(err) is transient


# ── the drain ────────────────────────────────────────────────────────────────


def _refuse_with(monkeypatch, exc_factory):
    import chatlog_core

    def _refuse(rows, **kw):
        raise exc_factory()

    monkeypatch.setattr(chatlog_core, "_executemany_insert", _refuse)


def test_a_temporary_outage_longer_than_the_budget_parks_nothing(tmp_path, monkeypatch, caplog):
    import chatlog_core
    import chatlog_embed_sweeper

    db, spill_dir = _store(tmp_path, monkeypatch)
    it = _item("outage", str(db))
    chatlog_core._spill_batch([it])
    monkeypatch.setattr(chatlog_embed_sweeper, "_spill_max_attempts", lambda now=None: 2)
    real_insert = chatlog_core._executemany_insert
    _refuse_with(monkeypatch, lambda: TimeoutError("store unreachable (simulated)"))

    for _ in range(5):  # well past the budget of 2
        assert _drain() == 0
    pending = list(spill_dir.glob("*.jsonl"))
    assert len(pending) == 1, "a temporary failure must not quarantine the file"
    assert not list((spill_dir / "quarantine").glob("*.reason.json"))
    sidecar = json.loads((spill_dir / (pending[0].name + ".attempts")).read_text(encoding="utf-8"))
    assert sidecar["consecutive_failures"] == 0 and sidecar.get("transient_since"), sidecar
    assert "temporarily unavailable" in caplog.text, "a temporary failure is still reported"

    monkeypatch.setattr(chatlog_core, "_executemany_insert", real_insert)  # the store is back
    assert _drain() == 1
    assert _count(db, it["_id"]) == 1
    assert not list(spill_dir.glob("*.jsonl"))


def test_a_permanent_failure_still_quarantines(tmp_path, monkeypatch):
    import chatlog_core
    import chatlog_embed_sweeper

    db, spill_dir = _store(tmp_path, monkeypatch)
    chatlog_core._spill_batch([_item("permanent", str(db))])
    monkeypatch.setattr(chatlog_embed_sweeper, "_spill_max_attempts", lambda now=None: 2)
    _refuse_with(monkeypatch, lambda: sqlite3.OperationalError("no such table: memory_items"))
    _drain()
    _drain()
    assert not list(spill_dir.glob("*.jsonl"))
    assert len(list((spill_dir / "quarantine").glob("*.reason.json"))) == 1


def test_quarantined_files_get_one_retry_a_day(tmp_path, monkeypatch):
    import chatlog_core
    import chatlog_embed_sweeper

    db, spill_dir = _store(tmp_path, monkeypatch)
    it = _item("parked", str(db))
    chatlog_core._spill_batch([it])
    monkeypatch.setattr(chatlog_embed_sweeper, "_spill_max_attempts", lambda now=None: 2)
    real_insert = chatlog_core._executemany_insert
    _refuse_with(monkeypatch, lambda: sqlite3.OperationalError("no such table: memory_items"))
    _drain()
    _drain()
    qdir = spill_dir / "quarantine"
    assert len(list(qdir.glob("*.reason.json"))) == 1

    # Still failing: the daily retry spends one attempt and re-quarantines.
    assert _drain() == 0
    assert not list(spill_dir.glob("*.jsonl")) and len(list(qdir.glob("*.reason.json"))) == 1
    # Within the day: no further retry, even once the cause is fixed.
    monkeypatch.setattr(chatlog_core, "_executemany_insert", real_insert)
    assert _drain() == 0 and _count(db, it["_id"]) == 0
    # A day later the retry lands it and quarantine is empty.
    stamp = qdir / chatlog_embed_sweeper._RETRY_STAMP
    old = time.time() - chatlog_embed_sweeper.QUARANTINE_RETRY_S - 60
    import os

    os.utime(stamp, (old, old))
    assert _drain() == 1
    assert _count(db, it["_id"]) == 1
    assert not list(qdir.glob("*.reason.json")) and not list(qdir.glob("*.jsonl"))


def test_malformed_and_foreign_parked_lines_are_not_retried(tmp_path, monkeypatch):
    import chatlog_embed_sweeper

    _db, spill_dir = _store(tmp_path, monkeypatch)
    qdir = spill_dir / "quarantine"
    qdir.mkdir(parents=True)
    (qdir / "20261016.jsonl.bad").write_text("not json\n", encoding="utf-8")
    (qdir / "foreign-20261016.jsonl").write_text("{}\n", encoding="utf-8")
    assert chatlog_embed_sweeper._retry_quarantined(str(spill_dir), 5) == 0
    assert (qdir / "20261016.jsonl.bad").exists() and (qdir / "foreign-20261016.jsonl").exists()
