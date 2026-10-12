"""Chatlog work run off the event loop must see the caller's active_database.

The chatlog impls run their blocking query bodies in a worker thread, and that
body opens its connection with `M3Context.get_chatlog_conn()`, which resolves the
SQLite chatlog store through `chatlog_config.chatlog_db_path()` -- and that reads
the `active_database` ContextVar. `loop.run_in_executor` does NOT copy the
caller's context into the thread, so the override vanished there and the query
ran against the default store: a `database=` argument on a chatlog tool was
silently ignored. `asyncio.to_thread` copies the context (plan item R5).

Measured by recording the ContextVar value the thread sees when it opens the
connection, then stopping, so no store is touched.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from conftest import use_backend


class _Stop(Exception):
    pass


@pytest.fixture
def seen(monkeypatch):
    from m3_core import paths
    from m3_sdk import M3Context

    out: list = []

    @contextlib.contextmanager
    def _record(self):
        out.append(paths._active_db.get())
        raise _Stop
        yield  # pragma: no cover

    monkeypatch.setattr(M3Context, "get_chatlog_conn", _record)
    use_backend("sqlite")
    return out


async def _search():
    import chatlog_core

    return await chatlog_core._chatlog_search_separate("q", 5, "", "", "", "", "", "", "", db_path="unused")


async def _cost_report():
    import chatlog_core

    return await chatlog_core.chatlog_cost_report_impl()


async def _promote():
    import chatlog_core

    return await chatlog_core.chatlog_promote_impl(ids=["00000000-0000-0000-0000-000000000000"])


@pytest.mark.parametrize("call", [_search, _cost_report, _promote], ids=["search", "cost_report", "promote"])
def test_worker_thread_sees_the_callers_database(seen, tmp_path, call):
    from m3_sdk import active_database

    target = tmp_path / "chosen.db"

    async def _go():
        with active_database(str(target)) as resolved:
            with contextlib.suppress(_Stop):
                await call()
            return resolved

    resolved = asyncio.run(_go())
    assert seen, "the chatlog connection was never opened; the test exercised nothing"
    assert seen[-1] == resolved, f"worker thread saw active_database={seen[-1]!r}, caller set {resolved!r}"
