"""The FDW fast path "never raises": an import failure must fall back, not crash.

`_try_pg_fdw_fastpath` imported pg_fdw_sync INSIDE the try whose except clause
names `pg_fdw_sync.FdwUnavailable`. When the import (or the m3_sdk import beside
it) failed, evaluating that except clause raised UnboundLocalError, which
escaped the function and hid the real import error. Found by mypy
`possibly-undefined` (plan 2D.M).
"""

from __future__ import annotations

import importlib
import sys
import types


def test_an_import_failure_falls_back_to_the_generic_bridge(monkeypatch):
    sa = importlib.import_module("sync_all")
    mb = importlib.import_module("memory.backends")
    monkeypatch.setattr(mb, "active_backend", lambda: types.SimpleNamespace(name="postgres"))
    monkeypatch.setitem(sys.modules, "pg_fdw_sync", None)  # makes `import pg_fdw_sync` raise
    reasons: list[str] = []
    monkeypatch.setattr(sa, "_set_fdw_reason", reasons.append)

    assert sa._try_pg_fdw_fastpath(dry_run=True) is None
    assert reasons and "pg_fdw_sync" in reasons[0], reasons
