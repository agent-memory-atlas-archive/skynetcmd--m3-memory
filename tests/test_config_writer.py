"""m3_core.config.writer: the writer of m3_config.toml.

The user's comments and layout survive; each refusal leaves the file byte for
byte unchanged; what it writes, the loader reads back.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest


def _loc(tmp_path: Path):
    from m3_core.config import Location

    return Location(file=tmp_path / "m3_config.toml", root=tmp_path, tier="t")


def _key(name):
    from m3_core.config import REGISTRY

    return REGISTRY.get(name)


def _load(loc):
    from m3_core.config import REGISTRY, load

    return load(loc, REGISTRY).values


def test_a_new_file_gets_a_header_and_schema_and_reads_back(tmp_path):
    from m3_core.config.writer import set_values

    loc = _loc(tmp_path)
    set_values(loc, {_key("db.backend"): "postgres", _key("db.postgres.pool_max"): 32})
    text = loc.file.read_text(encoding="utf-8")
    assert text.startswith("# m3 configuration") and "schema_version = 1" in text
    assert dict(_load(loc)) == {"db.backend": "postgres", "db.postgres.pool_max": 32}


def test_the_users_comments_and_layout_survive(tmp_path):
    from m3_core.config.writer import set_values

    loc = _loc(tmp_path)
    original = (
        "schema_version = 1\n"
        "# my note about the backend\n"
        "[db]\n"
        'backend = "sqlite"   # trailing comment\n'
        "\n"
        "[search]\n"
        "row_cap = 4000\n"
    )
    loc.file.write_text(original, encoding="utf-8", newline="\n")
    set_values(loc, {_key("search.row_cap"): 6000})
    text = loc.file.read_text(encoding="utf-8")
    assert "# my note about the backend" in text and "# trailing comment" in text
    assert text == original.replace("row_cap = 4000", "row_cap = 6000")


def test_none_removes_a_key(tmp_path):
    from m3_core.config.writer import set_values

    loc = _loc(tmp_path)
    set_values(loc, {_key("search.row_cap"): 6000, _key("db.pool_size"): 3})
    set_values(loc, {_key("search.row_cap"): None})
    assert dict(_load(loc)) == {"db.pool_size": 3}


def test_paths_and_lists_are_written_toml_native(tmp_path):
    from m3_core.config.writer import set_values

    loc = _loc(tmp_path)
    set_values(
        loc, {_key("paths.engine_root"): Path(r"C:\m3 'eng"), _key("db.postgres.forbidden_hosts"): ["wh.example", "b"]}
    )
    got = _load(loc)
    assert got["paths.engine_root"] == Path(r"C:\m3 'eng").expanduser()
    assert got["db.postgres.forbidden_hosts"] == ["wh.example", "b"]


@pytest.mark.parametrize(
    "name,value,match",
    [
        ("M3_SERVE_TOKEN", "s3cret", "never written to m3_config.toml; store it with"),
        ("db.backend", "postgre", "not one of"),
    ],
)
def test_refusals_leave_the_file_untouched(tmp_path, name, value, match):
    from m3_core.config import ConfigValueError
    from m3_core.config.writer import ConfigWriteError, set_values

    loc = _loc(tmp_path)
    set_values(loc, {_key("db.pool_size"): 3})
    before = loc.file.read_bytes()
    with pytest.raises((ConfigWriteError, ConfigValueError), match=match):
        set_values(loc, {_key(name): value})
    assert loc.file.read_bytes() == before
    assert b"s3cret" not in before


def test_new_content_that_would_not_load_is_refused(tmp_path):
    """The user's own file holds an error the writer did not make: refuse to
    replace it with something that still fails, and name the problem."""
    from m3_core.config import ConfigFileError
    from m3_core.config.writer import set_values

    loc = _loc(tmp_path)
    loc.file.write_text("[db]\nbakend = 1\n", encoding="utf-8")
    before = loc.file.read_bytes()
    with pytest.raises(ConfigFileError, match="1 invalid entry"):
        set_values(loc, {_key("db.pool_size"): 3})
    assert loc.file.read_bytes() == before


def test_a_key_from_another_registry_is_refused(tmp_path):
    from m3_core.config import Key, parse_bool
    from m3_core.config.writer import ConfigWriteError, set_values

    rogue = Key[bool](name="db.rogue", parse=parse_bool, default=False, doc="x")
    with pytest.raises(ConfigWriteError, match="not a registry key"):
        set_values(_loc(tmp_path), {rogue: True})


@pytest.mark.requires_posix  # POSIX mode bits
def test_a_new_file_is_user_only(tmp_path):
    from m3_core.config.writer import set_values

    loc = _loc(tmp_path)
    set_values(loc, {_key("db.pool_size"): 3})
    assert stat.S_IMODE(os.stat(loc.file).st_mode) == 0o600


@pytest.mark.requires_windows  # Windows DACLs
def test_a_new_file_in_a_shared_directory_is_refused(tmp_path):
    from m3_core.config import Location
    from m3_core.config.writer import ConfigWriteError, set_values

    shared = tmp_path / "shared"
    shared.mkdir()
    subprocess.run(["icacls", str(shared), "/grant", "*S-1-5-32-545:(OI)(CI)R"], check=True, capture_output=True)
    loc = Location(file=shared / "m3_config.toml", root=shared, tier="t")
    with pytest.raises(ConfigWriteError, match="NOT written"):
        set_values(loc, {_key("db.pool_size"): 3})
    assert [p.name for p in shared.iterdir() if not p.name.endswith(".lock")] == []


def test_the_writer_reads_back_a_non_reloadable_key_it_wrote():
    """A daemon keeps a non-reloadable key at its startup value; the process that
    WRITES the file must not. Setup imported embed.server.port into a new file,
    kept reading the startup default, and wrote another port over the import."""
    from m3_core.config import locate, settings
    from m3_core.config.writer import set_values

    key = _key("embed.server.port")
    assert key.reloadable is False, "precondition: the case the freeze applies to"
    before = settings.setting("embed.server.port")  # builds the snapshot first
    set_values(locate(), {key: before + 11})
    assert settings.setting("embed.server.port") == before + 11


def test_the_writer_leaves_a_test_installed_snapshot_alone(m3_settings):
    from m3_core.config import locate, settings
    from m3_core.config.writer import set_values

    m3_settings({"embed.server.port": 9123})
    set_values(locate(), {_key("embed.server.port"): 9124})
    assert settings.setting("embed.server.port") == 9123
