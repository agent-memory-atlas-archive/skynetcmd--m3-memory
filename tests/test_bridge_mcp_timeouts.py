"""The MCP bridge enforces `timeout` like m3_call and the CLI.

The bridge's wrappers popped `timeout` and ran the impl unbounded, so the
argument every tool schema advertises was ignored over MCP only. They now go
through catalog.dispatch._run_impl_bounded, the path execute_tool uses.
"""

from __future__ import annotations

import asyncio
import time

import pytest


async def _slow_async():
    await asyncio.sleep(0.5)
    return "done"


def _slow_sync():
    time.sleep(0.5)
    return "done"


def _mcp_fn(impl, is_async, timeout_s=None):
    import mcp_tool_catalog as cat
    import memory_bridge

    spec = cat.ToolSpec(
        name="memory_probe",
        description="probe",
        parameters={"type": "object", "properties": {"timeout": {"type": "number"}}},
        impl=impl,
        is_async=is_async,
        timeout_s=timeout_s,
    )
    return memory_bridge._build_typed_function(spec, for_mcp=True)


@pytest.mark.parametrize("impl,is_async", [(_slow_async, True), (_slow_sync, False)], ids=["async-impl", "sync-impl"])
def test_a_per_call_timeout_bounds_an_mcp_call(impl, is_async):
    out = asyncio.run(_mcp_fn(impl, is_async)(timeout=0.1))
    assert out.startswith("Error: ToolTimeout: memory_probe exceeded 0.1s"), out


@pytest.mark.parametrize("impl,is_async", [(_slow_async, True), (_slow_sync, False)], ids=["async-impl", "sync-impl"])
def test_timeout_zero_disables_the_bound(impl, is_async):
    assert asyncio.run(_mcp_fn(impl, is_async)(timeout=0)) == "done"


def test_the_tools_own_budget_applies_when_no_timeout_is_given(monkeypatch):
    monkeypatch.delenv("M3_TOOL_TIMEOUT", raising=False)
    tight = asyncio.run(_mcp_fn(_slow_async, True, timeout_s=0.1)())
    roomy = asyncio.run(_mcp_fn(_slow_async, True, timeout_s=5)())
    assert tight.startswith("Error: ToolTimeout") and roomy == "done"
