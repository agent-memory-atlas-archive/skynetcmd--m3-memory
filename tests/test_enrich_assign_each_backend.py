"""m3_enrich_assign routes enrichment_groups rows on EVERY backend.

It refused on PostgreSQL (require_sqlite_backend), and PostgreSQL had no
`send_to` column at all: migration 031 was never ported, so a routed
`m3_enrich --send-to` run failed there too (pg_060 adds it).
"""

from __future__ import annotations

import sys
import uuid

import pytest
from m3_core.paths import pin_database

from conftest import skip_reason

_COLS = (
    "source_variant",
    "target_variant",
    "group_key",
    "user_id",
    "db_path",
    "turn_count",
    "source_content_hash",
    "status",
    "content_size_k",
)


@pytest.fixture
def groups(each_backend):
    """Three groups (sizes 4, 10, 20 KB) under variants unique to this run."""
    from memory.backends import dialect

    src, tgt = (f"assign-{uuid.uuid4().hex[:8]}-{x}" for x in ("src", "tgt"))
    p = dialect().param()
    with each_backend.connection() as conn:
        for i, size in enumerate((4, 10, 20)):
            conn.execute(
                f"INSERT INTO enrichment_groups ({', '.join(_COLS)}) VALUES ({', '.join([p] * len(_COLS))})",
                (src, tgt, f"g{i}", "", "x.db", 1, f"h{i}", "pending", size),
            )
        conn.commit()
    try:
        yield src, tgt
    finally:
        with each_backend.connection() as conn:
            conn.execute(f"DELETE FROM enrichment_groups WHERE source_variant = {p}", (src,))
            conn.commit()


def _assign(monkeypatch, *argv: str) -> int:
    import m3_enrich_assign as ea

    monkeypatch.setattr(sys, "argv", ["m3_enrich_assign", *argv])
    return ea.main()


def _routes(each_backend, src: str) -> dict[str, str | None]:
    from memory.backends import dialect

    p = dialect().param()
    with each_backend.connection() as conn:
        return {
            r[0]: r[1]
            for r in conn.execute(
                f"SELECT group_key, send_to FROM enrichment_groups WHERE source_variant = {p}", (src,)
            )
        }


def test_routes_by_size_band(each_backend, groups, monkeypatch):
    src, tgt = groups
    where = f"on {each_backend.name}. inspect: bin/m3_enrich_assign.py"
    base = ("--source-variant", src, "--target-variant", tgt)

    assert _assign(monkeypatch, *base, "--max-size-k", "7", "--send-to", "grok") == 0
    assert _assign(monkeypatch, *base, "--min-size-k", "16", "--send-to", "gemini") == 0
    assert _routes(each_backend, src) == {"g0": "grok", "g1": None, "g2": "gemini"}, where

    assert _assign(monkeypatch, *base, "--only-unassigned", "--send-to", "local") == 0
    assert _routes(each_backend, src) == {"g0": "grok", "g1": "local", "g2": "gemini"}, where

    assert _assign(monkeypatch, *base, "--send-to", "grok", "--dry-run") == 0
    assert _routes(each_backend, src)["g2"] == "gemini", f"observed: --dry-run wrote {where}"

    assert _assign(monkeypatch, *base, "--send-to", "NULL") == 0
    assert set(_routes(each_backend, src).values()) == {None}, where


def test_a_routed_worker_claims_only_its_rows(each_backend, groups, monkeypatch):
    """The claim half of routing: `m3_enrich --send-to` reads through
    eligible_for_resume, which failed on PostgreSQL's missing column."""
    from enrichment_state import eligible_for_resume

    src, tgt = groups
    assert (
        _assign(monkeypatch, "--source-variant", src, "--target-variant", tgt, "--max-size-k", "7", "--send-to", "grok")
        == 0
    )
    with each_backend.connection() as conn:
        grok = eligible_for_resume(conn, source_variant=src, target_variant=tgt, send_to="grok")
        unrouted = eligible_for_resume(conn, source_variant=src, target_variant=tgt)
    assert [g for _id, g, _u in grok] == ["g0"], f"observed: {grok!r} on {each_backend.name}"
    assert sorted(g for _id, g, _u in unrouted) == ["g0", "g1", "g2"]


def test_explicit_db_wins_over_m3_database(each_backend, groups, monkeypatch, tmp_path):
    """--db must name the store acted on even when M3_DATABASE points elsewhere
    (the seam ranks that env above active_database, which once sent this tool's
    UPDATE to the main store)."""
    if not each_backend.stores_databases_as_files:
        pytest.skip(skip_reason("db", "only a file backend has a --db to honour"))
    import sqlite3

    from m3_sdk import resolve_db_path

    target = resolve_db_path(None)
    decoy = tmp_path / "decoy.db"
    sqlite3.connect(decoy).close()
    pin_database(str(decoy))
    src, tgt = groups
    assert (
        _assign(
            monkeypatch,
            "--db",
            target,
            "--source-variant",
            src,
            "--target-variant",
            tgt,
            "--max-size-k",
            "7",
            "--send-to",
            "grok",
        )
        == 0
    )
    pin_database(target)
    assert _routes(each_backend, src)["g0"] == "grok", "observed: the update did not reach --db"


def test_db_on_a_server_backend_is_refused(each_backend, monkeypatch, capsys):
    base = ("--source-variant", "s", "--target-variant", "t", "--send-to", "x")
    if each_backend.name == "sqlite":
        assert _assign(monkeypatch, "--db", "no-such-dir/none.db", *base) == 2
        assert "observed: no SQLite DB at" in capsys.readouterr().err
    else:
        assert _assign(monkeypatch, "--db", "agent_memory.db", *base) == 2
        assert "observed: --db names a SQLite file" in capsys.readouterr().err
