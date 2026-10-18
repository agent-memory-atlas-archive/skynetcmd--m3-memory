"""Named stores.

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
from m3_core.paths import pin_database

from conftest import use_backend


@pytest.fixture
def stores_file(monkeypatch, tmp_path):
    """Write [stores] into the sandbox's m3_config.toml and reload settings. The
    sandbox's own content ([paths], which keeps every root under tmp_path) is
    kept: replacing it would send the roots to the developer's ~/.m3."""
    from m3_core.config import settings as st

    cfg_root = Path(os.environ["M3_CONFIG_ROOT"])
    cfg_root.mkdir(parents=True, exist_ok=True)
    cfg = cfg_root / "m3_config.toml"
    base = cfg.read_text(encoding="utf-8") if cfg.exists() else ""

    def write(text: str) -> None:
        cfg.write_text(base.rstrip("\n") + "\n" + text, encoding="utf-8")
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
    out, problems = _parse(
        f'[stores.scratch]\nbackend = "sqlite"\npath = "{(tmp_path / "s.db").as_posix()}"\n'
        '[stores.bench]\nbackend = "postgres"\ndsn_secret = "M3_STORE_BENCH_PG_URL"\n'
    )
    assert problems == []
    assert out["scratch"].path == str((tmp_path / "s.db").absolute())
    assert (out["bench"].backend, out["bench"].dsn_secret) == ("postgres", "M3_STORE_BENCH_PG_URL")


@pytest.mark.parametrize(
    "text,key,fragment",
    [
        ('[stores.main]\nbackend = "sqlite"\npath = "x"\n', "stores.main", "derived from db.backend"),
        ('[stores.Bad]\nbackend = "sqlite"\npath = "x"\n', "stores.Bad", "lowercase"),
        ('[stores.a]\nbackend = "mysql"\n', "stores.a.backend", "not one of"),
        ('[stores.a]\nbackend = "postgres"\n', "stores.a.dsn_secret", "needs dsn_secret"),
        (
            '[stores.a]\nbackend = "sqlite"\npath = "x"\ndsn_secret = "Y"\n',
            "stores.a.dsn_secret",
            "not a sqlite store key",
        ),
        ('[stores.a]\nbackend = "postgres"\ndsn_secret = 3\n', "stores.a.dsn_secret", "must be a string"),
    ],
)
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
    pin_database(legacy)
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

    use_backend("sqlite")
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

    use_backend("sqlite")
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

    from m3_core.secret_store import set_secret

    use_backend("sqlite")
    # The store first: its dsn_secret is a name the secrets file accepts once declared.
    stores_file('[stores.bench]\nbackend = "postgres"\ndsn_secret = "M3_STORE_BENCH_PG_URL"\n')
    if dsn is not None:
        set_secret("M3_STORE_BENCH_PG_URL", dsn)
    return importlib.import_module("memory.backends.selector")


def test_an_unset_store_secret_is_reported_with_the_fix(monkeypatch, stores_file):
    from m3_core.config.stores import active_store

    mb = _pg_store(monkeypatch, stores_file, None)
    mb._reset_for_tests()
    with active_store("bench"):
        with pytest.raises(
            RuntimeError, match=r"dsn_secret M3_STORE_BENCH_PG_URL.*m3 secret set M3_STORE_BENCH_PG_URL"
        ):
            mb.active_backend()


def test_a_store_on_the_warehouse_is_refused(monkeypatch, stores_file):
    from m3_core.config.stores import active_store

    dsn = "postgresql://u:p@wh.example:5432/cdw"
    mb = _pg_store(monkeypatch, stores_file, dsn)
    from m3_core.secret_store import set_secret

    set_secret("M3_CDW_PG_URL", dsn)
    mb._reset_for_tests()
    with active_store("bench"):
        with pytest.raises(Exception, match=r"(?i)warehouse"):
            mb.active_backend()


