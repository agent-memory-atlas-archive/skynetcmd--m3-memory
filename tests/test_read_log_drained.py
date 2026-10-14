"""conftest._read_log_drained: a test's queued reads never reach the next test
(the background flusher rebuilt a later test's settings snapshot)."""

from __future__ import annotations

import importlib


def test_teardown_leaves_no_queued_read_for_the_flusher(tmp_path):
    import conftest

    db = importlib.import_module("memory.db")
    # Under tmp_path: the drain FLUSHES first, and opening a relative store name
    # created it in the working directory (the repo tree on a gate).
    store = str(tmp_path / "no-such-store.db")
    with db._read_log_lock:
        db._read_log_pending.append((store, "m1", "agent", None, "read"))
    conftest.drain_read_log()  # what the autouse fixture runs at teardown
    assert db._read_log_pending == []
