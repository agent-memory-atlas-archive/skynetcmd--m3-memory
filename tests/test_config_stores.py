"""Named stores (CONFIG_DESIGN §14; plan 2C.3a; user decisions D1-D3, m3 dfeb41b7).

A store is (backend, locator) in m3_config.toml. Selecting one moves every
caller that resolves the backend or the SQLite path (D3: the store outranks the
legacy M3_DATABASE path), and never the chatlog (D1: process-wide).
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def stores_file(monkeypatch, tmp_path):
    """Write [stores] into the sandbox's m3_config.toml and reload settings."""
    from m3_core.config import settings as st

    cfg_root = Path(os.environ["M3_CONFIG_ROOT"])
    cfg_root.mkdir(parents=True, exist_ok=True)

    def write(text: str) -> None:
        (cfg_root / "m3_config.toml").write_text(text, encoding="utf-8")
        st._reset_for_tests()

    yield write
    st._reset_for_tests()


# ── declaration ──────────────────────────────────────────────────────────────
def test_every_selectable_backend_declares_its_locator():
    """A backend cannot be selectable without saying what locates one of its stores."""
    from m3_core.config.stores import LOCATOR_KEYS
    from m3_core.paths import SELECTABLE_BACKENDS

    assert set(LOCATOR_KEYS) == set(SELECTABLE_BACKENDS)


def _parse(text: str):
    import tomllib

    from m3_core.config.stores import parse_stores

    problems: list[tuple[str, str]] = []
    out = parse_stores(tomllib.loads(text)["stores"], lambda k, m: problems.append((k, m)))
    return out, problems


def test_valid_stores_parse(tmp_path):
    out, problems = _parse(f'[stores.scratch]\nbackend = "sqlite"\npath = "{(tmp_path / "s.db").as_posix()}"\n'
                           '[stores.bench]\nbackend = "postgres"\ndsn_secret = "M3_STORE_BENCH_PG_URL"\n')
    assert problems == []
    assert out["scratch"].path == str((tmp_path / "s.db").absolute())
    assert (out["bench"].backend, out["bench"].dsn_secret) == ("postgres", "M3_STORE_BENCH_PG_URL")


@pytest.mark.parametrize("text,key,fragment", [
    ('[stores.main]\nbackend = "sqlite"\npath = "x"\n', "stores.main", "derived from db.backend"),
    ('[stores.Bad]\nbackend = "sqlite"\npath = "x"\n', "stores.Bad", "lowercase"),
    ('[stores.a]\nbackend = "mysql"\n', "stores.a.backend", "not one of"),
    ('[stores.a]\nbackend = "postgres"\n', "stores.a.dsn_secret", "needs dsn_secret"),
    ('[stores.a]\nbackend = "sqlite"\npath = "x"\ndsn_secret = "Y"\n', "stores.a.dsn_secret", "not a sqlite store key"),
    ('[stores.a]\nbackend = "postgres"\ndsn_secret = 3\n', "stores.a.dsn_secret", "must be a string"),
])
def test_invalid_stores_are_reported(text, key, fragment):
    _, problems = _parse(text)
    assert any(k == key and fragment in m for k, m in problems), problems


def test_the_loader_reports_store_problems_with_the_rest(tmp_path):
    from m3_core.config import REGISTRY, ConfigFileError, Location, format_config_error, load

    p = tmp_path / "m3_config.toml"
    p.write_text('[db]\nbakend = 1\n[stores.a]\nbackend = "postgres"\n', encoding="utf-8")
    with pytest.raises(ConfigFileError) as exc:
        load(Location(file=p, root=tmp_path, tier="t"), REGISTRY)
    text = format_config_error(exc.value)
    assert "2 invalid entries" in text and "[db].bakend" in text and "needs dsn_secret" in text


def test_an_unknown_store_name_is_an_error_with_a_hint(stores_file, tmp_path):
    from m3_core.config.stores import StoreError, active_store

    stores_file(f'[stores.scratch]\nbackend = "sqlite"\npath = "{(tmp_path / "s.db").as_posix()}"\n')
    with pytest.raises(StoreError, match="did you mean 'scratch'"):
        with active_store("scrach"):
            pass


# ── selection (D3) and the chatlog (D1) ──────────────────────────────────────
def test_a_sqlite_store_outranks_the_legacy_path_and_is_undone_after(stores_file, tmp_path, monkeypatch):
    from m3_core.config.stores import active_store, current_store_name
    from m3_core.paths import resolve_backend_name, resolve_db_path

    legacy = str(tmp_path / "legacy.db")
    monkeypatch.setenv("M3_DATABASE", legacy)
    stores_file(f'[stores.scratch]\nbackend = "sqlite"\npath = "{(tmp_path / "s.db").as_posix()}"\n')
    with active_store("scratch"):
        assert resolve_db_path(None) == str((tmp_path / "s.db").absolute())
        assert resolve_backend_name() == "sqlite" and current_store_name() == "scratch"
    assert resolve_db_path(None) == os.path.abspath(legacy) and current_store_name() == "main"


