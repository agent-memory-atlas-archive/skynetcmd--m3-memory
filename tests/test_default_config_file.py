"""The shipped default m3_config.toml (m3_core.config.default_file).

It is generated from the registry with every key commented at its default, so a
fresh file means nothing but schema_version; it is the same bytes whichever OS
generates it; m3 copies it (hash-verified) only into a config root that has no
m3_config.toml; and setting/unsetting a key edits its commented line in place.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


def _loc(root: Path):
    from m3_core.config import Location

    return Location(file=root / "m3_config.toml", root=root, tier="t")


def test_the_default_file_loads_to_pure_defaults(tmp_path):
    """Every key commented: loading it yields no value of its own, so every key
    resolves to its registry default (none comes from the file)."""
    from m3_core.config import REGISTRY, load
    from m3_core.config.default_file import DEFAULT_FILE
    from m3_core.config.settings import build

    loc = _loc(tmp_path)
    loc.file.write_bytes(DEFAULT_FILE.read_bytes())
    loaded = load(loc, REGISTRY)
    assert dict(loaded.values) == {} and dict(loaded.stores) == {}
    assert loaded.schema_version == 1 and "schema_version = 1" in loc.file.read_text(encoding="utf-8")
    settings = build(loc, environ={})
    from_file = [n for n, r in settings.resolved.items() if r.origin.startswith("file:")]
    assert from_file == []
    for key in REGISTRY:
        r = settings.resolved.get(key.name)
        if r is not None and r.origin == "default":
            assert r.value == key.default, key.name


def test_every_config_key_is_listed_once_and_no_secret_appears():
    from m3_core.config import REGISTRY, KeyClass
    from m3_core.config.default_file import commented_line, render

    text = render()
    for key in REGISTRY:
        if key.klass is KeyClass.CONFIG:
            assert commented_line(key) in text, key.name
        else:
            # A doc may name a secret (cloud.auth_token_keyring points at one); no line may SET it.
            assert not [ln for ln in text.splitlines() if ln.lstrip("# ").startswith(f"{key.name} =")], key.name


def test_a_computed_default_uncommented_as_is_is_not_valid_toml():
    """The header promises it: a <placeholder> left in place fails loudly rather
    than becoming a literal path such as "<config root>/m3_secrets.toml"."""
    from m3_core.config import REGISTRY, KeyClass
    from m3_core.config.default_file import commented_line

    computed = [k for k in REGISTRY if k.klass is KeyClass.CONFIG and k.default is None]
    assert computed
    for key in computed:
        line = commented_line(key).removeprefix("# ")
        with pytest.raises(tomllib.TOMLDecodeError):
            tomllib.loads(line)
    secrets_file = commented_line(REGISTRY.get("secrets.file"))
    assert secrets_file == "# file = <config root>/m3_secrets.toml  # default (computed)"


@pytest.mark.parametrize("os_name", ["Windows", "Darwin", "Linux"])
def test_the_default_file_is_byte_identical_on_every_os(os_name, monkeypatch, tmp_path):
    """Rendered under each simulated OS, with home and every root pointed at a
    machine-specific directory: the same bytes as the committed file, and none of
    those directories (nor the separator-joined form of them) leaks in."""
    from m3_core import paths
    from m3_core.config.default_file import DEFAULT_FILE, render

    from conftest import simulate_os

    real_home = Path.home()
    home = tmp_path / f"home-{os_name}"
    # Through the locators, not the environment (tests configure m3 without env writes).
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(p).replace("~", str(home), 1))
    for name in ("get_m3_root", "get_m3_config_root", "get_m3_engine_root", "get_m3_logs_root", "get_m3_models_root"):
        monkeypatch.setattr(paths, name, lambda n=name: str(tmp_path / n))
    monkeypatch.setattr(paths, "config_root_with_tier", lambda: (str(tmp_path / "cfg"), "env"))
    simulate_os(monkeypatch, os_name)
    text = render()
    assert text.encode("utf-8") == DEFAULT_FILE.read_bytes().replace(b"\r\n", b"\n")
    for leak in (str(tmp_path), tmp_path.as_posix(), str(real_home), real_home.as_posix()):
        assert leak not in text
    assert "\r" not in text


def test_the_hash_module_matches_the_shipped_file():
    from m3_core.config._default_config_sha256 import SHA256
    from m3_core.config.default_file import DEFAULT_FILE, digest

    assert digest(DEFAULT_FILE.read_bytes()) == SHA256
    assert digest(b"a\r\nb\n") == digest(b"a\nb\n"), "a CRLF checkout must still verify"


def test_a_tampered_default_is_refused_and_nothing_is_written(tmp_path, monkeypatch):
    from m3_core.config import default_file
    from m3_core.config._default_config_sha256 import SHA256
    from m3_core.config.writer import ConfigWriteError, create_from_default

    bad = tmp_path / default_file.DEFAULT_FILE_NAME
    bad.write_text(default_file.DEFAULT_FILE.read_text(encoding="utf-8") + "\n[db]\nbackend = 'x'\n", "utf-8")
    monkeypatch.setattr(default_file, "DEFAULT_FILE", bad)
    root = tmp_path / "cfg"  # does not exist: a refused copy must not create it either
    with pytest.raises(ConfigWriteError) as e:
        create_from_default(_loc(root))
    msg = str(e.value)
    assert str(bad) in msg and SHA256[:12] in msg and default_file.digest(bad.read_bytes())[:12] in msg
    assert "reinstall m3" in msg and "nothing was written" in msg
    assert not root.exists()


def test_a_missing_default_is_refused_with_the_fix(tmp_path, monkeypatch):
    from m3_core.config import default_file
    from m3_core.config.writer import ConfigWriteError, create_from_default

    monkeypatch.setattr(default_file, "DEFAULT_FILE", tmp_path / "gone.toml")
    with pytest.raises(ConfigWriteError, match="reinstall m3"):
        create_from_default(_loc(tmp_path / "cfg"))
    assert not (tmp_path / "cfg").exists()


def test_the_copy_happens_only_when_absent_and_never_overwrites(tmp_path):
    from m3_core.config.default_file import verified_text
    from m3_core.config.writer import create_from_default

    loc = _loc(tmp_path / "cfg")
    assert create_from_default(loc) is True
    assert loc.file.read_text(encoding="utf-8") == verified_text()
    assert create_from_default(loc) is False  # idempotent
    loc.file.write_text("schema_version = 1\n[db]\npool_size = 3\n", encoding="utf-8")
    before = loc.file.read_bytes()
    assert create_from_default(loc) is False
    assert loc.file.read_bytes() == before


@pytest.mark.requires_posix  # POSIX mode bits
def test_the_copy_is_user_only(tmp_path):
    import stat

    from m3_core.config.writer import create_from_default

    loc = _loc(tmp_path / "cfg")
    create_from_default(loc)
    assert stat.S_IMODE(os.stat(loc.file).st_mode) == 0o600


def test_set_replaces_the_commented_default_and_unset_restores_it(tmp_path):
    """tomlkit alone appends `pool_size = 7` at the end of [db] and leaves
    `# pool_size = 5` above: two lines for one key. The writer edits in place."""
    from m3_core.config import REGISTRY, load
    from m3_core.config.default_file import verified_text
    from m3_core.config.writer import create_from_default, set_values

    loc = _loc(tmp_path)
    create_from_default(loc)
    key = REGISTRY.get("db.pool_size")
    set_values(loc, {key: 7})
    lines = loc.file.read_text(encoding="utf-8").splitlines()
    db = lines[lines.index("[db]") : lines.index("[db.postgres]")]
    assert db.count("pool_size = 7") == 1
    assert not any(ln.startswith("# pool_size = ") for ln in db), "the stale commented default stayed behind"
    i = lines.index("pool_size = 7")
    assert lines[i - 1].startswith("# type: integer"), "the value moved away from its doc"
    assert dict(load(loc, REGISTRY).values) == {"db.pool_size": 7}
    set_values(loc, {key: None})
    assert loc.file.read_text(encoding="utf-8") == verified_text()


def test_a_new_file_from_set_starts_from_the_default(tmp_path):
    from m3_core.config import REGISTRY, load
    from m3_core.config.writer import set_values

    loc = _loc(tmp_path)
    set_values(loc, {REGISTRY.get("search.row_cap"): 6000})
    text = loc.file.read_text(encoding="utf-8")
    assert text.startswith("# m3 configuration") and "# pool_size = 5" in text
    assert text.count("row_cap = 6000") == 1 and "# row_cap = " not in text
    assert dict(load(loc, REGISTRY).values) == {"search.row_cap": 6000}


def test_the_default_file_ships_in_the_wheel():
    """setup.py grafts every file of bin/ into the wheel ("*" per package); the
    default sits in bin/m3_core/config/, beside the module that locates it."""
    root = Path(__file__).resolve().parents[1]
    src = (root / "setup.py").read_text(encoding="utf-8")
    ns: dict = {}
    # Only the mapping function: running setup() itself would build.
    start, end = src.index("def migration_trees"), src.index("_pkg_dir, _payload_pkgs, _pkg_data")
    cwd = os.getcwd()
    try:
        os.chdir(root)
        exec(compile("import os\n" + src[start:end], "setup.py", "exec"), ns)  # noqa: S102  # nosec B102
        package_dir, packages, package_data = ns["_payload_mapping"]()
    finally:
        os.chdir(cwd)
    pkg = "m3_memory.bin.m3_core.config"
    assert pkg in packages and package_data[pkg] == ["*"]
    from m3_core.config.default_file import DEFAULT_FILE

    assert (root / package_dir[pkg] / DEFAULT_FILE.name).is_file()
    assert DEFAULT_FILE.parent == (root / package_dir[pkg]).resolve()


def test_the_environment_page_covers_every_name():
    """docs/ENVIRONMENT_VARIABLES.md names every env name the registry honours,
    every NOT_SETTINGS and legacy store name, and every retired one as retired."""
    from m3_core.config.keys import NOT_SETTINGS, REGISTRY, RETIRED
    from m3_core.config.stores import LEGACY_LOCATORS

    text = (Path(__file__).resolve().parents[1] / "docs" / "ENVIRONMENT_VARIABLES.md").read_text(encoding="utf-8")
    assert "GENERATED by bin/gen_config_docs.py" in text
    missing = sorted(
        n for n in (*REGISTRY.env_names(), *NOT_SETTINGS, *LEGACY_LOCATORS, *RETIRED) if f"`{n}`" not in text
    )
    assert not missing, missing
    for name in RETIRED:
        assert f"| `{name}` | retired: no effect |" in text
    for key in REGISTRY:
        for env in key.env_names:
            if key.klass.value == "config":
                assert f"| `{env}` | `[{key.table}]` | `{key.leaf}` |" in text, env
            else:
                assert f"| `{env}` | `{key.name}` in the secrets file |" in text, env
