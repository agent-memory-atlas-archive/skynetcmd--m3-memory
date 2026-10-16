"""files_promote's "already promoted" answer checks the memory row it names: a
row gdpr_forget erased is refused (re-promoting would restore erased content),
a soft-deleted one is reported as such."""

from __future__ import annotations

import importlib
from contextlib import contextmanager

import pytest


class _Conn:
    def __init__(self, marker):
        self._marker = marker

    def execute(self, sql, params=()):
        marker = self._marker

        class _Cur:
            def fetchone(self):
                return marker

        return _Cur()


def _patch(monkeypatch, state):
    pm = importlib.import_module("files_memory.promote")
    marker = {"uuid": "mk-1", "promoted_to": "mem-1", "mapped_type": "note"}

    @contextmanager
    def fake_db(path=None):
        yield _Conn(marker)

    monkeypatch.setattr(pm, "_db", fake_db)
    monkeypatch.setattr(pm, "_resolve_source", lambda conn, uuid: {"kind": "fact"})
    monkeypatch.setattr(pm, "_promoted_row_state", lambda mid: state)
    return pm


def test_erased_target_is_refused(monkeypatch):
    pm = _patch(monkeypatch, "absent")
    with pytest.raises(ValueError, match="erased by gdpr_forget"):
        pm.files_promote("src-1")


def test_soft_deleted_target_is_reported(monkeypatch):
    pm = _patch(monkeypatch, "deleted")
    out = pm.files_promote("src-1")
    assert out["already_promoted"] is True and out["promoted_row_state"] == "deleted"


def test_row_state_reads_the_memory_store(each_backend):
    import uuid

    pm = importlib.import_module("files_memory.promote")
    from memory.backends import dialect

    p = dialect().param()
    mid = str(uuid.uuid4())
    with each_backend.connection() as c:
        c.execute(
            f"INSERT INTO memory_items (id, type, title, content, is_deleted) VALUES ({p}, 'note', 't', 'c', 1)", (mid,)
        )
        c.commit()
    try:
        assert pm._promoted_row_state(mid) == "deleted"
        assert pm._promoted_row_state(str(uuid.uuid4())) == "absent"
    finally:
        with each_backend.connection() as c:
            c.execute(f"DELETE FROM memory_items WHERE id = {p}", (mid,))
            c.commit()
