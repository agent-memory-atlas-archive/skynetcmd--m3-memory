"""conftest._read_log_drained: a test's queued reads never reach the next test
(the background flusher rebuilt a later test's settings snapshot; m3 9cbed0af)."""

from __future__ import annotations

import importlib


def test_teardown_leaves_no_queued_read_for_the_flusher():
    import conftest

    db = importlib.import_module("memory.db")
    with db._read_log_lock:
        db._read_log_pending.append(("no-such-store", "m1", "agent", None, "read"))
    conftest.drain_read_log()  # what the autouse fixture runs at teardown
    assert db._read_log_pending == []
