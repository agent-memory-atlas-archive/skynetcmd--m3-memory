"""chatlog_promote across two SQLite FILES — the path that had no tests.

`chatlog_promote_impl` has three branches: PostgreSQL (two tables, one database),
SQLite-unified (chatlog path == main path, a plain UPDATE), and SQLite cross-file
(ATTACH the main DB onto a chatlog connection and copy). Only the first two were
covered — `test_chatlog_pg_live.py` for PG, and nothing at all for cross-file,
which is how that branch accumulated:

  * `SELECT * FROM memory_items` — 29 chatlog columns fetched to use 22 (§4),
  * a dead `", ".join(...)` expression whose value was discarded, left behind
    under a "# Simpler: build rows in Python" comment,
  * one `INSERT` per row in a Python loop, and
  * `try: values.append(r[c]) except (KeyError, IndexError): values.append(None)`
    per column per row — so a chatlog store missing a column promoted it as NULL
    and reported success. Data loss shaped like a clean result.

The column list was also spelled out three times (writer + both promote
branches), the §10a duplication that lets a newly added column silently stop
being promoted. It now has one owner, `_ROW_COLUMNS`.

The test that matters most here is the faithful-copy one: it compares every
column, so it fails if any value arrives NULL — which is precisely what the old
per-column `except` produced silently.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "bin"))

from conftest import create_full_main_schema, isolate_chatlog_env  # noqa: E402

CONV = "promote-cross-file"


@pytest.fixture()
def two_files(tmp_path, monkeypatch):
    """A chatlog DB and a main DB in SEPARATE files — the cross-file branch.

    `isolate_chatlog_env` already points the two at different paths; both get the
    full schema so the 22 promoted columns exist on each side.
    """
    paths = isolate_chatlog_env(monkeypatch, tmp_path)
    create_full_main_schema(str(paths["db_path"]))
    create_full_main_schema(str(paths["main_db_path"]))
    paths["ids"] = _seed()
    return paths


def _seed(n: int = 3, conv: str = CONV) -> list[str]:
    """Write `n` turns through the CANONICAL writer, each column distinct.

    Using `_executemany_insert` rather than a hand-rolled INSERT keeps this
    honest: it is the writer whose columns promote is supposed to carry over, so
    if the two ever disagree this test sees it.
    """
    import chatlog_core

    ids = [f"{conv}-{i:03d}" for i in range(n)]
    batch = [{
        "_id": rid,
        "_title": f"title-{i}",
        "_content": f"content-{i}",
        "_metadata_json": json.dumps({"i": i}),
        "model_id": f"model-{i}",
        "agent_id": f"agent-{i}",
        "change_agent": f"changer-{i}",
        "origin_device": f"device-{i}",
        "user_id": f"user-{i}",
        "scope": f"scope-{i}",
        "expires_at": "2099-01-01T00:00:00Z",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2099-01-01T00:00:00Z",
        "refresh_on": "2027-01-01T00:00:00Z",
        "refresh_reason": f"reason-{i}",
        "variant": f"variant-{i}",
        "conversation_id": conv,
        "_created_at": "2026-09-29T00:00:00Z",
    } for i, rid in enumerate(ids)]
    assert chatlog_core._executemany_insert(batch) == n
    return ids


def _promote(**kw) -> dict:
    import chatlog_core
    return json.loads(asyncio.run(chatlog_core.chatlog_promote_impl(**kw)))


def _rows(db_path, where="1=1") -> dict[str, sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return {r["id"]: r for r in
                conn.execute(f"SELECT * FROM memory_items WHERE {where}")}
    finally:
        conn.close()


# ── the branch works at all ──────────────────────────────────────────────────

def test_promote_moves_rows_into_the_other_file(two_files):
    res = _promote(conversation_id=CONV, target_type="conversation")

    assert res["unified"] is False, "two distinct files is the cross-file case"
    assert res["promoted"] == 3
    assert sorted(res["ids"]) == [f"{CONV}-{i:03d}" for i in range(3)]

    promoted = _rows(two_files["main_db_path"])
    assert len(promoted) == 3
    assert {r["type"] for r in promoted.values()} == {"conversation"}


def test_every_column_survives_the_copy(two_files):
    """THE regression test. Compares all 22 columns, so a silently NULLed one
    fails here — which is exactly what the old per-column `except` produced."""
    import chatlog_core

    ids = two_files["ids"]
    source = _rows(two_files["db_path"], "type='chat_log'")
    _promote(ids=ids, target_type="conversation")
    copied = _rows(two_files["main_db_path"])

    assert set(copied) == set(source)
    for rid in source:
        for col in chatlog_core._ROW_COLUMNS:
            if col == "type":
                assert copied[rid]["type"] == "conversation"
                continue
            assert copied[rid][col] == source[rid][col], (
                f"column {col!r} did not survive promote for {rid}: "
                f"{copied[rid][col]!r} != {source[rid][col]!r}"
            )
        # and nothing arrived empty
        assert copied[rid]["content_hash"], "content_hash lost"
        assert copied[rid]["variant"], "variant lost"


def test_copy_true_leaves_the_chatlog_rows_in_place(two_files):
    _promote(conversation_id=CONV, copy=True)
    assert len(_rows(two_files["db_path"], "type='chat_log'")) == 3


def test_copy_false_removes_the_chatlog_rows(two_files):
    res = _promote(conversation_id=CONV, copy=False)
    assert res["promoted"] == 3
    assert _rows(two_files["db_path"], "type='chat_log'") == {}
    assert len(_rows(two_files["main_db_path"])) == 3


def test_promote_is_idempotent(two_files):
    """The second pass must not duplicate or raise — the conflict clause is
    seam-routed (`insert_or_ignore` + `on_conflict_ignore`), not a hard-coded
    `INSERT OR IGNORE`."""
    first = _promote(conversation_id=CONV)
    second = _promote(conversation_id=CONV)
    assert first["promoted"] == second["promoted"] == 3
    assert len(_rows(two_files["main_db_path"])) == 3


def test_no_match_reports_zero_rather_than_failing(two_files):
    res = _promote(conversation_id="no-such-conversation")
    assert res == {"promoted": 0, "ids": [], "unified": False}


def test_promote_requires_a_selector(two_files):
    """Pre-existing contract: an unfiltered promote would take the whole store."""
    with pytest.raises(ValueError, match="promote requires"):
        _promote()


# ── a missing column is reported, not silently promoted as NULL ──────────────

def test_a_missing_column_is_reported_not_nulled(two_files, monkeypatch):
    """The old code turned an absent column into NULL, per row, silently.

    Simulated by dropping a column from the chatlog table — the shape a store
    left behind by an unapplied migration has.
    """
    conn = sqlite3.connect(str(two_files["db_path"]))
    try:
        # SQLite refuses DROP COLUMN while an index references it
        # ("error in index idx_memory_items_variant after drop column"), so clear
        # the dependants first. Derived from the catalog rather than hard-coded,
        # because an index added later would silently re-skip this test.
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='memory_items' AND sql LIKE '%variant%'").fetchall():
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        conn.execute("ALTER TABLE memory_items DROP COLUMN variant")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError) as err:
        _promote(conversation_id=CONV)

    msg = str(err.value)
    assert "variant" in msg, msg
    assert "m3 doctor" in msg, "the error must say what to do about it"


# ── the column list has ONE owner ────────────────────────────────────────────

def test_row_columns_match_the_chatlog_schema(two_files):
    """`_ROW_COLUMNS` drifting from the table is the §10a failure this constant
    exists to prevent: promote would carry a column the store does not have, or
    quietly stop carrying one it does."""
    import chatlog_core

    conn = sqlite3.connect(str(two_files["db_path"]))
    try:
        have = {r[1] for r in conn.execute("PRAGMA table_info(memory_items)")}
    finally:
        conn.close()
    missing = [c for c in chatlog_core._ROW_COLUMNS if c not in have]
    assert not missing, f"_ROW_COLUMNS names columns the chatlog lacks: {missing}"


def test_row_columns_has_no_duplicates_and_leads_with_id():
    """id first, type second — the promote SELECT binds the type placeholder by
    POSITION, so reordering these two silently promotes the wrong value."""
    import chatlog_core

    cols = chatlog_core._ROW_COLUMNS
    assert len(cols) == len(set(cols)), "duplicate column in _ROW_COLUMNS"
    assert cols[0] == "id"
    assert cols[1] == "type"