def test_two_stores_on_one_database_share_an_instance(monkeypatch, stores_file):
    from m3_core.config.stores import active_store

    dsn = "postgresql://u:p@db.example:5432/m3"
    import importlib

    mb = importlib.import_module("memory.backends.selector")
    from m3_core.secret_store import set_secret

    use_backend("sqlite")
    stores_file(
        '[stores.a]\nbackend = "postgres"\ndsn_secret = "M3_STORE_A_PG_URL"\n'
        '[stores.b]\nbackend = "postgres"\ndsn_secret = "M3_STORE_B_PG_URL"\n'
    )
    set_secret("M3_STORE_A_PG_URL", dsn)
    set_secret("M3_STORE_B_PG_URL", dsn)
    prepared = _record_prepare(monkeypatch)
    mb._reset_for_tests()
    with active_store("a"):
        first = mb.active_backend()
    with active_store("b"):
        second = mb.active_backend()
    assert first is second, "one database, two pools"
    assert prepared == ["postgres:db.example:5432/m3"], "prepared once, for the new store only"
    mb._reset_for_tests()


def _record_prepare(monkeypatch) -> list:
    import importlib

    pg = importlib.import_module("memory.backends.postgres_backend")
    prepared: list = []
    monkeypatch.setattr(pg.PostgresBackend, "prepare_store", lambda self: prepared.append(self.store_identity()))
    return prepared


def test_a_store_naming_the_default_database_is_never_prepared(monkeypatch, stores_file):
    """Its schema is the installer's; a store alias must not migrate it, even when
    nothing in the process has used the default store yet."""
    import importlib

    from m3_core.config.stores import active_store

    dsn = "postgresql://u:p@db.example:5432/m3"
    mb = importlib.import_module("memory.backends.selector")
    from m3_core.secret_store import set_secret

    stores_file(
        '[db]\nbackend = "postgres"\n[stores.alias]\nbackend = "postgres"\ndsn_secret = "M3_STORE_ALIAS_PG_URL"\n'
    )
    set_secret("M3_PRIMARY_PG_URL", dsn)
    set_secret("M3_STORE_ALIAS_PG_URL", "host=db.example dbname=m3 user=other")
    prepared = _record_prepare(monkeypatch)
    mb._reset_for_tests()
    try:
        with active_store("alias"):
            via_store = mb.active_backend()
        assert via_store is mb.active_backend(), "the alias shares the default instance"
        assert prepared == []
    finally:
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
    return cat, cat.ToolSpec(
        name="memory_probe", description="probe", parameters={"type": "object", "properties": props}, impl=_impl
    )


def _scratch(stores_file, tmp_path, monkeypatch) -> str:
    use_backend("sqlite")
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
    _cat, spec = _path_probe(ran)
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


@pytest.mark.parametrize(
    "args,fragment",
    [
        ({"store": "scrach"}, "did you mean 'scratch'"),
        ({"store": "scratch", "database": "x.db"}, "both select the store"),
        ({"store": 3}, "is a store name"),
    ],
)
def test_a_bad_store_argument_is_refused_before_the_impl(stores_file, tmp_path, monkeypatch, args, fragment):
    import asyncio

    _scratch(stores_file, tmp_path, monkeypatch)
    ran: list = []
    cat, spec = _path_probe(ran)
    assert fragment in asyncio.run(cat.execute_tool(spec, dict(args), agent_id=""))
    with pytest.raises(ValueError, match=fragment):  # dry_run validates it too
        asyncio.run(cat.execute_tool_structured(spec, dict(args), agent_id="", dry_run=True))
    assert ran == []


# ── 3c: a store is core + files ──────────────────────────────────────────────
def test_a_sqlite_store_derives_its_own_files_database(tmp_path):
    from m3_core.config.stores import StoreError, files_db_path
    from m3_core.paths import StoreRef

    core = tmp_path / "engine" / "scratch.db"
    assert files_db_path(StoreRef(name="s", backend="sqlite", path=str(core))) == str(
        core.with_name("scratch.files.db")
    )
    with pytest.raises(StoreError, match="only a SQLite store"):
        files_db_path(StoreRef(name="b", backend="postgres", dsn_secret="X"))


def test_files_writes_inside_a_sqlite_store_land_in_its_files_database(stores_file, tmp_path, monkeypatch):
    """Inside a store, files writes go to the store's files database, never the
    default one under the engine root (D3)."""
    import importlib

    from m3_core.config.stores import active_store

    from m3_memory import _roots

    default = Path(_roots.engine_root()) / "files_database.db"
    _scratch(stores_file, tmp_path, monkeypatch)
    fdb = importlib.import_module("files_memory.db")
    marker = f"files-probe-{uuid.uuid4().hex}"
    with active_store("scratch"):
        with fdb._db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS _files_probe (v TEXT)")
            conn.execute("INSERT INTO _files_probe (v) VALUES (?)", (marker,))
    with sqlite3.connect(tmp_path / "s.files.db") as raw:
        assert raw.execute("SELECT v FROM _files_probe").fetchall() == [(marker,)]
    assert not default.exists(), "the default files store was opened inside the store"


