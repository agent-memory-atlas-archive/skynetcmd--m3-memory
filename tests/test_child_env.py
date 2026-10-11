"""child_env: the one owner of a child process's environment (CONFIG_DESIGN §5, 2E.1)."""

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


def test_a_handoff_carries_only_the_names_it_sanctions():
    import pytest

    from m3_memory._childenv import PG_BOOTSTRAP_SECRET

    env = child_env(
        handoff=(PG_BOOTSTRAP_SECRET, {"M3_DB_BACKEND": "postgres", "M3_PRIMARY_PG_URL": "postgresql://h/d"})
    )
    assert env["M3_PRIMARY_PG_URL"] == "postgresql://h/d"
    with pytest.raises(ValueError, match="does not sanction"):
        child_env(handoff=(PG_BOOTSTRAP_SECRET, {"M3_DATABASE": "x"}))


def test_the_sanctioned_handoffs_are_reviewed():
    """Adding a handoff, or a name to one, must change this list: env is not a
    general channel for m3 settings (2E.1 part 2, m3 9ecda06b)."""
    from m3_memory._childenv import HANDOFFS

    assert {h.name: sorted(h.names) for h in HANDOFFS} == {
        "rust-embed-server": ["M3_EMBED_GGUF", "M3_EMBED_SERVER_PORT"],
        "pg-bootstrap-secret": ["M3_DB_BACKEND", "M3_PRIMARY_PG_URL"],
    }


def test_the_embed_server_gets_the_configured_port(m3_settings, tmp_path):
    """The port was hardcoded to 8082: a port set in m3_config.toml moved m3's own
    probe but never the server."""
    from m3_memory import embedder_admin

    m3_settings({"embed.server.port": 9123})
    env = embedder_admin._embed_server_env(tmp_path / "m.gguf")
    assert env["M3_EMBED_SERVER_PORT"] == "9123"
    assert env["M3_EMBED_GGUF"] == str(tmp_path / "m.gguf")
