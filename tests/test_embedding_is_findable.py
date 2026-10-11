"""Every embedded memory must be REACHABLE by search, not merely embedded.

The failure this guards against is nastier than a missing embedding, because
every health check reports success:

  * ``memory_get`` returns the row, with full correct content.
  * ``m3 embedder backfill`` reports **0 pending** -- an embedding row exists,
    and the sweeper's predicate only asks whether one EXISTS.
  * ``memory_search`` never returns it, at any score, for any query, including
    verbatim strings from its own body.

Root cause (fixed 2026-09-08 in memory/write.py): a row too large for one
window was split, and EVERY window was labelled ``window_N``::

    base_kind = "default" if len(chunks) == 1 else f"window_{chunk_idx}"

but ``memory_search``'s back-compat strategy scores ONLY
``vector_kind = 'default'`` rows (search.py: ``where_clauses.append(
"me.vector_kind = 'default'")``). A windowed row therefore had embeddings that
no default-strategy search could ever see.

Observed on a 28,527-char procedure: exactly ONE such row among 3,864 live
memories. That is why it went unnoticed -- the defect needs a memory large
enough to window, so it lies dormant until someone writes a big one, and then
silently swallows precisely the most detailed memory in the store.

The fix names the FIRST window 'default' (later windows stay window_N and are
reachable via vector_kind_strategy="max"). These tests pin the invariant, not
the implementation.
"""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

import pytest

from conftest import skip_reason

_BIN = str(Path(__file__).resolve().parents[1] / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)


class TestFirstWindowIsDefault(unittest.TestCase):
    """Unit-level: the kind-naming rule itself."""

    @staticmethod
    def _kind(chunk_idx: int) -> str:
        """Mirror of memory/write.py's base_kind rule."""
        return "default" if chunk_idx == 0 else f"window_{chunk_idx}"

    def test_single_chunk_is_default(self):
        self.assertEqual(self._kind(0), "default")

    def test_first_of_many_is_still_default(self):
        """THE REGRESSION. A split row must keep a 'default' vector or the
        default search strategy can never reach it."""
        self.assertEqual(
            self._kind(0),
            "default",
            "the first window of a multi-window memory must be 'default'",
        )

    def test_later_windows_keep_distinct_kinds(self):
        """The fix must not collapse every window onto one kind -- that would
        make them indistinguishable and break the 'max' strategy's dedupe."""
        self.assertEqual(self._kind(1), "window_1")
        self.assertEqual(self._kind(2), "window_2")

    def test_source_uses_chunk_index_not_chunk_count(self):
        """Pin the actual line: `len(chunks) == 1` was the bug -- it asks "is
        this row small?" when the question is "is this the FIRST window?"."""
        import inspect

        w = importlib.import_module("memory.write")

        src = inspect.getsource(w)
        code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
        self.assertIn('base_kind = "default" if chunk_idx == 0', code)
        self.assertNotIn('base_kind = "default" if len(chunks) == 1', code)


def _seed_one_healthy_one_unfindable(conn, d):
    """A split memory WITH a 'default' vector, and one with only window_N."""
    import struct
    import uuid

    from conftest import seed_embedding_row, seed_memory_row  # type: ignore

    vec = struct.pack("<4f", 1.0, 0.0, 0.0, 0.0)
    tag = uuid.uuid4().hex[:10]
    healthy, orphan = f"findable-{tag}", f"unfindable-{tag}"
    for mid in (healthy, orphan):
        seed_memory_row(conn, d, mid=mid, title=mid, content="body " * 20)
    for mid, kinds in ((healthy, (None, "window_1")), (orphan, ("window_0", "window_1"))):
        for i, kind in enumerate(kinds):
            seed_embedding_row(
                conn, d, mid=mid, embedding=vec, dim=4, embed_model="test-model", emb_id=f"e{i}-{mid}", vector_kind=kind
            )
    conn.commit()
    return healthy, orphan


