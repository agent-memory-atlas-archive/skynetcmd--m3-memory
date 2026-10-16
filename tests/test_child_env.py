"""child_env: the one owner of a child process's environment."""

from __future__ import annotations

import os
import typing

from m3_memory._childenv import ChildEnv, child_env


def test_the_parent_environment_is_never_modified(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "parent")
    before = dict(os.environ)
    child_env(PYTHONUTF8="1", prepend_pythonpath="BIN", drop=["PATH"])
    assert dict(os.environ) == before


def test_overrides_drop_and_pythonpath_prepend(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "parent")
    env = child_env(PYTHONUTF8="1", prepend_pythonpath="BIN", drop=["PATH"])
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONPATH"] == "BIN" + os.pathsep + "parent"
    assert "PATH" not in env


def test_prepend_without_an_existing_pythonpath(monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    assert child_env(prepend_pythonpath="BIN")["PYTHONPATH"] == "BIN"


def test_the_overrides_are_typed_non_m3_names_only():
    """PEP 692: the override set is closed. An m3 setting is a CLI argument for an
    m3 child (spawn_m3_child), never an env override."""
    names = set(typing.get_type_hints(ChildEnv))
    assert names and not any(n.startswith("M3_") for n in names), names


def test_pg_tools_get_no_m3_secret(monkeypatch):
    """pg_dump / pg_restore are not m3: an env secret override must not reach them."""
    from memory.backends import postgres_backend as pb

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    _conninfo, env = pb._conninfo_and_env("postgresql://u:pw@h:5432/db")
    assert "ANTHROPIC_API_KEY" not in env
    assert env["PGPASSWORD"] == "pw"


def test_no_m3_setting_is_handed_to_a_child_through_env():
    """The one sanctioned handoff (m3-embed-server's GGUF and port) is gone: the
    server reads m3_config.toml, named by --config (test_embed_server_config_file)."""
    import inspect

    assert "handoff" not in inspect.signature(child_env).parameters
