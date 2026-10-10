"""m3_core.aio.gather_or_cancel: gather() that does not orphan the siblings.

Plain `asyncio.gather` (without return_exceptions) re-raises the first failure
and leaves every other awaitable RUNNING in the background, unobserved: a
failed bulk embed kept posting its other chunks, a failed write batch kept
writing. TaskGroup would cancel them but raises ExceptionGroup even for one
failure, which every caller up the stack catching a specific type would miss
(user decision 2026-10-08, m3 74995ad5). gather_or_cancel cancels and awaits
the siblings, then re-raises the ORIGINAL exception unchanged.
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest


def test_results_in_argument_order():
    from m3_core.aio import gather_or_cancel

    async def val(v, delay):
        await asyncio.sleep(delay)
        return v

    assert asyncio.run(gather_or_cancel(val("a", 0.02), val("b", 0), val("c", 0.01))) == ["a", "b", "c"]


def test_failure_cancels_siblings_and_reraises_the_original():
    from m3_core.aio import gather_or_cancel

    state = {"sibling": "running"}

    async def slow():
        try:
            await asyncio.sleep(10)
            state["sibling"] = "finished"
        except asyncio.CancelledError:
            state["sibling"] = "cancelled"
            raise

    async def boom():
        await asyncio.sleep(0.01)
        raise KeyError("the original")

    async def main():
        with pytest.raises(KeyError, match="the original"):
            await gather_or_cancel(slow(), boom())
        return state["sibling"]  # read BEFORE the loop closes: no orphan left behind

    assert asyncio.run(main()) == "cancelled"


def test_outer_cancellation_cancels_the_children():
    from m3_core.aio import gather_or_cancel

    seen = []

    async def child():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            seen.append("child cancelled")
            raise

    async def main():
        t = asyncio.create_task(gather_or_cancel(child(), child()))
        await asyncio.sleep(0.01)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    asyncio.run(main())
    assert seen == ["child cancelled", "child cancelled"]


def test_children_see_the_callers_context():
    from m3_core.aio import gather_or_cancel

    var = contextvars.ContextVar("v", default="unset")

    async def read():
        return var.get()

    async def main():
        var.set("caller")
        return await gather_or_cancel(read(), read())

    assert asyncio.run(main()) == ["caller", "caller"]


def test_no_plain_gather_without_return_exceptions():
    """New fan-outs use gather_or_cancel; the allowlisted gather drains by design."""
    import ast
    import pathlib

    allowed = {
        # Producer/consumer record their own failures and never raise; the
        # consumer must keep draining after the producer stops.
        ("bin/m3_enrich_batch.py", "_run_async"),
    }
    root = pathlib.Path(__file__).resolve().parents[1]
    found = set()
    for top in ("bin", "m3_memory"):
        for p in (root / top).rglob("*.py"):
            rel = p.relative_to(root).as_posix()
            if p.name.startswith(("bench_", "benchmark_")) or "__pycache__" in p.parts or rel == "bin/m3_core/aio.py":
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for n in ast.walk(fn):
                    if (
                        isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "gather"
                        and not any(
                            k.arg == "return_exceptions" and isinstance(k.value, ast.Constant) and k.value.value is True
                            for k in n.keywords
                        )
                    ):
                        found.add((rel, fn.name))
    assert found == allowed, f"use gather_or_cancel: {sorted(found - allowed)}; stale: {sorted(allowed - found)}"