def test_unfindable_query_flags_only_the_unreachable_row_sqlite(tmp_path):
    """The doctor probe's predicate (the one owner, not a copy), against a
    real-schema SQLite store: the planted window_N-only row is flagged, the
    healthy split row is not."""
    import sqlite3

    from doctor.embed_space_probe import unfindable_memories
    from memory.backends.dialect import dialect_for

    from conftest import create_full_main_schema  # type: ignore

    db = tmp_path / "agent_memory.db"
    create_full_main_schema(db)
    conn = sqlite3.connect(str(db))
    try:
        d = dialect_for("sqlite")
        healthy, orphan = _seed_one_healthy_one_unfindable(conn, d)
        ids = {row[0] for row in unfindable_memories(conn, d, limit=100)}
    finally:
        conn.close()
    assert orphan in ids, "an embedded-but-unfindable row was not reported"
    assert healthy not in ids, "a row with a 'default' vector was reported unfindable"


@pytest.mark.requires_pg
def test_unfindable_query_flags_only_the_unreachable_row_postgres():
    """Same predicate on the lane's PostgreSQL store, through the seam."""
    from doctor.embed_space_probe import unfindable_memories
    from memory.backends import dialect
    from memory.db import _db

    d = dialect()
    with _db() as conn:
        healthy, orphan = _seed_one_healthy_one_unfindable(conn, d)
        try:
            ids = {row[0] for row in unfindable_memories(conn, d, limit=10000)}
        finally:
            p = d.param()
            for mid in (healthy, orphan):
                conn.execute(f"DELETE FROM memory_embeddings WHERE memory_id = {p}", (mid,))
                conn.execute(f"DELETE FROM memory_items WHERE id = {p}", (mid,))
            conn.commit()
    assert orphan in ids, "an embedded-but-unfindable row was not reported"
    assert healthy not in ids, "a row with a 'default' vector was reported unfindable"


class TestNoEmbeddedButUnfindableRows(unittest.TestCase):
    """Store-level: the invariant, checked against the developer's real database.

    A live-store check, not a unit test: it skips wherever no real store exists,
    which includes every test lane (the sandbox hides ~/.m3). The predicate
    itself is pinned hermetically by the two tests above, and `m3 doctor` runs
    it against the live store on every invocation.
    """

    def _live_db(self):
        """The REAL engine store, not the suite's temp fixture.

        conftest redirects M3_DATABASE at a throwaway path, so
        resolve_db_path(None) here returns a tmp DB that has no rows and
        "unable to open database file" -> the check would skipTest and guard
        NOTHING while reading as coverage (the exact failure mode
        tests/test_doctor_brief_visibility.py exists to forbid).

        So resolve the engine root directly. Skipping is still correct when
        there genuinely is no local store (CI, a fresh clone) -- but then the
        file is absent, not merely unreadable.
        """
        try:
            from memory.backends import active_backend
        except Exception as exc:  # pragma: no cover - seam unavailable
            self.skipTest(skip_reason("dep", f"seam not importable: {exc}"))

        import os

        root = os.environ.get("M3_ENGINE_ROOT") or os.path.join(os.path.expanduser("~"), ".m3", "engine")
        path = os.path.join(root, "agent_memory.db")
        if not os.path.isfile(path):
            self.skipTest(
                skip_reason(
                    "env",
                    f"no local engine store at {path} (the test sandbox "
                    "redirects M3_ENGINE_ROOT; `m3 doctor` runs this check live)",
                )
            )
        return active_backend(), path

    def test_every_embedded_memory_has_a_default_vector(self):
        """Delegates to the doctor probe's query rather than restating it.

        A second copy of this predicate here would be the §10a defect the probe
        itself documents: copies drift, and the one in a test drifts silently
        because nothing cross-checks them. The probe is also the component that
        actually runs against a live store on every `m3 doctor`.
        """
        backend, path = self._live_db()
        try:
            from doctor.embed_space_probe import unfindable_memories
            from memory.backends import dialect

            with backend.open_readonly(str(path)) as conn:
                rows = unfindable_memories(conn, dialect(), limit=20)
        except Exception as exc:  # pragma: no cover - no store / no table yet
            self.skipTest(skip_reason("env", f"store not queryable: {exc}"))

        self.assertEqual(
            [],
            list(rows),
            "These memories are EMBEDDED BUT UNFINDABLE: they have embedding "
            "rows (so `m3 embedder backfill` reports 0 pending) but no "
            "vector_kind='default' row, and memory_search's default strategy "
            "scores only 'default' rows -- so semantic search can never return "
            "them at any score. Re-embed them: "
            "`m3 embedder backfill --db <path> --force`. "
            f"Offenders (id, content_chars): {list(rows)}",
        )


if __name__ == "__main__":
    unittest.main()
