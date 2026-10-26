"""Tests for bin/embed_backfill.py — query construction, hardening, and write path.

The embed_many call is mocked to keep tests offline; we assert:
  - the candidate query filters out already-embedded rows, soft-deleted rows, and empty content
  - filters (variant, type, user_id, scope, id_prefix, max_age_days) compose into the SQL
  - empty content is skipped without an embed call
  - oversized rows are skipped
  - mismatched dim is skipped, not written
  - successful batch writes memory_embeddings
  - INSERT OR IGNORE protects against double-write on memory_id
  - lockfile prevents two concurrent sweepers
  - schema sanity check fails clearly on missing tables
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import pytest
from m3_core.paths import configured_main_db, pin_database

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bin"))


# ── Fixtures ──────────────────────────────────────────────────────────────


def _make_min_schema(db_path: Path) -> None:
    """The real main schema (memory_items + memory_embeddings with content_hash,
    which embed_backfill writes). A hand-built store is one the lazy migrator
    cannot upgrade, and its failure now raises instead of being logged."""
    from conftest import create_full_main_schema

    create_full_main_schema(db_path)


def _seed_rows(db_path: Path, rows: list[dict]) -> None:
    """Insert memory_items rows. rows = [{id, content, type, variant, ...}]."""
    conn = sqlite3.connect(str(db_path))
    for r in rows:
        conn.execute(
            "INSERT INTO memory_items "
            "(id, type, title, content, metadata_json, is_deleted, created_at, "
            " user_id, scope, variant) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["id"],
                r.get("type", "note"),
                r.get("title", ""),
                r.get("content", ""),
                r.get("metadata_json", "{}"),
                r.get("is_deleted", 0),
                r.get("created_at", "2026-05-01T00:00:00Z"),
                r.get("user_id", ""),
                r.get("scope", "agent"),
                r.get("variant", None),
            ),
        )
    conn.commit()
    conn.close()


def _make_args(db_path: Path, **overrides) -> argparse.Namespace:
    """Build a minimal Namespace with the fields _build_query / _run_sweep need."""
    base = {
        "db": db_path,
        "variant": [],
        "type": [],
        "user_id": None,
        "scope": None,
        "id_prefix": None,
        "max_age_days": None,
        "limit": None,
        "batch_size": 4,
        "concurrency": 1,
        "connection_refresh": 1000,
        "timeout_s": 10.0,
        "max_runtime_min": 1,
        "max_consecutive_fails": 3,
        "max_row_bytes": 32_768,
        "expected_dim": 4,  # tiny dim for test vectors
        "lockfile": None,
        "no_augment_anchors": True,  # don't pull metadata-driven anchors in tests
        # Mirror the CLI default. Without this the getattr() fallback silently
        # put every test in the legacy skip mode while production subdivides.
        "subdivide_oversize": True,
        "dry_run": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "test.db"
    _make_min_schema(p)
    return p


# ── Query construction tests ──────────────────────────────────────────────


def test_build_query_excludes_already_embedded(db):
    import embed_backfill as eb

    _seed_rows(
        db,
        [
            {"id": "row-A", "content": "alpha"},
            {"id": "row-B", "content": "beta"},
        ],
    )
    # Pretend row-A already has an embedding
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at, content_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("e1", "row-A", b"\x00" * 16, "test", 4, "2026-05-01T00:00:00Z", "h_alpha"),
    )
    conn.commit()
    conn.close()

    args = _make_args(db)
    n = eb._count_pending(db, args)
    assert n == 1


def test_build_query_excludes_soft_deleted(db):
    import embed_backfill as eb

    _seed_rows(
        db,
        [
            {"id": "row-A", "content": "alpha", "is_deleted": 0},
            {"id": "row-B", "content": "beta", "is_deleted": 1},
        ],
    )
    args = _make_args(db)
    assert eb._count_pending(db, args) == 1


def test_build_query_excludes_empty_content(db):
    import embed_backfill as eb

    _seed_rows(
        db,
        [
            {"id": "row-A", "content": "alpha"},
            {"id": "row-B", "content": ""},
            {"id": "row-C", "content": "   "},  # whitespace-only
            {"id": "row-D", "content": None},
        ],
    )
    args = _make_args(db)
    assert eb._count_pending(db, args) == 1


def test_build_query_filter_variant(db):
    import embed_backfill as eb

    _seed_rows(
        db,
        [
            {"id": "row-A", "content": "alpha", "variant": "VARIANT_X"},
            {"id": "row-B", "content": "beta", "variant": "VARIANT_Y"},
            {"id": "row-C", "content": "gamma", "variant": None},
        ],
    )
    args = _make_args(db, variant=["VARIANT_X"])
    assert eb._count_pending(db, args) == 1

    args = _make_args(db, variant=["VARIANT_X", "VARIANT_Y"])
    assert eb._count_pending(db, args) == 2


def test_build_query_filter_type(db):
    import embed_backfill as eb

    _seed_rows(
        db,
        [
            {"id": "row-A", "content": "alpha", "type": "note"},
            {"id": "row-B", "content": "beta", "type": "decision"},
        ],
    )
    args = _make_args(db, type=["note"])
    assert eb._count_pending(db, args) == 1


def test_build_query_filter_user_id_and_scope(db):
    import embed_backfill as eb

    _seed_rows(
        db,
        [
            {"id": "row-A", "content": "alpha", "user_id": "u1", "scope": "user"},
            {"id": "row-B", "content": "beta", "user_id": "u2", "scope": "agent"},
        ],
    )
    args = _make_args(db, user_id="u1")
    assert eb._count_pending(db, args) == 1
    args = _make_args(db, scope="agent")
    assert eb._count_pending(db, args) == 1


def test_build_query_filter_id_prefix(db):
    import embed_backfill as eb

    _seed_rows(
        db,
        [
            {"id": "abc-A", "content": "alpha"},
            {"id": "abd-B", "content": "beta"},
            {"id": "xyz-C", "content": "gamma"},
        ],
    )
    args = _make_args(db, id_prefix="ab")
    assert eb._count_pending(db, args) == 2


# ── Schema sanity tests ──────────────────────────────────────────────────


def test_verify_schema_missing_db_errors(tmp_path):
    """A bad --db must fail LOUD. The exception TYPE changed deliberately.

    _verify_schema used to gate on `db_path.exists()` and raise
    FileNotFoundError. That gate is a SQLite assumption: on PostgreSQL there is
    no DB file, so it would abort a backfill against a perfectly healthy store
    before a single row was read.

    With the gate gone, a nonexistent SQLite path opens as an EMPTY database and
    the very next check -- "does memory_items exist?" -- fires instead. Same
    contract (loud, non-silent, names the path), and the message is strictly
    more useful: it also tells the operator to run migrate_memory. So this
    asserts the CONTRACT, not the historical exception class.
    """
    import embed_backfill as eb

    bad = tmp_path / "nope.db"
    with pytest.raises((FileNotFoundError, RuntimeError)) as exc:
        eb._verify_schema(bad)
    assert "nope.db" in str(exc.value), "the error must name the offending path"


def test_verify_schema_missing_table_errors(tmp_path):
    import embed_backfill as eb

    p = tmp_path / "bare.db"
    sqlite3.connect(str(p)).close()  # empty DB, no tables
    with pytest.raises(RuntimeError, match="memory_items"):
        eb._verify_schema(p)


def test_verify_schema_minimal_ok(db):
    import embed_backfill as eb

    eb._verify_schema(db)  # should not raise


# ── Lockfile tests ────────────────────────────────────────────────────────


def test_lockfile_creates_and_deletes(tmp_path):
    import embed_backfill as eb

    lock = tmp_path / "lock.txt"
    assert not lock.exists()
    with eb._lockfile_guard(lock):
        assert lock.exists()
        # content is "<pid> <epoch>"
        content = lock.read_text(encoding="utf-8")
        parts = content.strip().split()
        assert int(parts[0]) == os.getpid()
        assert int(parts[1]) > 0
    assert not lock.exists()


def test_lockfile_blocks_second_sweeper(tmp_path):
    import embed_backfill as eb

    lock = tmp_path / "lock.txt"
    lock.write_text("99999 1234567890", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Lockfile already exists"):
        with eb._lockfile_guard(lock):
            pass
    # should leave the existing lockfile in place
    assert lock.exists()


def test_lockfile_none_is_noop(tmp_path):
    import embed_backfill as eb

    with eb._lockfile_guard(None):
        pass  # no-op; just shouldn't error


# ── Counters ─────────────────────────────────────────────────────────────


def test_counters_record_error():
    import embed_backfill as eb

    c = eb.Counters()
    c.record_error(ValueError("x"))
    c.record_error(ValueError("y"))
    c.record_error(KeyError("z"))
    assert c.errors_by_class == {"ValueError": 2, "KeyError": 1}


# ── End-to-end async sweep with mocked _embed_many ────────────────────────


@pytest.mark.asyncio
async def test_sweep_writes_embeddings_and_queue(db, monkeypatch):
    """Full async sweep with _embed_many mocked. Assert embeddings land."""
    import embed_backfill as eb

    _seed_rows(
        db,
        [
            {"id": "row-A", "content": "alpha cat"},
            {"id": "row-B", "content": "beta dog"},
        ],
    )

    # Mock _embed_many BEFORE _run_sweep imports memory_core (it pins
    # the main database first, then late-imports). We monkeypatch on the module
    # after import.
    args = _make_args(db)
    counters = eb.Counters()

    # _run_sweep imports memory_core lazily; trigger the import here
    # so we can monkeypatch _embed_many before sweep dispatches.
    pin_database(str(db))
    if str(REPO / "bin") not in sys.path:
        sys.path.insert(0, str(REPO / "bin"))
    import memory_core as mc

    async def fake_embed_many(texts):
        return [([0.1, 0.2, 0.3, 0.4], "test-model") for _ in texts]

    monkeypatch.setattr(mc, "_embed_many", fake_embed_many)
    # Bypass _augment_embed_text_with_anchors variability since we're
    # passing no_augment_anchors=True anyway, but be defensive.
    monkeypatch.setattr(mc, "_augment_embed_text_with_anchors", lambda text, _meta: text)
    # _content_hash + _pack are real — we want them to produce real bytes

    await eb._run_sweep(args, counters)

    # Verify both rows got embedded
    assert counters.embedded == 2
    assert counters.skipped_empty == 0
    assert counters.skipped_oversize == 0
    assert counters.skipped_bad_dim == 0

    conn = sqlite3.connect(str(db))
    n_embeddings = conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
    conn.close()
    assert n_embeddings == 2


@pytest.mark.asyncio
async def test_sweep_skips_oversize(db, monkeypatch):
    """--no-subdivide-oversize (legacy) mode: drop the row, exclude it from
    pending so it cannot livelock the loop, and report it as EXCLUDED."""
    import embed_backfill as eb

    big = "x" * 50_000  # 50KB > default 32KB
    _seed_rows(
        db,
        [
            {"id": "row-A", "content": big},
            {"id": "row-B", "content": "small"},
        ],
    )

    pin_database(str(db))
    import memory_core as mc

    embed_calls = []

    async def fake_embed_many(texts):
        embed_calls.append(list(texts))
        return [([0.1, 0.2, 0.3, 0.4], "test-model") for _ in texts]

    monkeypatch.setattr(mc, "_embed_many", fake_embed_many)

    args = _make_args(db, max_row_bytes=32_768, subdivide_oversize=False)
    counters = eb.Counters()
    await eb._run_sweep(args, counters)

    # In skip mode the candidate QUERY excludes oversize rows, so they are never
    # selected and skipped_oversize stays 0. (The per-row skip is still live and
    # still needed: anchor augmentation can push a row over the cap AFTER it was
    # selected — see test_sweep_skips_oversize_after_augment.)
    assert counters.skipped_oversize == 0
    assert counters.embedded == 1
    # The oversize row should not have been sent to the embedder
    flat = [t for batch in embed_calls for t in batch]
    assert big not in flat

    # The livelock guard (2026-08-09): an oversize row must NOT count as pending,
    # or has_embed_work() reports work that can never drain and the cognitive
    # loop pins itself to its 1s idle-backlog floor forever. It must still be
    # VISIBLE though — excluded is not the same as forgotten.
    assert eb._count_pending(db, args) == 0
    assert eb.count_oversize_excluded(db, args) == 1


@pytest.mark.asyncio
async def test_sweep_skips_oversize_after_augment(db, monkeypatch):
    """The per-row oversize skip must stay live even though the candidate query
    now filters on size: anchor augmentation runs BEFORE the size check
    (embed_sweep_lib ~L161), so a row that is under the cap on disk can exceed
    it once anchors are prepended. Query-time filtering alone would let that row
    reach the embedder and fail the batch."""
    import embed_backfill as eb

    _seed_rows(db, [{"id": "row-A", "content": "small enough on disk"}])

    pin_database(str(db))
    import memory_core as mc

    # Row passes the query filter, then augmentation blows it past the cap.
    monkeypatch.setattr(mc, "_augment_embed_text_with_anchors", lambda text, metadata: "y" * 50_000)

    embed_calls = []

    async def fake_embed_many(texts):
        embed_calls.append(list(texts))
        return [([0.1, 0.2, 0.3, 0.4], "test-model") for _ in texts]

    monkeypatch.setattr(mc, "_embed_many", fake_embed_many)

    # no_augment_anchors must be False or _run_sweep uses the identity transform
    # and the augmentation path under test never runs.
    args = _make_args(db, max_row_bytes=32_768, no_augment_anchors=False, subdivide_oversize=False)

    # Small on disk: the query DOES select it, and it counts as real pending work.
    assert eb._count_pending(db, args) == 1
    assert eb.count_oversize_excluded(db, args) == 0

    counters = eb.Counters()
    await eb._run_sweep(args, counters)

    # ...but the per-row check catches it after augmentation.
    assert counters.skipped_oversize == 1
    assert counters.embedded == 0
    assert embed_calls == []


@pytest.mark.asyncio
async def test_sweep_subdivides_oversize_by_default(db, monkeypatch):
    """Default mode: an oversize row is EMBEDDED, not dropped.

    embed_many already recovers a lone oversized row by splitting it and
    mean-pooling the sub-vectors (_recover_oversized_single); the only thing
    that ever stopped these rows was embed_backfill's own pre-filter. The row
    must therefore (a) stay in the candidate set, (b) reach embed_many ALONE so
    the lone-row recovery fires instead of failing a shared batch, and (c) be
    counted so "did subdivide actually happen?" is answerable from the report.
    """
    import embed_backfill as eb

    big = "x" * 50_000  # 50KB > default 32KB
    _seed_rows(
        db,
        [
            {"id": "row-A", "content": big},
            {"id": "row-B", "content": "small"},
        ],
    )

    pin_database(str(db))
    import memory_core as mc

    embed_calls = []

    async def fake_embed_many(texts):
        embed_calls.append(list(texts))
        return [([0.1, 0.2, 0.3, 0.4], "test-model") for _ in texts]

    monkeypatch.setattr(mc, "_embed_many", fake_embed_many)

    args = _make_args(db, max_row_bytes=32_768)  # subdivide_oversize=True
    assert eb._count_pending(db, args) == 2  # drainable, so pending
    assert eb.count_oversize_excluded(db, args) == 0  # nothing written off

    counters = eb.Counters()
    await eb._run_sweep(args, counters)

    assert counters.skipped_oversize == 0
    assert counters.oversize_subdivided == 1
    assert counters.embedded == 2

    # The oversize row must have been sent in a batch of its own.
    lone = [b for b in embed_calls if b == [big]]
    assert lone, f"oversize row was not sent alone: {[len(b) for b in embed_calls]}"
    assert all(big not in b for b in embed_calls if b != [big]), "oversize row must not ride along in a shared batch"

    # And it is genuinely gone from the backlog.
    assert eb._count_pending(db, args) == 0


@pytest.mark.asyncio
async def test_sweep_subdivides_oversize_after_augment(db, monkeypatch):
    """Same for a row that only becomes oversize after anchor augmentation —
    the case the candidate query cannot see."""
    import embed_backfill as eb

    _seed_rows(db, [{"id": "row-A", "content": "small enough on disk"}])

    pin_database(str(db))
    import memory_core as mc

    huge = "y" * 50_000
    monkeypatch.setattr(mc, "_augment_embed_text_with_anchors", lambda text, metadata: huge)

    embed_calls = []

    async def fake_embed_many(texts):
        embed_calls.append(list(texts))
        return [([0.1, 0.2, 0.3, 0.4], "test-model") for _ in texts]

    monkeypatch.setattr(mc, "_embed_many", fake_embed_many)

    args = _make_args(db, max_row_bytes=32_768, no_augment_anchors=False)
    counters = eb.Counters()
    await eb._run_sweep(args, counters)

    assert counters.skipped_oversize == 0
    assert counters.oversize_subdivided == 1
    assert counters.embedded == 1
    assert embed_calls == [[huge]]


@pytest.mark.asyncio
async def test_sweep_skips_bad_dim(db, monkeypatch):
    import embed_backfill as eb

    _seed_rows(db, [{"id": "row-A", "content": "alpha"}])

    pin_database(str(db))
    import memory_core as mc

    async def fake_embed_many(texts):
        # Wrong dim — 8 instead of expected 4
        return [([0.0] * 8, "test-model") for _ in texts]

    monkeypatch.setattr(mc, "_embed_many", fake_embed_many)
    monkeypatch.setattr(mc, "_augment_embed_text_with_anchors", lambda text, _meta: text)

    args = _make_args(db, expected_dim=4)
    counters = eb.Counters()
    await eb._run_sweep(args, counters)

    assert counters.skipped_bad_dim == 1
    assert counters.embedded == 0
    # No row should have been written
    conn = sqlite3.connect(str(db))
    n = conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
    conn.close()
    assert n == 0


@pytest.mark.asyncio
async def test_sweep_handles_embed_failure_gracefully(db, monkeypatch):
    import embed_backfill as eb

    _seed_rows(db, [{"id": "row-A", "content": "alpha"}])

    pin_database(str(db))
    import memory_core as mc

    async def fake_embed_many(texts):
        # _embed_many returns (None, model) on failure
        return [(None, "test-model") for _ in texts]

    monkeypatch.setattr(mc, "_embed_many", fake_embed_many)
    monkeypatch.setattr(mc, "_augment_embed_text_with_anchors", lambda text, _meta: text)

    args = _make_args(db)
    counters = eb.Counters()
    await eb._run_sweep(args, counters)

    # No rows embedded, but no batch failure either (None vector is a
    # legit "skip this one" outcome from _embed_many bisection)
    assert counters.embedded == 0


@pytest.mark.asyncio
async def test_sweep_resumes_skipping_already_embedded(db, monkeypatch):
    """Run sweep twice — second pass should embed nothing (resume by NOT EXISTS)."""
    import embed_backfill as eb

    _seed_rows(
        db,
        [
            {"id": "row-A", "content": "alpha"},
            {"id": "row-B", "content": "beta"},
        ],
    )

    pin_database(str(db))
    import memory_core as mc

    n_calls = [0]

    async def fake_embed_many(texts):
        n_calls[0] += len(texts)
        return [([0.1, 0.2, 0.3, 0.4], "test-model") for _ in texts]

    monkeypatch.setattr(mc, "_embed_many", fake_embed_many)
    monkeypatch.setattr(mc, "_augment_embed_text_with_anchors", lambda text, _meta: text)

    args = _make_args(db)
    counters1 = eb.Counters()
    await eb._run_sweep(args, counters1)
    assert counters1.embedded == 2
    assert n_calls[0] == 2

    # Second pass — nothing should embed
    counters2 = eb.Counters()
    await eb._run_sweep(args, counters2)
    assert counters2.embedded == 0
    assert n_calls[0] == 2  # no additional embed calls


# ── argparse smoke ────────────────────────────────────────────────────────


def test_parse_args_defaults(monkeypatch):
    import embed_backfill as eb

    monkeypatch.delenv("M3_DATABASE", raising=False)
    args = eb._parse_args([])
    assert args.batch_size == 256
    assert args.concurrency == 4
    assert args.timeout_s == 60.0
    assert args.dry_run is False
    assert args.lockfile is None


def test_parse_args_repeatable_filters():
    import embed_backfill as eb

    args = eb._parse_args(
        [
            "--variant",
            "A",
            "--variant",
            "B",
            "--type",
            "note",
            "--type",
            "decision",
        ]
    )
    assert args.variant == ["A", "B"]
    assert args.type == ["note", "decision"]


# ── M3_DATABASE must not leak out of a sweep (#180) ────────────────────────
# _run_sweep pins the main database (it wrote M3_DATABASE until phase 2) before
# its late import of memory_core. Leaking
# it poisons every later resolve_db_path(None) in the process: in the cognitive
# loop the value left behind was the CHATLOG db, so chat_store_paths() returned
# the chatlog path as its "main store", collapsed to one entry, and the chatlog
# store dropped out of _embed_target_dbs() entirely -- unembedded chat turns
# then grew without bound until a restart. These pin the restore, not the sweep.


@pytest.mark.asyncio
async def test_run_sweep_restores_m3_database(tmp_path, monkeypatch):
    """A sweep must leave the main-database pin exactly as it found it."""
    import embed_backfill as eb

    sentinel = str(tmp_path / "main.db")
    pin_database(sentinel)

    swept = tmp_path / "chatlog.db"
    _make_min_schema(swept)  # no rows: sweep is a no-op, restore still required

    await eb._run_sweep(_make_args(swept), eb.Counters())

    assert configured_main_db() == sentinel, "the main-database pin leaked the swept db; see #180"


@pytest.mark.asyncio
async def test_run_sweep_restores_m3_database_absence(tmp_path, monkeypatch):
    """Previously UNSET must come back unset, not as an empty string.

    A sweep with no pin before must leave no pin after: anything else makes
    chat_store_paths()/_main_path_from_env() see a main store that was never
    configured. Restoring absence as absence is the only faithful restore.
    """
    import embed_backfill as eb

    pin_database(None)

    swept = tmp_path / "chatlog.db"
    _make_min_schema(swept)

    await eb._run_sweep(_make_args(swept), eb.Counters())

    assert configured_main_db() is None, "absence must be restored as absence; see #180"
    assert "M3_DATABASE" not in os.environ, "a sweep must not write the environment"


@pytest.mark.asyncio
async def test_run_sweep_restores_m3_database_on_exception(tmp_path, monkeypatch):
    """The restore is in a finally: a failing sweep must not leak either.

    A locked or unreadable store is exactly when a sweep raises, and exactly
    when the loop goes on to sweep the next store -- so the error path is the
    one that matters most for #180.
    """
    import embed_backfill as eb

    sentinel = str(tmp_path / "main.db")
    pin_database(sentinel)

    swept = tmp_path / "chatlog.db"
    _make_min_schema(swept)

    boom = RuntimeError("sweep exploded")

    async def _explode(*_a, **_k):
        raise boom

    monkeypatch.setattr(eb, "_run_sweep_inner", _explode)

    with pytest.raises(RuntimeError):
        await eb._run_sweep(_make_args(swept), eb.Counters())

    assert configured_main_db() == sentinel, "the main-database pin leaked after a failing sweep; see #180"


_POST_SWEEP_STEPS = (
    "_run_owed_contradiction_checks",
    "_repair_unfindable",
    "_repair_stale_after_edit",
)


def _record_post_sweep_steps(eb, monkeypatch, *, failing: str | None = None) -> list[str]:
    ran: list[str] = []
    for name in _POST_SWEEP_STEPS:

        async def step(_args, name=name):
            ran.append(name)
            if name == failing:
                raise RuntimeError(f"{name} broke")

        monkeypatch.setattr(eb, name, step, raising=False)
    return ran


def _one_unembedded_row(store: Path, schema, monkeypatch):
    import embed_backfill as eb
    import memory_core as mc

    schema(store)
    _seed_rows(store, [{"id": "row-A", "content": "a row"}])
    pin_database(str(store))

    async def fake_embed_many(texts):
        return [([0.1, 0.2, 0.3, 0.4], "test-model") for _ in texts]

    monkeypatch.setattr(mc, "_embed_many", fake_embed_many)
    return eb, eb.Counters()


@pytest.mark.asyncio
async def test_post_sweep_steps_run_on_the_core_store_only(tmp_path, monkeypatch):
    """The chatlog file shares the core table names and the loop sweeps it as
    --target core, so only its tables say it is not the core store. Run there,
    the core-only steps fail every cycle on 'no such table: memory_history'."""
    from conftest import create_full_chatlog_schema, create_full_main_schema

    for name, schema, expected in (
        ("chatlog.db", create_full_chatlog_schema, []),
        ("main.db", create_full_main_schema, list(_POST_SWEEP_STEPS)),
    ):
        eb, counters = _one_unembedded_row(tmp_path / name, schema, monkeypatch)
        ran = _record_post_sweep_steps(eb, monkeypatch)
        await eb._run_sweep(_make_args(tmp_path / name), counters)
        assert counters.embedded == 1, f"observed: {counters.embedded} rows embedded in {name}"
        assert ran == expected, f"observed: post-sweep steps {ran} on {name}"


@pytest.mark.asyncio
async def test_a_failed_post_sweep_step_does_not_skip_the_others(tmp_path, monkeypatch):
    """Each step runs; the failure comes back as PostSweepError naming the step,
    so the loop can say the embedding landed instead of blaming the embedder."""
    import re

    from conftest import create_full_main_schema

    eb, counters = _one_unembedded_row(tmp_path / "main.db", create_full_main_schema, monkeypatch)
    ran = _record_post_sweep_steps(eb, monkeypatch, failing="_run_owed_contradiction_checks")
    with pytest.raises(eb.PostSweepError, match=re.escape("owed contradiction checks (RuntimeError")):
        await eb._run_sweep(_make_args(tmp_path / "main.db"), counters)
    assert counters.embedded == 1
    assert ran == list(_POST_SWEEP_STEPS), f"observed: only {ran} ran after the first failed"


@pytest.mark.asyncio
async def test_the_loop_does_not_blame_the_embedder_for_a_post_sweep_failure(tmp_path, monkeypatch, caplog):
    """A repair that raises after the embeddings landed is reported as that
    step's failure, with its traceback, never as a possible embedder outage."""
    import embed_backfill as eb
    import m3_cognitive_loop as loop

    async def sweep(_args, counters):
        counters.embedded = 3
        boom = sqlite3.OperationalError("no such table: memory_history")
        raise eb.PostSweepError(
            "main.db", ["owed contradiction checks", "unfindable-memory repair"], [("unfindable-memory repair", boom)]
        )

    async def no_spill():
        return None

    monkeypatch.setattr(loop, "has_embed_work", lambda _db: True)
    monkeypatch.setattr(loop, "_drain_chatlog_spill", no_spill)
    monkeypatch.setattr(loop, "_embed_targets", lambda _db: [(str(tmp_path / "main.db"), "core")])
    monkeypatch.setattr(loop, "_fill_vector_copies", lambda: 0)
    monkeypatch.setattr(eb, "_run_sweep", sweep)
    caplog.set_level("ERROR")
    await loop.run_embed_pass(argparse.Namespace(database=str(tmp_path / "main.db"), limit_per_pass=10))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "embedded 3 row(s)" in text and "unfindable-memory repair" in text, f"observed: {text!r}"
    assert "not the embedder" in text and "embedder is down" not in text, f"observed: {text!r}"
    assert "completed: owed contradiction checks" in text and "migrate_memory.py status" in text, f"observed: {text!r}"
    assert any(r.exc_info for r in caplog.records), "observed: the failed step's traceback was not logged"
    summary = caplog.records[0].getMessage()
    assert summary.count("main.db") == 1, f"observed: the store is named more than once: {summary!r}"