def test_a_files_tool_follows_store(stores_file, tmp_path, monkeypatch):
    import asyncio

    import mcp_tool_catalog as cat

    from m3_memory import _roots

    default = Path(_roots.engine_root()) / "files_database.db"
    _scratch(stores_file, tmp_path, monkeypatch)
    out = asyncio.run(cat.execute_tool_structured(cat._BY_NAME["files_stats"], {"store": "scratch"}, agent_id=""))
    assert isinstance(out, dict)
    assert (tmp_path / "s.files.db").exists() and not default.exists()


def test_postgres_files_init_is_per_store(monkeypatch):
    """One sentinel for every PostgreSQL database marked a store's files schema
    migrated once ANY database was, so a named store never got one."""
    import importlib

    fdb = importlib.import_module("files_memory.db")
    mb = importlib.import_module("memory.backends")
    idents = iter(["postgres:h:5432/main", "postgres:h:5432/bench"])

    class _Backend:
        def __init__(self, ident):
            self.ident = ident

        def store_identity(self):
            return self.ident

    first, second = _Backend(next(idents)), _Backend(next(idents))
    monkeypatch.setattr(mb, "active_backend", lambda: first)
    a = fdb._init_key(None, True)
    monkeypatch.setattr(mb, "active_backend", lambda: second)
    assert fdb._init_key(None, True) != a


@pytest.mark.requires_pg
def test_a_postgres_store_on_a_second_database_gets_its_own_core_and_files(monkeypatch, stores_file):
    """A store on another database of the same cluster gets its core and files
    schemas created THERE, after main's were (the order a single files sentinel
    got wrong)."""
    import importlib

    import psycopg2
    from m3_core.config.stores import active_store
    from psycopg2.extensions import make_dsn, parse_dsn

    from conftest import pg_dsn

    lane = pg_dsn()
    db = f"m3_store_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(lane)
    admin.autocommit = True
    admin.cursor().execute(f'CREATE DATABASE "{db}"')
    store_dsn = make_dsn(lane, dbname=db)
    mb = importlib.import_module("memory.backends.selector")
    try:
        from m3_core.secret_store import set_secret

        stores_file('[stores.second]\nbackend = "postgres"\ndsn_secret = "M3_STORE_SECOND_PG_URL"\n')
        set_secret("M3_STORE_SECOND_PG_URL", store_dsn)
        mb._reset_for_tests()
        fdb = importlib.import_module("files_memory.db")
        from files_memory.config import files_table

        mdb = importlib.import_module("memory.db")
        with mdb._db():  # main first: under one sentinel this marked files "done"
            pass
        with fdb._db():
            pass
        with active_store("second"):
            assert mb.active_backend().store_identity().endswith(f"/{db}")
            with mdb._db():
                pass
            with fdb._db() as conn:  # the files schema exists in the second database
                conn.execute(f"SELECT COUNT(*) FROM {files_table('file_nodes')}").fetchall()
        with psycopg2.connect(store_dsn) as raw, raw.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('files.file_nodes') IS NOT NULL, to_regclass('public.memory_items') IS NOT NULL"
            )
            assert cur.fetchone() == (True, True)
        assert parse_dsn(store_dsn)["dbname"] == db
    finally:
        for backend in list(mb._backends.values()):
            close = getattr(backend, "close", None)
            if close is not None:
                close()
        mb._reset_for_tests()
        admin.cursor().execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        admin.close()


def test_the_cli_passes_store_to_the_dispatcher(stores_file, tmp_path, monkeypatch):
    """`m3 <domain> <tool> --store`: the dispatcher's refusal proves the flag arrived."""
    import subprocess
    import sys

    _scratch(stores_file, tmp_path, monkeypatch)
    repo = Path(__file__).resolve().parent.parent
    run = subprocess.run(
        [sys.executable, "-m", "m3_memory.cli", "memory", "memory_search", "--query", "x", "--store", "scrach"],
        capture_output=True,
        text=True,
        cwd=str(repo),
    )
    assert "did you mean 'scratch'" in run.stdout + run.stderr, run.stdout + run.stderr


