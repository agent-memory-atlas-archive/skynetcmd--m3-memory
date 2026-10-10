"""Registry table [db] / [db.postgres] / [paths] (plan 2C.1, first table).

Defaults are pinned to today's readers where the code holds them in a named
constant or a callable, so the registry cannot drift from what runs; literals
inside a call (the SQLite pool's 5 / 30) are covered by the 2A.2 parity capture.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _key(name):
    from m3_core.config import REGISTRY

    return REGISTRY.get(name)


def _build(tmp_path, text, environ):
    from m3_core.config import Location
    from m3_core.config.settings import build

    p = tmp_path / "m3_config.toml"
    if text is not None:
        p.write_text(text, encoding="utf-8")
    return build(Location(file=p, root=tmp_path, tier="test"), environ=environ)


def test_postgres_pool_defaults_are_the_backends():
    from memory.backends import postgres_backend as pb

    assert _key("db.postgres.pool_min").default == pb._DEFAULT_MINCONN
    assert _key("db.postgres.pool_max").default == pb._DEFAULT_MAXCONN


def test_dispatch_schema_default_is_the_readers(monkeypatch):
    from m3_core import paths

    monkeypatch.delenv("M3_DISPATCH_PG_SCHEMA", raising=False)
    assert _key("db.postgres.dispatch_schema").default == paths.dispatch_pg_schema()


@pytest.mark.parametrize(
    "name,env,raw,value",
    [
        ("db.pool_size", "DB_POOL_SIZE", "7", 7),  # legacy alias still answers
        ("db.pool_timeout_s", "M3_DB_POOL_TIMEOUT", "45", 45),
        ("db.postgres.forbidden_hosts", "M3_PG_FORBIDDEN_HOSTS", " a.b , ,c.d ", ["a.b", "c.d"]),
        ("db.postgres.upsert_page_size", "M3_PG_UPSERT_PAGE_SIZE", "250", 250),
    ],
)
def test_env_names_parse_as_todays_readers_do(tmp_path, name, env, raw, value):
    s = _build(tmp_path, None, {env: raw})
    assert s[_key(name)] == value and s.origin(_key(name)) == f"env-legacy:{env}"


def test_the_file_spells_them_as_tables(tmp_path):
    s = _build(
        tmp_path,
        "\n".join(
            [
                "[db]",
                "pool_size = 3",
                "[db.postgres]",
                "pool_max = 32",
                'forbidden_hosts = ["wh.example"]',
                "[paths]",
                'engine_root = "~/m3-engine"',
                "",
            ]
        ),
        {},
    )
    assert (s[_key("db.pool_size")], s[_key("db.postgres.pool_max")]) == (3, 32)
    assert s[_key("db.postgres.forbidden_hosts")] == ["wh.example"]
    assert s[_key("paths.engine_root")] == Path("~/m3-engine").expanduser()


def test_mmap_size_is_validated_not_silently_ignored(tmp_path):
    """sqlite_pragmas ignores a non-integer silently today: intended change."""
    from m3_core.config import format_config_error
    from m3_core.config.resolve import ConfigEnvErrors

    with pytest.raises(ConfigEnvErrors) as exc:
        _build(tmp_path, None, {"M3_SQLITE_MMAP_SIZE": "big"})
    assert "M3_SQLITE_MMAP_SIZE='big'" in format_config_error(exc.value)


def test_a_list_default_is_never_the_registrys_own_object(tmp_path):
    s = _build(tmp_path, None, {})
    got = s[_key("db.postgres.forbidden_hosts")]
    got.append("mutated")
    assert _key("db.postgres.forbidden_hosts").default == []


def test_the_config_locator_is_not_a_key():
    """CONFIG_DESIGN §2 read path 1: it picks the file, so it cannot be in it."""
    from m3_core.config import REGISTRY
    from m3_core.config.keys import NOT_SETTINGS

    assert REGISTRY.by_env("M3_CONFIG_ROOT") is None and "M3_CONFIG_ROOT" in NOT_SETTINGS
