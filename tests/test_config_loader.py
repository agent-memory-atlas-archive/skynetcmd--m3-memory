"""m3_core.config: registry invariants, the locator, and the file loader.

Planted bad files, each of which must fail LOUD with the file and the key named
(CONFIG_DESIGN §3): unknown key, wrong type, invalid value, a secret placed in
m3_config.toml, an env name used as a key, invalid TOML. Several at once must
all be reported.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "m3_config.toml"
    p.write_text(text, encoding="utf-8", newline="\n")
    return p


def _load(path: Path):
    from m3_core.config import REGISTRY, Location, load

    return load(Location(file=path, root=path.parent, tier="test"), REGISTRY)


# ── registry ─────────────────────────────────────────────────────────────────
def test_bool_parser_is_canonical():
    from m3_core.config import ConfigValueError, parse_bool

    for raw in ("1", "true", "YES", " on "):
        assert parse_bool(raw) is True
    for raw in ("0", "false", "No", "off", ""):
        assert parse_bool(raw) is False
    with pytest.raises(ConfigValueError, match="'maybe' is not a boolean"):
        parse_bool("maybe")


def test_no_key_can_let_env_outrank_the_file():
    """The one former exception (db.backend) is gone: no
    key's env value outranks m3_config.toml, so the machinery for one is gone.
    Bringing it back is a design change, not a flag."""
    from dataclasses import fields

    from m3_core.config import Key

    assert "env_override" not in {f.name for f in fields(Key)}


def test_bootstrap_secrets_are_the_design_set():
    """CONFIG_DESIGN §10: never resolved from the vault."""
    from m3_core.config import REGISTRY, KeyClass

    boot = {k.name for k in REGISTRY if k.bootstrap}
    assert boot == {"M3_PRIMARY_PG_URL", "AGENT_OS_MASTER_KEY", "M3_AGENT_OS_SALT_HEX"}
    assert all(k.klass is KeyClass.SECRET and k.default is None for k in REGISTRY if k.klass is KeyClass.SECRET)


def test_db_backend_set_is_the_selectors():
    """S6: the registry validates against SELECTABLE_BACKENDS, not a copy."""
    from m3_core.config import REGISTRY, ConfigValueError
    from m3_core.paths import SELECTABLE_BACKENDS

    key = REGISTRY.get("db.backend")
    for name in SELECTABLE_BACKENDS:
        key.check(name)
    with pytest.raises(ConfigValueError):
        key.check("postgre")


def test_every_key_has_a_typed_parser():
    """A key parsed by the builtin ``str`` got no TOML type check at all:
    ``dashboard.host = 8088`` passed through as an int (13 keys, found 2026-10-11).
    Every key must use a registry parser or an annotated one of its own."""
    import typing

    from m3_core.config import REGISTRY
    from m3_core.config.registry import known_type

    untyped = sorted(
        k.name for k in REGISTRY if known_type(k) is None and "return" not in typing.get_type_hints(k.parse)
    )
    assert not untyped, f"keys with an untyped parser (use registry.parse_str etc.): {untyped}"


def test_a_string_key_rejects_a_non_string_file_value():
    from m3_core.config import REGISTRY, ConfigValueError

    with pytest.raises(ConfigValueError, match="expected str"):
        REGISTRY.get("dashboard.host").coerce_file_value(8088)


def test_an_env_name_is_claimed_by_one_key_only():
    from m3_core.config import REGISTRY, Key, Registry

    dup = Key[str](name="db.other", parse=str, default="", doc="x", env_names=("M3_DB_BACKEND",))
    with pytest.raises(ValueError, match=re.escape("claimed by db.backend")):
        Registry([*REGISTRY, dup])


# ── locator ──────────────────────────────────────────────────────────────────
def test_locator_tiers(monkeypatch, tmp_path):
    from m3_core.config import locate

    explicit = tmp_path / "x" / "my.toml"
    loc = locate(explicit)
    assert (loc.file, loc.root, loc.tier) == (explicit, explicit.parent, "arg:--config")

    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path / "cfg"))
    loc = locate()
    assert loc.tier == "env:M3_CONFIG_ROOT" and loc.file.name == "m3_config.toml"


def test_legacy_tier_warns(monkeypatch, tmp_path, caplog):
    import logging

    from m3_core.config import loader, locate

    monkeypatch.delenv("M3_CONFIG_ROOT", raising=False)
    monkeypatch.setenv("M3_MEMORY_ROOT", str(tmp_path / "legacy"))
    monkeypatch.setattr(loader, "_LEGACY_WARNED", set())
    with caplog.at_level(logging.WARNING):
        loc = locate()
    assert loc.tier.startswith("legacy:") and "removed at N+3" in caplog.text


# ── loader ───────────────────────────────────────────────────────────────────
def test_absent_file_is_not_an_error(tmp_path):
    got = _load(tmp_path / "m3_config.toml")
    assert got.identity is None and dict(got.values) == {}


def test_a_valid_file_loads_typed_values(tmp_path):
    got = _load(_write(tmp_path, 'schema_version = 1\n[db]\nbackend = "postgres"\n[secrets]\nfile = "~/s.toml"\n'))
    assert got.values["db.backend"] == "postgres"
    assert got.values["secrets.file"] == Path("~/s.toml").expanduser()
    assert got.identity is not None and len(got.identity.sha256) == 64


def test_every_problem_is_reported_at_once_with_file_key_and_line(tmp_path):
    from m3_core.config import ConfigFileError, format_config_error

    p = _write(
        tmp_path,
        "\n".join(
            [
                "schema_version = 1",
                "[db]",
                'backend = "postgre"',  # line 3: invalid value
                'bakend = "sqlite"',  # line 4: unknown, did-you-mean
                "[secrets]",
                "file = 7",  # line 6: wrong type
                'M3_PRIMARY_PG_URL = "postgresql://u:hunter2@h/db"',  # line 7: a secret in the file
                "",
            ]
        ),
    )
    with pytest.raises(ConfigFileError) as exc:
        _load(p)
    text = format_config_error(exc.value)
    assert "4 invalid entries" in text, text
    assert "[db].backend: 'postgre' is not one of sqlite, postgres" in text and "line 3" in text
    assert "[db].bakend: unknown key" in text and "did you mean [db].backend?" in text and "line 4" in text
    assert "[secrets].file: expected str, found int 7" in text and "line 6" in text
    assert "is a secret and is never read from this file" in text and "line 7" in text
    assert "hunter2" not in text, "a secret's value was echoed"
    assert str(p) in text and text.count("m3 config show --origin") == 4


def test_an_env_name_used_as_a_key_names_the_real_key(tmp_path):
    from m3_core.config import ConfigFileError, format_config_error

    with pytest.raises(ConfigFileError) as exc:
        _load(_write(tmp_path, 'M3_DB_BACKEND = "postgres"\n'))
    assert "in this file the setting is [db].backend" in format_config_error(exc.value)


def test_invalid_toml_names_the_file_and_tomllibs_position(tmp_path):
    from m3_core.config import ConfigFileError, ConfigSyntaxError, format_config_error

    p = _write(tmp_path, "[db\nbackend = 1\n")
    with pytest.raises(ConfigFileError) as exc:
        _load(p)
    assert isinstance(exc.value.exceptions[0], ConfigSyntaxError)
    text = format_config_error(exc.value)
    assert str(p) in text and "line 1" in text, text


def test_a_newer_schema_only_warns_on_unknown_keys(tmp_path):
    got = _load(_write(tmp_path, 'schema_version = 2\n[future]\nknob = 1\n[db]\nbackend = "sqlite"\n'))
    assert got.values["db.backend"] == "sqlite"
    assert any("[future].knob" in w and "schema 2" in w for w in got.warnings)


def test_a_bool_is_not_accepted_where_a_string_is_expected(tmp_path):
    from m3_core.config import ConfigFileError

    with pytest.raises(ConfigFileError, match="1 invalid entry"):
        _load(_write(tmp_path, "[db]\nbackend = true\n"))


def test_split_keeps_the_group_type():
    from m3_core.config import ConfigFileError, ConfigKeyError

    g = ConfigFileError("x", [ConfigKeyError(Path("f"), "a.b", "bad"), ValueError("other")])
    match, rest = g.split(ConfigKeyError)
    assert isinstance(match, ConfigFileError) and isinstance(rest, ConfigFileError)
