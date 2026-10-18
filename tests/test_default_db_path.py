"""Which main store m3 resolves when nothing names one explicitly.

Roots come from m3_config.toml ``[paths]`` (the legacy M3_MEMORY_ROOT /
M3_ENGINE_ROOT / M3_DATABASE names are not read). A root the file chooses is
used as written, even while empty; only an install that chose nothing probes for
a populated store. The DSN guard rejects a connection string wherever the
"which SQLite file" answer came from.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import m3_sdk  # noqa: E402
from m3_core import paths as _paths  # noqa: E402


def _make_db(path: str, populated: bool) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    if populated:
        conn.execute("CREATE TABLE memory_items (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO memory_items(id) VALUES ('x')")
        conn.commit()
    # else: leave it as a 0-table stub
    conn.close()


def _config(tmp_path, monkeypatch, **paths_keys: str):
    """Point M3_CONFIG_ROOT at a fresh config dir whose [paths] holds exactly
    ``paths_keys`` (replacing the sandbox file, which pins every root)."""
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    body = "schema_version = 1\n[paths]\n" + "".join(
        f'{k} = "{os.fspath(v).replace(os.sep, "/")}"\n' for k, v in paths_keys.items()
    )
    (cfg / "m3_config.toml").write_text(body, encoding="utf-8", newline="\n")
    monkeypatch.setenv("M3_CONFIG_ROOT", str(cfg))
    return cfg


def _home(tmp_path, monkeypatch, *, populated_default: bool):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    default_db = home / ".m3" / "engine" / "agent_memory.db"
    if populated_default:
        _make_db(str(default_db), populated=True)
    return default_db


def test_db_is_populated(tmp_path):
    missing = str(tmp_path / "nope.db")
    stub = str(tmp_path / "stub.db")
    full = str(tmp_path / "full.db")
    _make_db(stub, populated=False)
    _make_db(full, populated=True)
    assert m3_sdk._db_is_populated(missing) is False
    assert m3_sdk._db_is_populated(stub) is False
    assert m3_sdk._db_is_populated(full) is True


def test_a_file_engine_root_is_honored_even_if_empty(tmp_path, monkeypatch):
    """A root set in [paths] is a deliberate choice; honor it verbatim even when
    empty (fresh deployment), without scanning for a populated store elsewhere."""
    _home(tmp_path, monkeypatch, populated_default=True)
    engine_root = tmp_path / "explicit_engine"
    _config(tmp_path, monkeypatch, engine_root=str(engine_root))
    resolved = m3_sdk._default_db_path()
    assert os.path.abspath(resolved) == os.path.abspath(str(engine_root / "agent_memory.db"))


def test_a_file_memory_root_derives_the_engine_over_a_populated_default(tmp_path, monkeypatch):
    """[paths] memory_root alone: the engine root derives from it
    (<memory_root>/engine), and that derived store is what m3 uses -- a populated
    ~/.m3/engine must not win over the root the file chose."""
    _home(tmp_path, monkeypatch, populated_default=True)
    root = tmp_path / "m3-root"
    _config(tmp_path, monkeypatch, memory_root=str(root))
    resolved = m3_sdk._default_db_path()
    assert os.path.abspath(resolved) == os.path.abspath(str(root / "engine" / "agent_memory.db")), resolved


def test_legacy_root_env_names_are_not_read(tmp_path, monkeypatch):
    """M3_MEMORY_ROOT / M3_ENGINE_ROOT set in the environment change nothing."""
    _home(tmp_path, monkeypatch, populated_default=False)
    engine_root = tmp_path / "file_engine"
    _config(tmp_path, monkeypatch, engine_root=str(engine_root))
    monkeypatch.setenv("M3_MEMORY_ROOT", str(tmp_path / "env-memory"))
    monkeypatch.setenv("M3_ENGINE_ROOT", str(tmp_path / "env-engine"))
    resolved = m3_sdk._default_db_path()
    assert os.path.abspath(resolved) == os.path.abspath(str(engine_root / "agent_memory.db"))


def test_fresh_install_no_data_returns_engine_path(tmp_path, monkeypatch):
    """No populated DB anywhere -> the engine path (created later)."""
    resolved = m3_sdk._default_db_path()
    expected = os.path.join(m3_sdk.get_m3_engine_root(), "agent_memory.db")
    assert os.path.abspath(resolved) == os.path.abspath(expected)


# ── DSN guard ────────────────────────────────────────────────────────────────
# Regression: resolve_db_path() ran os.path.abspath() on its candidate
# unconditionally, so a DSN became a path rooted at the CWD. On Windows that
# produced WinError 123 and wedged AgentOS_NotificationWaiter in a restart loop
# (2026-09-12); on POSIX the mangled name is a LEGAL path, so SQLite would have
# created an empty DB there and every read would have silently returned nothing.
# The main store is chosen by the --database pin now (M3_DATABASE is not read).


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://user:pw@db.example.invalid:5432/agent_memory",
        "postgres://u@h/db",
        "mysql://u@h/db",
        "mariadb://u@h/db",
        "sqlite:///tmp/x.db",
        "  POSTGRESQL://upper@h/db  ",  # whitespace + case must not evade the guard
    ],
)
def test_dsn_is_rejected_not_coerced(dsn, monkeypatch):
    monkeypatch.setattr(_paths, "_pinned_db", dsn)
    with pytest.raises(ValueError) as exc:
        m3_sdk.resolve_db_path(None)
    assert "--database pin" in str(exc.value)
    assert "db.backend" in str(exc.value), "must point at the right knob"


def test_dsn_error_does_not_leak_password(monkeypatch):
    """A DSN carries credentials; the message must not echo them."""
    monkeypatch.setattr(_paths, "_pinned_db", "postgresql://dbuser:hunter2@h:5432/db")
    with pytest.raises(ValueError) as exc:
        m3_sdk.resolve_db_path(None)
    assert "hunter2" not in str(exc.value)
    assert "dbuser" not in str(exc.value)


def test_a_dsn_in_m3_database_is_not_read(monkeypatch):
    """The legacy M3_DATABASE is not read, so even a DSN there changes nothing."""
    monkeypatch.setenv("M3_DATABASE", "postgresql://u@h/db")
    assert m3_sdk.resolve_db_path(None).endswith("agent_memory.db")


def test_explicit_dsn_argument_is_rejected(monkeypatch):
    with pytest.raises(ValueError) as exc:
        m3_sdk.resolve_db_path("postgresql://u@h/db")
    assert "explicit argument" in str(exc.value)


def test_windows_drive_path_is_not_mistaken_for_a_dsn(monkeypatch):
    r"""`C:\...` parses as scheme 'c' under urlparse — must still be a PATH.

    This is why the guard matches known DSN schemes instead of asking whether
    the value merely has a scheme.
    """
    monkeypatch.setattr(_paths, "_pinned_db", r"C:\Users\x\.m3\engine\agent_memory.db")
    assert m3_sdk.resolve_db_path(None).lower().endswith("agent_memory.db")


def test_ordinary_paths_still_resolve(tmp_path, monkeypatch):
    """The guard must not disturb the normal path."""
    p = tmp_path / "agent_memory.db"
    monkeypatch.setattr(_paths, "_pinned_db", str(p))
    assert m3_sdk.resolve_db_path(None) == os.path.abspath(str(p))
    # explicit arg wins over the pin, and is still absolutised
    other = tmp_path / "other.db"
    assert m3_sdk.resolve_db_path(str(other)) == os.path.abspath(str(other))


def test_relative_path_is_still_absolutised(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_paths, "_pinned_db", "rel/agent_memory.db")
    assert os.path.isabs(m3_sdk.resolve_db_path(None))


def _chosen_engine_root(tmp_path, monkeypatch):
    """A home with a POPULATED default store, and a config file choosing another
    engine root. Returns (default_db, chosen_root)."""
    default_db = _home(tmp_path, monkeypatch, populated_default=True)
    chosen = tmp_path / "chosen-engine"
    cfg = tmp_path / "cfg-chosen"
    cfg.mkdir()
    (cfg / "m3_config.toml").write_text(
        f'[paths]\nengine_root = "{chosen.as_posix()}"\n', encoding="utf-8", newline="\n"
    )
    monkeypatch.setenv("M3_CONFIG_ROOT", str(cfg))
    return default_db, chosen


def test_a_config_chosen_engine_root_wins_over_a_populated_default(tmp_path, monkeypatch):
    """Only the env pin counted as a choice: a root set in m3_config.toml lost to a
    populated ~/.m3/engine/agent_memory.db, so m3 used a store it was told not to
    (2026-10-17). A fresh chosen root is honoured even while empty."""
    default_db, chosen = _chosen_engine_root(tmp_path, monkeypatch)
    resolved = os.path.abspath(m3_sdk._default_db_path())
    assert resolved == os.path.abspath(str(chosen / "agent_memory.db")), resolved
    assert resolved != os.path.abspath(str(default_db))


def test_a_chosen_engine_root_is_not_replaced_by_a_legacy_file(tmp_path, monkeypatch):
    from m3_core import paths

    _default_db, chosen = _chosen_engine_root(tmp_path, monkeypatch)
    legacy = tmp_path / "home" / ".m3-memory" / "memory" / "state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}", encoding="utf-8")
    assert os.path.abspath(paths.resolve_engine_file("state.json")) == os.path.abspath(str(chosen / "state.json"))


def test_a_chosen_config_root_is_not_replaced_by_a_legacy_file(tmp_path, monkeypatch):
    from m3_core import paths

    _chosen_engine_root(tmp_path, monkeypatch)
    legacy = tmp_path / "home" / ".m3-memory" / "memory" / ".chatlog_config.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}", encoding="utf-8")
    got = paths.resolve_config_file(".chatlog_config.json")
    assert os.path.abspath(got) == os.path.abspath(str(tmp_path / "cfg-chosen" / ".chatlog_config.json")), got
