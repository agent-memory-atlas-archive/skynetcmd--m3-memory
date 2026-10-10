"""Sync code that must run a coroutine keeps the caller's context (ContextVars).

files_memory.promote ran memory_write_impl through `asyncio.run` on a worker
thread whenever a loop was already running on the calling thread (the dispatcher
runs sync files tools inline on its loop). A new thread starts with an EMPTY
context, so the per-call store (`store=`) and the legacy `active_database` were
lost there: the promoted memory went to the default store's core while its
files marker went to the selected store. m3_core.aio.run_blocking is the one
bridge and copies the caller's context into the worker.
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import os
from pathlib import Path

import pytest

_PROBE: contextvars.ContextVar[str] = contextvars.ContextVar("probe", default="unset")


@pytest.mark.parametrize("inside_loop", [False, True], ids=["no-loop", "loop-running"])
def test_run_blocking_keeps_the_callers_context(inside_loop):
    from m3_core.aio import run_blocking

    async def read():
        return _PROBE.get()

    def sync_caller():
        return run_blocking(read)

    token = _PROBE.set("caller")
    try:
        if inside_loop:

            async def outer():
                return sync_caller()  # sync code called ON the loop thread

            got = asyncio.run(outer())
        else:
            got = sync_caller()
    finally:
        _PROBE.reset(token)
    assert got == "caller"


def test_run_blocking_reraises_the_coroutines_exception():
    from m3_core.aio import run_blocking

    async def boom():
        raise KeyError("x")

    async def outer():
        return run_blocking(boom)

    with pytest.raises(KeyError):
        asyncio.run(outer())


def test_no_hand_rolled_sync_to_async_bridge_outside_the_owner():
    """A worker thread running asyncio.run without copying the context is the
    defect run_blocking owns; a new copy would lose the store again."""
    import ast

    root = Path(__file__).resolve().parent.parent
    offenders = []
    for base in ("bin", "m3_memory"):
        for f in (root / base).rglob("*.py"):
            if f.name == "aio.py" and f.parent.name == "m3_core":
                continue
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                # <executor>.submit(<lambda or func whose body calls asyncio.run>)
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "submit"
                    and node.args
                    and any(
                        isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "run"
                        and isinstance(n.func.value, ast.Name)
                        and n.func.value.id == "asyncio"
                        for n in ast.walk(node.args[0])
                    )
                ):
                    offenders.append(f"{f.relative_to(root)}:{node.lineno}")
    assert not offenders, "use m3_core.aio.run_blocking:\n" + "\n".join(offenders)


# A bare asyncio.gather leaves the siblings of a failed awaitable running,
# unobserved; m3_core.aio.gather_or_cancel owns all-must-succeed fan-out. Bare
# gather is allowed only here, each with its reason.
_BARE_GATHER_ALLOWED = {
    # producer + consumer each record their own failure and never raise, and the
    # consumer must keep draining after the producer stops (comment at the site).
    "bin/m3_enrich_batch.py",
}


def test_every_bare_gather_is_the_owner_or_justified():
    import ast

    root = Path(__file__).resolve().parent.parent
    offenders = []
    for base in ("bin", "m3_memory"):
        for f in (root / base).rglob("*.py"):
            rel = f.relative_to(root).as_posix()
            if rel == "bin/m3_core/aio.py" or rel in _BARE_GATHER_ALLOWED:
                continue
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "gather"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "asyncio"
                    and not any(k.arg == "return_exceptions" for k in node.keywords)
                ):
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "all must succeed: m3_core.aio.gather_or_cancel; partial failure "
        "expected: return_exceptions=True:\n" + "\n".join(offenders)
    )


def test_timeouts_use_asyncio_timeout():
    """`async with asyncio.timeout()` (3.11) is the one timeout form: wait_for is
    implemented on it in 3.12, and two spellings of one thing drift."""
    import ast

    root = Path(__file__).resolve().parent.parent
    offenders = []
    for base in ("bin", "m3_memory"):
        for f in (root / base).rglob("*.py"):
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            offenders += [
                f"{f.relative_to(root)}:{n.lineno}"
                for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "wait_for"
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "asyncio"
            ]
    assert not offenders, "use `async with asyncio.timeout(...)`:\n" + "\n".join(offenders)


def test_files_promotion_inside_a_loop_writes_to_the_selected_store(monkeypatch, tmp_path):
    """The real path: _write_to_memory_db called on a running loop, store selected."""
    from m3_core.config import settings as st
    from m3_core.config.stores import active_store
    from m3_core.paths import resolve_db_path

    cfg = Path(os.environ["M3_CONFIG_ROOT"])
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "m3_config.toml").write_text(
        f'[stores.scratch]\nbackend = "sqlite"\npath = "{(tmp_path / "s.db").as_posix()}"\n', encoding="utf-8"
    )
    st._reset_for_tests()
    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")

    seen: list[str] = []

    async def fake_write(**_kw):
        seen.append(resolve_db_path(None))
        return "Created: 00000000-0000-0000-0000-000000000001"

    wr = importlib.import_module("memory.write")
    monkeypatch.setattr(wr, "memory_write_impl", fake_write)
    promote = importlib.import_module("files_memory.promote")

    async def outer():
        with active_store("scratch"):
            return promote._write_to_memory_db("c", "t", "note", "agent", 0.5, "a", {})

    try:
        assert asyncio.run(outer()) == "00000000-0000-0000-0000-000000000001"
        assert seen == [str((tmp_path / "s.db").absolute())], "the promotion lost the store"
    finally:
        st._reset_for_tests()
