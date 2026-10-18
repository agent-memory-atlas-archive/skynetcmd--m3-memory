"""Every m3 timestamp is UTC and says so.

A bare date was stored as typed ('2026-04-18'), and a value without a zone was
stored without one; both were read as UTC, but a stored value that does not say
its zone is a standing ambiguity. ``m3_core.runtime.utc_stored`` is the owner of
the rule; the write paths and chat capture route caller-supplied times through
it, and migration 061 converts rows written before.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from m3_core.runtime import utc_stored

MIGRATION = Path(__file__).resolve().parent.parent / "memory" / "migrations" / "061_timestamps_utc_zoned.up.sql"

# ── the owner ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "given,stored",
    [
        ("2026-04-18", "2026-04-18T00:00:00+00:00"),
        ("2026-04-18T09:30:00", "2026-04-18T09:30:00+00:00"),
        ("2026-04-18 09:30:00.250000", "2026-04-18T09:30:00.250000+00:00"),
        ("2026-04-18T09:30:00Z", "2026-04-18T09:30:00+00:00"),
        ("2026-04-18T09:30:00-05:00", "2026-04-18T14:30:00+00:00"),
        ("2026-04-18T09:30:00+00:00", "2026-04-18T09:30:00+00:00"),
    ],
)
def test_utc_stored_is_zoned_utc(given, stored):
    assert utc_stored(given) == stored


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_is_open_ended(blank):
    assert utc_stored(blank) is None


@pytest.mark.parametrize("bad", ["yesterday", "2026/04/18", "18-04-2026"])
def test_unparseable_raises(bad):
    with pytest.raises(ValueError, match="ISO-8601"):
        utc_stored(bad)


# ── the write paths, on every backend ────────────────────────────────────────


def _zoned(value) -> bool:
    """True when a stored value carries a UTC zone (PG hands back datetimes)."""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt.tzinfo is not None and dt.utcoffset().total_seconds() == 0


def _row(mem_id: str) -> dict:
    from memory.backends import dialect
    from memory.db import _db

    p = dialect().param()
    with _db() as db:
        r = db.execute(
            f"SELECT created_at, updated_at, valid_from, valid_to, expires_at FROM memory_items WHERE id = {p}",
            (mem_id,),
        ).fetchone()
    return dict(r)


def _assert_all_zoned(row: dict) -> None:
    """The guard: no timestamp column of a written row lacks its zone."""
    for col, value in row.items():
        if value not in (None, ""):
            assert _zoned(value), f"{col}={value!r} is not zoned UTC"


def _new_id(res: str) -> str:
    return res.split("Created:", 1)[1].strip().split()[0]


def test_single_write_stores_a_bare_date_as_midnight_utc(each_backend):
    from memory.write import memory_write_impl

    res = asyncio.run(
        memory_write_impl(
            "note", f"utcprobe {uuid.uuid4().hex}", embed=False, valid_from="2026-04-18", valid_to="2026-04-19T10:00:00"
        )
    )
    row = _row(_new_id(res))
    _assert_all_zoned(row)
    vf = row["valid_from"]
    vf = vf if isinstance(vf, datetime) else datetime.fromisoformat(vf)
    assert vf.isoformat() == "2026-04-18T00:00:00+00:00"


def test_single_write_refuses_an_unparseable_time(each_backend):
    from memory.write import memory_write_impl

    res = asyncio.run(memory_write_impl("note", f"utcprobe {uuid.uuid4().hex}", embed=False, valid_from="last week"))
    assert res.startswith("Error:") and "ISO-8601" in res


def test_bulk_write_stores_zoned_and_survives_a_bad_time(each_backend, caplog):
    from memory.write import memory_write_bulk_impl

    tag = uuid.uuid4().hex
    out = asyncio.run(
        memory_write_bulk_impl(
            [
                {"type": "note", "content": f"utcbulk {tag} a", "valid_from": "2026-04-18", "embed": False},
                {"type": "note", "content": f"utcbulk {tag} b", "valid_from": "not a time", "embed": False},
            ]
        )
    )
    ids = [o if isinstance(o, str) else o["id"] for o in out]
    assert len(ids) == 2, out
    for mid in ids:
        _assert_all_zoned(_row(mid))
    assert "not ISO-8601" in caplog.text


def test_supersede_closes_the_old_memory_at_a_zoned_time(each_backend):
    from memory.write import memory_supersede_impl, memory_write_impl

    old = _new_id(asyncio.run(memory_write_impl("fact", f"utcsup {uuid.uuid4().hex}", embed=False)))
    res = asyncio.run(memory_supersede_impl(old, "replacement body", embed=False, valid_from="2026-05-01"))
    assert "Created:" in res, res
    row = _row(old)
    _assert_all_zoned(row)
    vt = row["valid_to"]
    vt = vt if isinstance(vt, datetime) else datetime.fromisoformat(vt)
    assert vt.isoformat() == "2026-05-01T00:00:00+00:00"


def test_supersede_refuses_an_unparseable_time(each_backend):
    from memory.write import memory_supersede_impl, memory_write_impl

    old = _new_id(asyncio.run(memory_write_impl("fact", f"utcsup {uuid.uuid4().hex}", embed=False)))
    res = asyncio.run(memory_supersede_impl(old, "replacement body", embed=False, valid_from="soon"))
    assert res.startswith("Error:") and "ISO-8601" in res
    assert _row(old)["valid_to"] in (None, "")  # nothing was closed


# ── chat capture ─────────────────────────────────────────────────────────────


def test_turn_time_is_zoned_and_never_fails_capture(caplog):
    import chatlog_core

    assert chatlog_core._turn_time("2026-04-18T09:30:00", "x") == "2026-04-18T09:30:00+00:00"
    assert chatlog_core._turn_time(None, "fallback") == "fallback"
    assert chatlog_core._turn_time("garbage", "fallback") == "fallback"
    assert "not ISO-8601" in caplog.text


# ── migration 061 ────────────────────────────────────────────────────────────

_ROWS = {
    # id: (valid_from, valid_to, created_at)  ->  expected after
    "bare": (
        ("2026-04-18", "2026-04-19", "2026-04-18T01:02:03+00:00"),
        ("2026-04-18T00:00:00+00:00", "2026-04-19T00:00:00+00:00", "2026-04-18T01:02:03+00:00"),
    ),
    "naive": (
        ("2026-04-18T09:30:00", None, "2026-04-18 09:30:00.123456"),
        ("2026-04-18T09:30:00+00:00", None, "2026-04-18T09:30:00.123456+00:00"),
    ),
    "zoned": (
        ("2026-04-18T09:30:00Z", "2026-04-18T09:30:00-05:00", "2026-04-18T09:30:00.5+00:00"),
        ("2026-04-18T09:30:00Z", "2026-04-18T09:30:00-05:00", "2026-04-18T09:30:00.5+00:00"),
    ),
}


def _migrated(runs: int) -> dict:
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE memory_items (id TEXT, valid_from TEXT, valid_to TEXT, expires_at TEXT, "
        "created_at TEXT, updated_at TEXT)"
    )
    for mid, (before, _after) in _ROWS.items():
        con.execute("INSERT INTO memory_items (id, valid_from, valid_to, created_at) VALUES (?,?,?,?)", (mid, *before))
    for _ in range(runs):
        con.executescript(MIGRATION.read_text(encoding="utf-8"))
    out = {r[0]: r[1:] for r in con.execute("SELECT id, valid_from, valid_to, created_at FROM memory_items")}
    con.close()
    return out


@pytest.mark.parametrize("runs", [1, 2])
def test_migration_zones_old_rows_and_is_idempotent(runs):
    got = _migrated(runs)
    for mid, (_before, after) in _ROWS.items():
        assert got[mid] == after, mid
