"""In-process unit tests for mcp_proxy: catalog gating and dispatch.

Verifies that the proxy imports cleanly, serves only m3's own catalog, hides
destructive catalog tools by default and exposes them when allowed, and that
_execute_tool refuses unknown and disabled tools with a helpful message.

This file lived in bin/ and was run by hand only, so it rotted: it asserted the
deleted protocol and debug bridges' 5 and 6 tools long after they were gone.
It now runs with the suite. The gate is set on the proxy module directly rather
than through the environment: what is tested here is the proxy's gating, and
`destructive_tools_allowed` (the env reading) has its own owner in m3_sdk.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

_DESTRUCTIVE = {
    "memory_delete",
    "memory_maintenance",
    "memory_set_retention",
    "memory_export",
    "memory_import",
    "gdpr_export",
    "gdpr_forget",
    "agent_offline",
}


def _proxy(monkeypatch, *, allow: bool):
    mp = importlib.import_module("mcp_proxy")
    monkeypatch.setattr(mp, "ALLOW_DESTRUCTIVE", allow)
    # A fresh, empty catalog cache, so the build below reflects `allow`.
    monkeypatch.setattr(mp, "_catalog_built", False)
    monkeypatch.setattr(mp, "_catalog_tool_schemas", [])
    monkeypatch.setattr(mp, "_catalog_specs_by_name", {})
    return mp


@pytest.fixture
def proxy(monkeypatch):
    return _proxy(monkeypatch, allow=False)


@pytest.fixture
def proxy_allowing_destructive(monkeypatch):
    return _proxy(monkeypatch, allow=True)


def _names(schemas) -> set[str]:
    return {t["function"]["name"] for t in schemas}


def test_imports_clean(proxy):
    assert hasattr(proxy, "PROXY_HOST")
    assert hasattr(proxy, "LMSTUDIO_BASE")
    assert proxy.PROXY_PORT == 9000


def test_the_deleted_bridges_left_no_tools_behind(proxy):
    """The protocol and debug bridges were deleted; the proxy serves only m3's catalog."""
    assert proxy.PROTOCOL_TOOLS == []
    assert proxy.DEBUG_TOOLS == []


def test_catalog_default_filters_destructive(proxy):
    leaked = _names(proxy._build_catalog_tools()[0]) & _DESTRUCTIVE
    assert leaked == set(), f"Destructive tools leaked into default mode: {leaked}"


def test_total_tool_count_default(proxy):
    catalog_schemas, _ = proxy._build_catalog_tools()
    total = len(proxy.PROTOCOL_TOOLS) + len(proxy.DEBUG_TOOLS) + len(catalog_schemas)
    assert total == len(proxy.get_mcp_tools())
    assert total >= 40


def test_execute_unknown_tool(proxy):
    assert "Unknown MCP tool" in asyncio.run(proxy._execute_tool("nope_not_real", {}, "tester"))


def test_execute_destructive_tool_default_denied(proxy):
    result = asyncio.run(proxy._execute_tool("memory_delete", {"id": "x"}, "tester"))
    assert "destructive and disabled by default" in result
    assert "M3_MCP_PROXY_ALLOW_DESTRUCTIVE" in result, "the hint must name the current variable"


def test_execute_catalog_tool_agent_list(proxy):
    result = asyncio.run(proxy._execute_tool("agent_list", {}, "test-smoke"))
    assert isinstance(result, str)
    assert result.startswith("Agents (") or "agents" in result.lower(), result[:200]


def test_inject_agent_id_on_memory_write():
    cat = importlib.import_module("mcp_tool_catalog")
    spec = cat.get_tool("memory_write")
    assert spec is not None
    assert spec.inject_agent_id, "memory_write must have inject_agent_id=True"


def test_legacy_dispatch_table_complete(proxy):
    legacy = _names(proxy.PROTOCOL_TOOLS) | _names(proxy.DEBUG_TOOLS)
    missing = legacy - set(proxy._LEGACY_DISPATCH.keys())
    assert missing == set(), f"Legacy tools missing from _LEGACY_DISPATCH: {missing}"


def test_destructive_exposed(proxy_allowing_destructive):
    present = _names(proxy_allowing_destructive._build_catalog_tools()[0]) & _DESTRUCTIVE
    assert present == _DESTRUCTIVE, f"Missing destructive tools when enabled: {_DESTRUCTIVE - present}"


def test_full_catalog_count(proxy_allowing_destructive):
    """With destructive enabled the proxy exposes every MCP tool (CLI-only commands
    are never MCP tools). Derived from mcp_tool_catalog.MCP_TOOLS, not a literal:
    a hand-edited count drifts (63 -> 110)."""
    tools = importlib.import_module("mcp_tool_catalog").MCP_TOOLS
    assert len(proxy_allowing_destructive._build_catalog_tools()[0]) == len(tools)


def test_catalog_schemas_is_the_one_builder(proxy):
    """The proxy's cached build and the uncached builder agree, so
    measure_tool_tokens (which uses the uncached one) measures what is served."""
    assert proxy._build_catalog_tools()[0] == proxy.catalog_schemas(include_destructive=False)[0]
    assert _names(proxy.catalog_schemas(include_destructive=True)[0]) >= _DESTRUCTIVE


# ── Error mapping: each failure names its backend and cause ──────────────────


def _status_error(status: int, url: str = "https://api.x.ai/v1/chat/completions?key=SECRET"):
    import httpx

    req = httpx.Request("POST", url)
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(status, request=req, text="denied"))


def _map(mp, exc):
    import json as _json

    r = mp._backend_error_response(exc, model="grok-3", btype="openai_compat", base_url=mp.GROK_BASE, agent_id="a")
    return r.status_code, _json.loads(r.body)["error"]


