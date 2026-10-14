"""The vector copy doctor probe and the cognitive loop's fill, against a fake
backend: what each state prints, that --fix re-applies and fills in committed
batches, and that the loop's fill respects its budget. The SQL itself is
covered live by test_insert_embeddings_seam_pg_live.py."""

from __future__ import annotations

import importlib
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from memory.backends.base import VectorCopyStatus  # noqa: E402


class _Conn:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


class _Backend:
    def __init__(self, status, pending=0):
        self.status = status
        self.pending = pending
        self.ensured = 0
        self.conn = _Conn()

    @contextmanager
    def connection(self):
        yield self.conn

    def vector_copy_status(self, conn):
        if self.status is None:
            return None
        return VectorCopyStatus(
            self.status.available, self.status.installed, dict.fromkeys(self.status.missing, self.pending)
        )

    def ensure_vector_copy(self, conn):
        self.ensured += 1
        return True

    def fill_vector_copy(self, conn, *, table="memory_embeddings", limit=1000):
        n = min(limit, self.pending)
        self.pending -= n
        return n


@pytest.fixture
def use_backend(monkeypatch):
    mb = importlib.import_module("memory.backends")

    def _use(backend):
        monkeypatch.setattr(mb, "active_backend", lambda: backend)
        return backend

    return _use


def _run(**kw):
    from doctor import vector_copy_probe

    return vector_copy_probe.run(**kw)


@pytest.mark.parametrize(
    ("status", "pending", "expected"),
    [
        (VectorCopyStatus(False, False), 0, "ℹ️  pgvector: not offered"),
        (VectorCopyStatus(True, False), 0, "ℹ️  pgvector: offered but not created"),
        (VectorCopyStatus(True, True), 0, "⚠️  pgvector: created, but the vector column is missing"),
        (VectorCopyStatus(True, True, {"memory_embeddings": 0}), 3, "⚠️  pgvector: 3 embedding(s) lack"),
        (VectorCopyStatus(True, True, {"memory_embeddings": 0}), 0, "✅ pgvector: every embedding"),
    ],
)
def test_each_state_prints_its_line(use_backend, capsys, status, pending, expected):
    use_backend(_Backend(status, pending))
    assert _run(brief=True) == 0
    assert capsys.readouterr().out.startswith(expected)


def test_a_backend_without_a_copy_is_silent_in_brief(use_backend, capsys):
    use_backend(_Backend(None))
    assert _run(brief=True) == 0
    assert capsys.readouterr().out == ""


def test_fix_reapplies_and_fills_in_committed_batches(use_backend, capsys, monkeypatch):
    from doctor import vector_copy_probe

    monkeypatch.setattr(vector_copy_probe, "_FILL_BATCH", 2)
    b = use_backend(_Backend(VectorCopyStatus(True, True, {"memory_embeddings": 0}), pending=5))
    assert _run(brief=False, fix=True) == 0
    out = capsys.readouterr().out
    assert b.ensured == 1 and b.pending == 0
    assert b.conn.commits >= 1 + 3  # the ensure, then one per batch of 2
    assert "✅ pgvector: every embedding" in out and "filled   : 5 row(s) in memory_embeddings" in out


def test_a_failing_backend_is_reported_not_raised(use_backend, capsys):
    class _Broken(_Backend):
        def vector_copy_status(self, conn):
            raise RuntimeError("connection refused")

    use_backend(_Broken(None))
    assert _run(brief=True) == 0
    assert "could not read its state (RuntimeError: connection refused)" in capsys.readouterr().out


def test_the_loop_fill_respects_its_budget(use_backend):
    import m3_cognitive_loop as loop

    b = use_backend(_Backend(VectorCopyStatus(True, True, {"memory_embeddings": 0}), pending=2500))
    assert loop._vector_copy_missing() == 2500
    assert loop._fill_vector_copies(budget=1500) == 1500
    assert b.pending == 1000
    assert loop._fill_vector_copies(budget=5000) == 1000
    assert loop._vector_copy_missing() == 0
