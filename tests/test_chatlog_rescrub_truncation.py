"""chatlog_rescrub must not silently leave most of the store unexamined.

Found 2026-09-27 on a real store of 69,528 live chat_log rows. `chatlog_rescrub`
defaults to `limit=10_000` and the result was only:

    {"matched_rows": 1, "updated": 1}

Nothing said that 59,528 rows had never been looked at. An operator reads that as
"the store is clean" — it is the documented remediation step, so a cap that goes
unreported turns a hygiene tool into a no-op over most of the data. Re-running
with an explicit larger limit found 11 more rows.

Worse, the capped SELECT had **no ORDER BY**, so the slice it examined was
whatever order the backend happened to return. A capped run could re-examine the
same arbitrary rows on every invocation and never converge on the rest.

These tests pin both halves — deterministic coverage (lowest ids first) and an
HONEST report (truncated / scanned / remaining, plus the limit that finishes the
job) — and they run on BOTH supported backends: the `ORDER BY id`, the `LIMIT`
placeholder and the conditional `COUNT(*)` all go through the dialect seam
(§10a), and a claim about portability that only ever ran on SQLite is not a
claim (§0.4).
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "bin"))

# Assembled at import rather than written as a literal. The value is pure test
# bait, but spelled out it is credential-SHAPED, so it trips this repo's own
# pre-push leakage gate and every external secret scanner on a public remote --
# noise that trains people to wave such alerts through. The scrubber sees the
# assembled string, so what this file tests is unchanged.
SECRET = "sk-" + "ant-" + "abc123def456xyz789abcd"


# ── helpers ──────────────────────────────────────────────────────────────────

def _seed(n: int, *, conv: str = "rescrub-cap", prefix: str = "rescrub") -> list[str]:
    """Insert `n` rows each holding a scrubbable secret, ids ascending.

    Uses the CANONICAL chatlog writer rather than a hand-rolled INSERT — the
    codebase's own rule (chatlog_embed_sweeper.py:308) is that a second INSERT
    maintained in parallel drifts from the real one.

    `prefix` exists because a PostgreSQL cluster is PERSISTENT and SHARED: fixed
    ids collide with the previous run (`Key (id)=(rescrub-00000) already exists`),
    so the PG test passes only the first time it is ever run. SQLite gets a fresh
    tmp file per test and never noticed.
    """
    import chatlog_core

    ids = [f"{prefix}-{i:05d}" for i in range(n)]
    batch = [{
        "_id": rid,
        "_title": "rescrub-cap-test",
        "_content": f"turn {i} key={SECRET}",
        "_metadata_json": json.dumps({}),
        "model_id": "test-model",
        "conversation_id": conv,
        "_created_at": "2026-09-27T00:00:00Z",
    } for i, rid in enumerate(ids)]
    written = chatlog_core._executemany_insert(batch)
    assert written == n, f"seed wrote {written} of {n}"
    return ids


def _purge(conv: str) -> None:
    """Hard-delete one conversation's rows. Required on a shared backend so the
    test is repeatable rather than green-once (§10 DB hygiene)."""
    from m3_sdk import M3Context
    from memory.backends import chatlog_table, dialect
    ctx = M3Context.for_db(None)
    with ctx.get_chatlog_conn() as conn:
        conn.execute(
            f"DELETE FROM {chatlog_table('items')} "
            f"WHERE conversation_id = {dialect().param()}", (conv,))
        conn.commit()


def _enable_redaction(enabled: bool = True) -> None:
    import chatlog_config
    cfg = chatlog_config.resolve_config()
    cfg.redaction.enabled = enabled
    cfg.redaction.patterns = ["api_keys"]
    chatlog_config.save_config(cfg)


def _rescrub(**kw) -> dict:
    import chatlog_core
    return json.loads(asyncio.run(chatlog_core.chatlog_rescrub_impl(**kw)))


def _contents(conv: str | None = None) -> dict[str, str]:
    """id -> content, straight from the store.

    `conv` scopes the read. Unscoped is fine on a per-test SQLite file but wrong
    on a shared cluster, where it would also see every other run's rows.
    """
    from m3_sdk import M3Context
    from memory.backends import chatlog_table, dialect
    ctx = M3Context.for_db(None)
    sql = (f"SELECT id, content FROM {chatlog_table('items')} "
           "WHERE type='chat_log' AND is_deleted=0")
    params: tuple = ()
    if conv is not None:
        sql += f" AND conversation_id = {dialect().param()}"
        params = (conv,)
    with ctx.get_chatlog_conn() as conn:
        rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    return {r["id"]: r["content"] for r in rows}


def _still_secret(conv: str | None = None) -> list[str]:
    return sorted(i for i, c in _contents(conv).items() if SECRET in c)


@pytest.fixture()
def sqlite_store(tmp_path, monkeypatch):
    """A seeded SQLite chatlog store with redaction on.

    Roots are already tmp via the m3_sandbox autouse fixture, so nothing here
    can reach the developer's real chatlog.
    """
    from conftest import create_full_main_schema

    db = tmp_path / "chatlog.db"
    create_full_main_schema(str(db))
    monkeypatch.setenv("M3_DATABASE", str(db))
    monkeypatch.setenv("M3_CHATLOG_DB_PATH", str(db))
    _enable_redaction()
    return _seed(25)


# ── the defect: a cap that reports nothing ───────────────────────────────────

def test_capped_run_reports_what_it_did_not_examine(sqlite_store):
    """THE DEFECT. A cap must be visible in the result, with the remaining count
    and the limit that would finish the job."""
    res = _rescrub(limit=10)

    assert res["truncated"] is True
    assert res["scanned"] == 10
    assert res["limit"] == 10
    assert res["candidates"] == 25
    assert res["remaining"] == 15
    assert "15" in res["note"]
    assert "limit=25" in res["note"], res["note"]


def test_capped_run_leaves_the_rest_unscrubbed_and_says_so(sqlite_store):
    """The under-processing is real, not cosmetic: rows past the cap still hold
    the secret. The report is what stops an operator believing otherwise."""
    res = _rescrub(limit=10)

    untouched = _still_secret()
    assert len(untouched) == 15
    assert res["remaining"] == len(untouched), "report disagrees with the store"


def test_cap_takes_the_lowest_ids_so_successive_runs_converge(sqlite_store):
    """Without ORDER BY the cap took an arbitrary slice. Deterministic ordering
    is what lets a second, larger run finish the job instead of re-rolling the
    dice over the same rows."""
    _rescrub(limit=10)
    assert _still_secret() == [f"rescrub-{i:05d}" for i in range(10, 25)]

    res = _rescrub(limit=25)
    assert res["truncated"] is False
    assert _still_secret() == [], "did not converge"


def test_uncapped_run_reports_no_truncation(sqlite_store):
    """Full coverage must say so plainly — and must not pay for the extra COUNT
    that only the truncated path needs (§4)."""
    res = _rescrub(limit=1000)

    assert res["truncated"] is False
    assert res["scanned"] == 25
    assert res["matched_rows"] == 25
    assert res["updated"] == 25
    assert "candidates" not in res
    assert "remaining" not in res
    assert "note" not in res


def test_exactly_at_the_cap_is_not_reported_as_truncated(sqlite_store):
    """limit == row count is full coverage. An off-by-one here would cry wolf on
    every complete run, which trains operators to ignore the flag."""
    res = _rescrub(limit=25)
    assert res["scanned"] == 25
    assert res["truncated"] is False


def test_already_clean_rows_are_scanned_but_not_rewritten(sqlite_store):
    """A second full pass is a no-op: matched_rows counts rows that still HELD a
    secret, so an idempotent re-run reports 0 updates while still confirming it
    examined everything."""
    _rescrub(limit=1000)
    res = _rescrub(limit=1000)

    assert res["scanned"] == 25
    assert res["matched_rows"] == 0
    assert res["updated"] == 0
    assert res["truncated"] is False


def test_rescrub_refuses_when_redaction_is_off(tmp_path, monkeypatch):
    """Pre-existing contract, pinned because the cap work touches this path:
    scrubbing with redaction disabled would rewrite content against an empty
    pattern set."""
    from conftest import create_full_main_schema

    db = tmp_path / "chatlog.db"
    create_full_main_schema(str(db))
    monkeypatch.setenv("M3_DATABASE", str(db))
    monkeypatch.setenv("M3_CHATLOG_DB_PATH", str(db))
    _enable_redaction(False)

    with pytest.raises(ValueError, match="redaction.enabled must be true"):
        _rescrub(limit=10)


# ── the same claim on PostgreSQL ─────────────────────────────────────────────

@pytest.mark.requires_pg
def test_truncation_report_holds_on_postgres(monkeypatch, pg_url):
    """ORDER BY id, the LIMIT placeholder and the conditional COUNT(*) must all
    work on the server backend too.

    A SQLite-only green proves nothing about portability, and this is exactly the
    kind of change (a new ORDER BY + a second aggregate query on the same WHERE)
    where a dialect assumption would hide until a PG deployment ran it.
    """
    monkeypatch.setenv("M3_PRIMARY_PG_URL", pg_url)
    monkeypatch.setenv("M3_PG_URL", pg_url)
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")

    from memory.backends import dialect
    # Guard against a silent sqlite fallback reporting a hollow pass.
    assert dialect().backend == "postgres", (
        f"expected the postgres dialect, resolved {dialect().backend!r} — "
        "the test would otherwise 'pass' without exercising PG at all"
    )

    # A shared cluster keeps its rows: unique per run, scoped reads, and a purge
    # in `finally`, or this test is green exactly once.
    conv = f"rescrub-cap-pg-{uuid.uuid4().hex[:10]}"
    _enable_redaction()
    try:
        _seed(25, conv=conv, prefix=conv)

        capped = _rescrub(conversation_id=conv, limit=10)
        assert capped["truncated"] is True
        assert capped["scanned"] == 10
        assert capped["candidates"] == 25
        assert capped["remaining"] == 15
        assert _still_secret(conv) == [f"{conv}-{i:05d}" for i in range(10, 25)]

        full = _rescrub(conversation_id=conv, limit=25)
        assert full["truncated"] is False
        assert full["scanned"] == 25
        assert _still_secret(conv) == [], "did not converge on postgres"
    finally:
        _purge(conv)