def test_main_overrides_an_outer_store(stores_file, tmp_path):
    from m3_core.config.stores import active_store, current_store_name

    stores_file(f'[stores.scratch]\nbackend = "sqlite"\npath = "{(tmp_path / "s.db").as_posix()}"\n')
    with active_store("scratch"):
        with active_store("main"):
            assert current_store_name() == "main"
        assert current_store_name() == "scratch"


def test_a_postgres_store_changes_the_backend_name(stores_file, monkeypatch):
    from m3_core.config.stores import active_store
    from m3_core.paths import resolve_backend_name

    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    stores_file('[stores.bench]\nbackend = "postgres"\ndsn_secret = "M3_STORE_BENCH_PG_URL"\n')
    with active_store("bench"):
        assert resolve_backend_name() == "postgres"
    assert resolve_backend_name() == "sqlite"


def test_the_chatlog_does_not_follow_a_store(stores_file, tmp_path):
    """D1: the chatlog is process-wide; store= must not move it."""
    import chatlog_config
    from m3_core.config.stores import active_store

    before = chatlog_config.chatlog_db_path()
    stores_file(f'[stores.scratch]\nbackend = "sqlite"\npath = "{(tmp_path / "s.db").as_posix()}"\n')
    with active_store("scratch"):
        assert chatlog_config.chatlog_db_path() == before


def test_a_write_inside_a_sqlite_store_lands_in_its_file(stores_file, tmp_path, monkeypatch):
    """Through memory.db._db(), the path every direct caller uses, not only the seam."""
    import importlib

    from m3_core.config.stores import active_store

    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    importlib.import_module("memory.backends.selector")._reset_for_tests()
    store_db = tmp_path / "scratch.db"
    stores_file(f'[stores.scratch]\nbackend = "sqlite"\npath = "{store_db.as_posix()}"\n')
    db = importlib.import_module("memory.db")
    marker = f"store-probe-{uuid.uuid4().hex}"
    with active_store("scratch"):
        with db._db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS _store_probe (v TEXT)")
            conn.execute("INSERT INTO _store_probe (v) VALUES (?)", (marker,))
    with sqlite3.connect(store_db) as raw:
        assert raw.execute("SELECT v FROM _store_probe").fetchall() == [(marker,)]
    with db._db() as conn:  # the default store never saw it
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='_store_probe'").fetchall() == []


# ── PostgreSQL stores: the DSN is a bootstrap secret, guarded like the primary ──
def _pg_store(monkeypatch, stores_file, dsn: str | None):
    import importlib

    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    if dsn is None:
        monkeypatch.delenv("M3_STORE_BENCH_PG_URL", raising=False)
    else:
        monkeypatch.setenv("M3_STORE_BENCH_PG_URL", dsn)
    stores_file('[stores.bench]\nbackend = "postgres"\ndsn_secret = "M3_STORE_BENCH_PG_URL"\n')
    return importlib.import_module("memory.backends.selector")


def test_an_unset_store_secret_is_reported_with_the_fix(monkeypatch, stores_file):
    from m3_core.config.stores import active_store

    mb = _pg_store(monkeypatch, stores_file, None)
    mb._reset_for_tests()
    with active_store("bench"):
        with pytest.raises(RuntimeError, match=r"dsn_secret M3_STORE_BENCH_PG_URL.*keyring set system"):
            mb.active_backend()


def test_a_store_on_the_warehouse_is_refused(monkeypatch, stores_file):
    from m3_core.config.stores import active_store

    dsn = "postgresql://u:p@wh.example:5432/cdw"
    mb = _pg_store(monkeypatch, stores_file, dsn)
    monkeypatch.setenv("M3_CDW_PG_URL", dsn)
    mb._reset_for_tests()
    with active_store("bench"):
        with pytest.raises(Exception, match="(?i)warehouse"):
            mb.active_backend()


def test_two_stores_on_one_database_share_an_instance(monkeypatch, stores_file):
    from m3_core.config.stores import active_store

    dsn = "postgresql://u:p@db.example:5432/m3"
    import importlib

    mb = importlib.import_module("memory.backends.selector")
    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    monkeypatch.setenv("M3_STORE_A_PG_URL", dsn)
    monkeypatch.setenv("M3_STORE_B_PG_URL", dsn)
    monkeypatch.delenv("M3_CDW_PG_URL", raising=False)
    stores_file('[stores.a]\nbackend = "postgres"\ndsn_secret = "M3_STORE_A_PG_URL"\n'
                '[stores.b]\nbackend = "postgres"\ndsn_secret = "M3_STORE_B_PG_URL"\n')
    mb._reset_for_tests()
    with active_store("a"):
        first = mb.active_backend()
    with active_store("b"):
        second = mb.active_backend()
    assert first is second, "one database, two pools"
    mb._reset_for_tests()


