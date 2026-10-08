"""`database` is refused wherever it would be silently ignored (plan U25).

The universal `database` argument names a SQLite file. Before this guard it was
dropped on the floor in two places, and the call ran against the default store:
on a non-SQLite backend (PostgreSQL addresses its one pooled DSN), and on every
files tool on every backend (files_memory resolves its own store; m3
`e03425ec`). `catalog.dispatch._pop_database` is the one owner of the refusal;
every surface (catalog dispatch, m3_call, the CLI, the MCP bridge) calls it.

The backend is selected by env alone here: the refusal happens before any
connection, so the PostgreSQL cases need no server.
"""
from __future__ import annotations

import asyncio

import pytest


def _set_backend(monkeypatch, name: str) -> None:
    monkeypatch.setenv("M3_DB_BACKEND", name)
    monkeypatch.delenv("DB_BACKEND", raising=False)


def test_sqlite_memory_tool_keeps_the_path(monkeypatch, tmp_path):
    import mcp_tool_catalog as cat

    _set_backend(monkeypatch, "sqlite")
    args = {"query": "q", "database": f"  {tmp_path / 'a.db'}  "}
    assert cat._pop_database(args, "memory_search") == str(tmp_path / "a.db")
    assert "database" not in args


@pytest.mark.parametrize("value", [None, "", "   "])
def test_absent_database_is_never_refused(monkeypatch, value):
    """Empty means absent, so the default store is what the caller asked for."""
    import mcp_tool_catalog as cat

    _set_backend(monkeypatch, "postgres")
    args = {} if value is None else {"database": value}
    assert cat._pop_database(args, "files_stats") is None


def test_postgres_refuses_database(monkeypatch, tmp_path):
    import mcp_tool_catalog as cat

    _set_backend(monkeypatch, "postgres")
    with pytest.raises(ValueError, match=r"memory_search: `database` would be ignored") as e:
        cat._pop_database({"database": str(tmp_path / "a.db")}, "memory_search")
    assert "M3_DB_BACKEND=postgres" in str(e.value) and "--store" in str(e.value)


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_files_tools_refuse_database_on_every_backend(monkeypatch, tmp_path, backend):
    import mcp_tool_catalog as cat

    _set_backend(monkeypatch, backend)
    with pytest.raises(ValueError, match="files tools resolve their own store"):
        cat._pop_database({"database": str(tmp_path / "f.db")}, "files_stats")


def test_execute_tool_reports_the_refusal_without_running(monkeypatch, tmp_path):
    """The impl never runs: a refusal that ran the call anyway is no refusal."""
    import mcp_tool_catalog as cat

    ran = []
    spec = cat.ToolSpec(
        name="memory_probe", description="probe",
        parameters={"type": "object", "properties": {"database": {"type": "string"}}},
        impl=lambda: ran.append(1) or "ran",
    )
    _set_backend(monkeypatch, "postgres")
    out = asyncio.run(cat.execute_tool(spec, {"database": str(tmp_path / "a.db")}, agent_id=""))
    assert out.startswith("Error: ValueError: memory_probe: `database` would be ignored"), out
    with pytest.raises(ValueError, match="would be ignored"):
        asyncio.run(cat.execute_tool_structured(
            spec, {"database": str(tmp_path / "a.db")}, agent_id=""))
    assert ran == []


@pytest.mark.parametrize("is_async", [False, True])
def test_bridge_wrapper_reports_the_refusal_without_running(monkeypatch, tmp_path, is_async):
    import mcp_tool_catalog as cat
    import memory_bridge

    ran = []

    def _sync_impl():
        ran.append(1)
        return "ran"

    async def _async_impl():
        ran.append(1)
        return "ran"

    spec = cat.ToolSpec(
        name="memory_probe", description="probe",
        parameters={"type": "object", "properties": {"database": {"type": "string"}}},
        impl=_async_impl if is_async else _sync_impl, is_async=is_async,
    )
    _set_backend(monkeypatch, "postgres")
    fn = memory_bridge._build_typed_function(spec)
    out = fn(database=str(tmp_path / "a.db"))
    if is_async:
        out = asyncio.run(out)
    assert out.startswith("Error: memory_probe: `database` would be ignored"), out
    assert ran == []
