"""gather_or_cancel: THE fan-out primitive for awaitables that must all succeed.

Plain ``asyncio.gather(*aws)`` re-raises the first failure and leaves every
other awaitable RUNNING, unobserved: a failed bulk embed kept posting its other
chunks, a failed write batch kept writing. This cancels and awaits the
siblings first, then re-raises the ORIGINAL exception unchanged, so callers'
``except SomeError`` clauses keep working.

Why not ``asyncio.TaskGroup``: it raises ``ExceptionGroup`` even for a single
failure, and callers up the stack catch specific exception types, so every
chain would need ``except*`` (user decision 2026-10-08, m3 74995ad5).

Use ``asyncio.gather(..., return_exceptions=True)`` instead when partial
failure is an expected result rather than an error.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any


async def gather_or_cancel(*aws: Awaitable[Any]) -> list[Any]:
    """``asyncio.gather(*aws)``, but a failure cancels the others before it propagates.

    Results come back in argument order. Cancelling the caller cancels every
    child (as gather does). Tasks are created in the caller's context, so
    ContextVars such as the active database propagate.
    """
    tasks = [asyncio.ensure_future(a) for a in aws]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            if not t.done():
                t.cancel()
        # Wait for the cancellations to land so nothing outlives this call.
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
