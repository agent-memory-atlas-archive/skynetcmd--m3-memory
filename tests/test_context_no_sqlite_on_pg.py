"""Building an M3Context off SQLite must not create a SQLite store.

Hazard: memory_core binds a default context at import; an eager SQLite pool
would create an empty agent_memory.db in the engine root of a PostgreSQL
install.
"""

from __future__ import annotations

import os
import sys

_BIN = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from m3_core.context import M3Context, _close_context_pool  # noqa: E402

from conftest import select_backend  # noqa: E402


def test_no_store_file_until_sqlite_is_used(monkeypatch, tmp_path):
    db = tmp_path / "agent_memory.db"
    select_backend("postgres")
    ctx = M3Context(str(db))
    try:
        assert not db.exists()
        # Asking for the SQLite pool on another backend refuses (2026-10-10): the
        # file is not the live store, and every caller is gated.
        import pytest

        with pytest.raises(RuntimeError, match=r"observed: db.backend='postgres'"):
            with ctx.get_sqlite_conn():
                pass
        assert not db.exists(), "a refused request must not create the store either"
    finally:
        _close_context_pool(ctx)


def test_sqlite_backend_still_opens_eagerly(monkeypatch, tmp_path):
    db = tmp_path / "agent_memory.db"
    ctx = M3Context(str(db))
    try:
        assert ctx._pool is not None
    finally:
        _close_context_pool(ctx)