def test_upstream_401_is_a_502_naming_the_backend_key_not_the_proxy_key(proxy):
    status, err = _map(proxy, _status_error(401))
    assert status == 502
    assert "XAI_API_KEY" in err["message"] and "not your MCP_PROXY_KEY" in err["message"]


def test_backend_json_error_is_not_reported_as_configuration(proxy):
    import json as _json

    status, err = _map(proxy, _json.JSONDecodeError("Expecting value", "<html>", 0))
    assert status == 502 and err["type"] == "backend_error"


def test_config_error_keeps_its_own_message(proxy):
    status, err = _map(proxy, proxy.BackendConfigError("ANTHROPIC_API_KEY is required"))
    assert status == 500 and "ANTHROPIC_API_KEY" in err["message"]


def test_any_timeout_is_a_504_naming_the_settings(proxy):
    import httpx

    status, err = _map(proxy, httpx.ConnectTimeout("slow"))
    assert status == 504 and "llm.read_timeout_s" in err["message"]


def test_transport_errors_name_the_backend_url(proxy):
    import httpx

    status, err = _map(proxy, httpx.ConnectError("[Errno 11001] getaddrinfo failed"))
    assert status == 503 and "api.x.ai" in err["message"] and "getaddrinfo" in err["message"]


def test_unexpected_errors_carry_their_message(proxy):
    status, err = _map(proxy, RuntimeError("the actual cause"))
    assert status == 500 and "the actual cause" in err["message"]


class _Req:
    def __init__(self, auth=None):
        self.headers = {"Authorization": auth} if auth is not None else {}


def test_missing_header_and_wrong_key_are_worded_differently(proxy, monkeypatch):
    monkeypatch.setattr(proxy, "_get_proxy_key", lambda: "the-real-key")
    assert "no Authorization header" in proxy._auth_failure(_Req())
    assert "mismatch" in proxy._auth_failure(_Req("Bearer nope"))
    assert proxy._auth_failure(_Req("Bearer the-real-key")) is None


def test_no_key_warns_once_not_per_request(proxy, monkeypatch, caplog):
    monkeypatch.setattr(proxy, "_get_proxy_key", lambda: "")
    monkeypatch.setattr(proxy, "_no_key_warned", False)
    with caplog.at_level("WARNING", logger="mcp_proxy"):
        for _ in range(3):
            assert proxy._auth_failure(_Req()) is None
    assert sum("MCP_PROXY_KEY is not set" in r.getMessage() for r in caplog.records) == 1


def test_non_loopback_bind_without_a_key_is_refused(proxy, monkeypatch):
    monkeypatch.setattr(proxy, "_get_proxy_key", lambda: "")
    assert proxy._refuse_unauthenticated_bind("0.0.0.0")
    assert proxy._refuse_unauthenticated_bind("127.0.0.1") is None
    monkeypatch.setattr(proxy, "_get_proxy_key", lambda: "k")
    assert proxy._refuse_unauthenticated_bind("0.0.0.0") is None


def test_catalog_failure_is_degraded_and_retried(proxy, monkeypatch):
    import sys as _sys

    monkeypatch.setattr(proxy, "_catalog_error", "")
    monkeypatch.setattr(proxy, "_catalog_retry_at", 0.0)
    monkeypatch.setitem(_sys.modules, "mcp_tool_catalog", None)  # import -> ImportError
    proxy._build_catalog_tools()
    assert proxy._catalog_error and not proxy._catalog_built
    r = asyncio.run(proxy.health())
    assert r.status_code == 503
    monkeypatch.delitem(_sys.modules, "mcp_tool_catalog")
    monkeypatch.setattr(proxy, "_catalog_retry_at", 0.0)
    proxy._build_catalog_tools()
    assert proxy._catalog_built and not proxy._catalog_error


def test_malformed_tool_arguments_are_returned_as_a_tool_error_not_run(proxy, monkeypatch):
    calls = []

    async def _never(name, args, agent_id="x"):
        calls.append((name, args))
        return "ran"

    replies = [
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{"id": "t1", "function": {"name": "memory_search", "arguments": "{not json"}}],
                    },
                }
            ]
        },
        {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}]},
    ]
    seen = []

    async def _call(base, key, messages, model, tools, max_tokens):
        seen.append(list(messages))
        return replies.pop(0)

    monkeypatch.setattr(proxy, "_execute_tool", _never)
    monkeypatch.setattr(proxy, "_call_openai_compat", _call)
    asyncio.run(proxy._run_with_tools([{"role": "user", "content": "hi"}], "local-model", [], 64, skip_mcp=True))
    assert calls == []
    tool_msg = seen[1][-1]
    assert tool_msg["role"] == "tool" and "was not run" in tool_msg["content"]


def test_max_tool_rounds_is_visible_to_the_client(proxy, monkeypatch):
    loop = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "tool_calls": [{"id": "t", "function": {"name": "x", "arguments": "{}"}}],
                },
            }
        ]
    }

    async def _call(*a, **k):
        return {"choices": [dict(loop["choices"][0])]}

    async def _tool(*a, **k):
        return "ok"

    monkeypatch.setattr(proxy, "MAX_TOOL_ROUNDS", 2)
    monkeypatch.setattr(proxy, "_call_openai_compat", _call)
    monkeypatch.setattr(proxy, "_execute_tool", _tool)
    out = asyncio.run(proxy._run_with_tools([{"role": "user", "content": "hi"}], "local-model", [], 64, skip_mcp=True))
    assert "MAX_TOOL_ROUNDS" in out["m3_warning"]