# ── 3d: a CLI's --store selects the store for the whole process ──────────────
def _cli_parser():
    import argparse

    from m3_core.paths import add_database_arg

    parser = argparse.ArgumentParser(prog="probe")
    add_database_arg(parser)
    return parser


def test_cli_store_is_seen_by_worker_threads(stores_file, tmp_path, monkeypatch):
    """A ContextVar set in main() is invisible to a ThreadPoolExecutor worker,
    which would quietly use the default store: the process layer is seen by all."""
    from concurrent.futures import ThreadPoolExecutor

    from m3_core.paths import resolve_db_path

    path = _scratch(stores_file, tmp_path, monkeypatch)
    _cli_parser().parse_args(["--store", "scratch"])
    assert resolve_db_path(None) == path
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(resolve_db_path, None).result() == path


def test_a_per_call_store_and_the_chatlog_override_the_process_store(stores_file, tmp_path, monkeypatch):
    import chatlog_config
    from m3_core.config.stores import active_store, current_store_name
    from m3_core.paths import resolve_db_path

    before = chatlog_config.chatlog_db_path()
    path = _scratch(stores_file, tmp_path, monkeypatch)
    _cli_parser().parse_args(["--store", "scratch"])
    with active_store("main"):
        assert current_store_name() == "main" and resolve_db_path(None) != path
    assert current_store_name() == "scratch"
    assert chatlog_config.chatlog_db_path() == before  # D1: process-wide, store or not


@pytest.mark.parametrize(
    "argv,fragment",
    [
        (["--store", "scrach"], "did you mean 'scratch'"),
        (["--store", "scratch", "--database", "x.db"], "not allowed with argument"),
    ],
)
def test_a_bad_cli_store_is_a_usage_error(stores_file, tmp_path, monkeypatch, capsys, argv, fragment):
    _scratch(stores_file, tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        _cli_parser().parse_args(argv)
    assert exc.value.code == 2 and fragment in capsys.readouterr().err


def test_a_script_given_store_writes_into_it(stores_file, tmp_path, monkeypatch):
    """End to end: a real CLI that only calls add_database_arg + resolve_db_path."""
    import subprocess
    import sys

    _scratch(stores_file, tmp_path, monkeypatch)
    repo = Path(__file__).resolve().parent.parent
    run = subprocess.run(
        [sys.executable, str(repo / "bin" / "setup_test_db.py"), "--store", "scratch"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert run.returncode == 0, run.stdout + run.stderr
    with sqlite3.connect(tmp_path / "s.db") as raw:
        assert raw.execute("SELECT 1 FROM sqlite_master WHERE name='memory_items'").fetchone()


def test_a_deprecated_alias_keeps_the_new_flags_default_and_warns(capsys):
    """argparse seeds a shared dest from the FIRST action: an alias declared first
    must not replace the new flag's default."""
    import argparse

    from m3_core.paths import deprecated_alias

    parser = argparse.ArgumentParser()
    parser.add_argument("--old", dest="new", action=deprecated_alias("--new"))
    parser.add_argument("--new", default="from-env")
    assert parser.parse_args([]).new == "from-env"
    assert parser.parse_args(["--old", "x"]).new == "x"
    assert "--old is the old spelling of --new" in capsys.readouterr().err


# ── 3e: a child process inherits the store through argv ──────────────────────
def test_child_store_args_round_trip_through_a_child_cli(stores_file, tmp_path, monkeypatch):
    """What child_store_args emits is exactly what a child's add_database_arg
    parses back into the same store."""
    from m3_core.config.stores import active_store, child_store_args, current_store_name

    _scratch(stores_file, tmp_path, monkeypatch)
    assert child_store_args() == []
    with active_store("scratch"):
        argv = child_store_args()
    assert argv == ["--store", "scratch"]
    _cli_parser().parse_args(argv)
    assert current_store_name() == "scratch"


def test_embed_backfill_takes_target_and_the_old_store_spelling(capsys):
    import embed_backfill as eb

    assert eb._parse_args([]).target == "core"
    assert eb._parse_args(["--target", "chatlog"]).target == "chatlog"
    assert eb._parse_args(["--store", "chatlog"]).target == "chatlog"
    assert "old spelling of --target" in capsys.readouterr().err
