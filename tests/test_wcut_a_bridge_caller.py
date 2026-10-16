"""An MCP tool call binds a caller for the read-access audit log: the caller's
claimed agent_id, or "mcp-unauthenticated" -- never "direct", which means the
read did not come through a tool at all."""

import asyncio


def _spec(params):
    import mcp_tool_catalog as cat
    from m3_core.caller import current_caller

    def impl(**kw):
        return current_caller()

    return cat.ToolSpec(
        name="_probe_caller",
        description="probe",
        parameters={"type": "object", "properties": params, "required": []},
        impl=impl,
        is_async=False,
    )


def test_mcp_call_without_agent_id_is_unauthenticated_not_direct():
    import memory_bridge

    fn = memory_bridge._build_typed_function(_spec({"query": {"type": "string"}}), for_mcp=True)
    assert asyncio.run(fn(query="x")) == "mcp-unauthenticated"


def test_mcp_call_records_the_claimed_agent_id():
    import memory_bridge

    fn = memory_bridge._build_typed_function(_spec({"agent_id": {"type": "string", "default": ""}}), for_mcp=True)
    assert asyncio.run(fn(agent_id="claude-x")) == "claude-x"