@pytest.mark.asyncio
async def test_one_failing_owed_check_does_not_stall_the_others(tmp_path, monkeypatch, capsys):
    """A row whose check raises must not abort the loop over the owed rows: the
    store returns the same rows first every sweep, so the rest would wait forever.
    The summary counts what was checked, not what was attempted."""
    import importlib

    import embed_backfill as eb

    from conftest import create_full_main_schema

    store = tmp_path / "main.db"
    create_full_main_schema(store)
    pin_database(str(store))
    write = importlib.import_module("memory.write")
    calls: list[str] = []

    async def check(mid):
        calls.append(mid)
        if mid == "a":
            raise ValueError("bad row")
        return None if mid == "b" else []

    monkeypatch.setattr(write, "owed_contradiction_checks", lambda _conn, limit: ["a", "b", "c"])
    monkeypatch.setattr(write, "run_deferred_contradiction_check", check)
    with pytest.raises(RuntimeError, match="1 of 3 owed checks raised; first: a"):
        await eb._run_owed_contradiction_checks(_make_args(store))
    assert calls == ["a", "b", "c"], f"observed: only {calls} were checked after the first raised"
    out = capsys.readouterr().out
    assert "3 owed, 1 checked, 0 memories superseded, 1 not run, 1 failed" in out, f"observed: {out!r}"