# ── 3b: the per-call `store` argument (catalog.dispatch.pop_call_scope) ──────
def _path_probe(ran: list):
    """A tool whose impl reports the SQLite path it would open."""
    import mcp_tool_catalog as cat
    from m3_core.paths import resolve_db_path

    def _impl():
        ran.append(1)
        return resolve_db_path(None)

    props = {"database": {"type": "string"}, "store": {"type": "string"}}
    return cat, cat.ToolSpec(name="memory_probe", description="probe",
                             parameters={"type": "object", "properties": props}, impl=_impl)


def _scratch(stores_file, tmp_path, monkeypatch) -> str:
    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    stores_file(f'[stores.scratch]\nbackend = "sqlite"\npath = "{(tmp_path / "s.db").as_posix()}"\n')
    return str((tmp_path / "s.db").absolute())


def test_every_catalog_tool_takes_store_and_the_manifest_omits_it():
    import gen_tool_manifest
    import mcp_tool_catalog as cat

    assert all("store" in t.parameters["properties"] for t in cat.TOOLS)
    args = [a["name"] for t in gen_tool_manifest.build_manifest()["tools"] for a in t["args"]]
    assert not set(cat.ROUTING_ARGS) & set(args)


def test_dispatch_runs_the_impl_inside_the_store(stores_file, tmp_path, monkeypatch):
    import asyncio

    path = _scratch(stores_file, tmp_path, monkeypatch)
    ran: list = []
    cat, spec = _path_probe(ran)
    assert asyncio.run(cat.execute_tool(spec, {"store": " scratch "}, agent_id="")) == path
    assert asyncio.run(cat.execute_tool_structured(spec, {"store": "scratch"}, agent_id="")) == path
    assert asyncio.run(cat.execute_tool(spec, {"store": "main"}, agent_id="")) != path
    assert asyncio.run(cat.execute_tool(spec, {"store": ""}, agent_id="")) != path


@pytest.mark.parametrize("kind", ["sync", "async", "offloaded"])
def test_every_bridge_wrapper_runs_inside_the_store(stores_file, tmp_path, monkeypatch, kind):
    """`offloaded` is the MCP path for a sync impl: the scope is entered in a worker thread."""
    import asyncio

    import memory_bridge

    path = _scratch(stores_file, tmp_path, monkeypatch)
    ran: list = []
    cat, spec = _path_probe(ran)
    if kind == "async":
        sync_impl = spec.impl

        async def _impl():
            return sync_impl()

        import dataclasses

        spec = dataclasses.replace(spec, impl=_impl, is_async=True)
    fn = memory_bridge._build_typed_function(spec, for_mcp=kind == "offloaded")
    out = fn(store="scratch", database="")
    if asyncio.iscoroutine(out):
        out = asyncio.run(out)
    assert out == path


@pytest.mark.parametrize("args,fragment", [
    ({"store": "scrach"}, "did you mean 'scratch'"),
    ({"store": "scratch", "database": "x.db"}, "both select the store"),
    ({"store": 3}, "is a store name"),
])
def test_a_bad_store_argument_is_refused_before_the_impl(stores_file, tmp_path, monkeypatch, args, fragment):
    import asyncio

    _scratch(stores_file, tmp_path, monkeypatch)
    ran: list = []
    cat, spec = _path_probe(ran)
    assert fragment in asyncio.run(cat.execute_tool(spec, dict(args), agent_id=""))
    with pytest.raises(ValueError, match=fragment):  # dry_run validates it too
        asyncio.run(cat.execute_tool_structured(spec, dict(args), agent_id="", dry_run=True))
    assert ran == []


def test_files_tools_refuse_store_until_they_follow_it(stores_file, tmp_path, monkeypatch):
    import mcp_tool_catalog as cat

    _scratch(stores_file, tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="files tools resolve their own store"):
        cat.pop_call_scope({"store": "scratch"}, "files_stats")


def test_the_cli_passes_store_to_the_dispatcher(stores_file, tmp_path, monkeypatch):
    """`m3 <domain> <tool> --store`: the dispatcher's refusal proves the flag arrived."""
    import subprocess
    import sys

    _scratch(stores_file, tmp_path, monkeypatch)
    repo = Path(__file__).resolve().parent.parent
    run = subprocess.run([sys.executable, "-m", "m3_memory.cli", "memory", "memory_search",
                          "--query", "x", "--store", "scrach"],
                         capture_output=True, text=True, cwd=str(repo))
    assert "did you mean 'scratch'" in run.stdout + run.stderr, run.stdout + run.stderr
