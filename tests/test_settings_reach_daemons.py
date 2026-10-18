"""A value set ONLY in m3_config.toml (or only in the secrets file) takes effect.

A daemon has no shell environment: a reader that consults env alone silently
ignores the configured value (measured for crypto.fips_mode, the same defect
class). Each test writes the sandbox's own config or secrets file, never env,
and asserts the reader follows it.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

from conftest import write_sandbox_config

_REPO = Path(__file__).resolve().parents[1]
_BIN = _REPO / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))


@pytest.fixture
def config_file():
    """Write the sandbox's m3_config.toml (the config root the sandbox points at)."""
    from m3_core.config import settings as st

    from m3_memory import _roots

    root = Path(_roots.config_root())
    root.mkdir(parents=True, exist_ok=True)

    def write(body: str) -> None:
        write_sandbox_config(body)

    yield write
    st._reset_for_tests()


# ── identity.node_id: the sync identity ──────────────────────────────────────
def test_node_id_comes_from_the_config_file(config_file):
    from m3_core.paths import node_id

    config_file('[identity]\nnode_id = "fleet-node-7"\n')
    assert node_id() == "fleet-node-7"


# ── dispatch.mode / db.postgres.dispatch_schema: dispatch routing ────────────
def test_dispatch_mode_comes_from_the_config_file(config_file):
    from m3_core.paths import dispatch_store_mode

    config_file('[dispatch]\nmode = "integrated"\n')
    assert dispatch_store_mode() == "integrated"


def test_dispatch_schema_comes_from_the_config_file(config_file):
    from m3_core.paths import dispatch_pg_schema

    config_file('[db.postgres]\ndispatch_schema = "m3_fleet_b"\n')
    assert dispatch_pg_schema() == "m3_fleet_b"


def test_an_unknown_dispatch_mode_is_refused_naming_the_key(config_file):
    """Never silently integrate on a typo: the registry refuses the value and the
    error names the key and the file."""
    from m3_core.config import ConfigFileError, format_config_error
    from m3_core.paths import dispatch_store_mode

    config_file('[dispatch]\nmode = "hybrid"\n')
    with pytest.raises(ConfigFileError) as caught:
        dispatch_store_mode()
    report = format_config_error(caught.value)
    assert "mode" in report and "hybrid" in report, report


# ── db.backend in the notification waiter ────────────────────────────────────
def test_waiter_follows_the_backend_in_the_config_file(config_file, monkeypatch):
    """The seam unimportable (stripped payload) but m3_core present: the waiter
    falls back to the selector, which reads [db] backend, not env. A PostgreSQL
    store set only in the file must take the polling lane, not watch a WAL file
    PostgreSQL never writes."""
    import builtins

    import m3_notification_waiter as waiter

    real_import = builtins.__import__

    def no_seam(name, *a, **k):
        if name.startswith("memory"):
            raise ImportError("simulated stripped payload")
        return real_import(name, *a, **k)

    config_file('[db]\nbackend = "postgres"\n')
    monkeypatch.setattr(builtins, "__import__", no_seam)
    monkeypatch.setattr(waiter, "_warn", lambda m: None)
    assert not waiter._has_change_signal("agent_memory.db")


# ── the warehouse host (installer reminder + sync_all) ───────────────────────
def test_warehouse_host_comes_from_the_config_file(config_file):
    from m3_core.paths import configured_warehouse_host

    config_file('[sync]\nwarehouse_host = "warehouse.example"\n')
    assert configured_warehouse_host() == "warehouse.example"


# ── log settings read at call time ──────────────────────────────────────────
def test_task_log_max_bytes_comes_from_the_config_file(config_file, monkeypatch):
    import _task_runtime

    config_file("[log]\nmax_bytes = 12345\n")
    monkeypatch.setattr(_task_runtime, "_log_config_path", lambda: str(_REPO / "no-such-log-config.json"))
    monkeypatch.setitem(_task_runtime._log_cfg_cache, "ts", 0.0)
    monkeypatch.setitem(_task_runtime._log_cfg_cache, "mtime", "force-reread")
    assert _task_runtime._log_max_bytes() == 12345


# ── secrets: from the secrets file, never env ────────────────────────────────
def test_openclaw_is_detected_from_the_secrets_file(monkeypatch, tmp_path):
    """The OpenClaw token is an m3 secret: its presence in the secrets file (the
    file lists every secret) is the detection signal, not an env var."""
    import shutil

    from m3_core import secret_store

    from m3_memory import setup_wizard

    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(setup_wizard, "_find_hermes_plugins_dir", lambda: None)
    assert setup_wizard._detect_agents().openclaw is False
    secret_store.set_secret("OPENCLAW_GATEWAY_TOKEN", "gw-token-value")
    assert setup_wizard._detect_agents().openclaw is True


def _install_os():
    spec = importlib.util.spec_from_file_location("_m3_install_os_secrets", _REPO / "install_os.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_install_os_hands_the_master_key_over_stdin_only(monkeypatch):
    """Not in argv, not in the child's environment: both are readable by other
    processes of the same user."""
    mod = _install_os()
    calls = []
    monkeypatch.setattr(mod, "run_cmd", lambda cmd, env=None, **kw: calls.append((cmd, env, kw)))
    mod._store_master_key("python-exe", "master-key-value")
    ((cmd, env, kw),) = calls
    assert env is None
    assert kw["stdin_text"] == "master-key-value"
    assert not any("master-key-value" in str(a) for a in cmd)
    assert cmd[-1] == str(_REPO / "bin")


def test_install_os_child_stores_through_secret_store(monkeypatch, capsys):
    """The child program goes through m3_core.secret_store (the sandbox blocks the
    keyring write, so it reports that failure) and never prints the value."""
    from m3_core import secret_store

    mod = _install_os()
    seen = []
    monkeypatch.setattr(secret_store, "store_in_keyring", lambda name, value: seen.append((name, value)) or "keyring:x")
    monkeypatch.setattr(sys, "argv", ["-c", str(_BIN)])
    monkeypatch.setattr(sys, "stdin", io.StringIO("master-key-value\n"))
    monkeypatch.setattr(sys, "path", list(sys.path))  # the program prepends bin/
    exec(compile(mod._STORE_MASTER_KEY, "<install_os child>", "exec"), {"__name__": "__main__"})  # noqa: S102
    assert seen == [("AGENT_OS_MASTER_KEY", "master-key-value")]
    assert "master-key-value" not in capsys.readouterr().out


def test_install_os_names_but_never_stores_a_key_seen_only_in_env(monkeypatch, capsys):
    """User decision a8083888 (a): a secret seen only in the process environment
    is listed, never stored."""
    mod = _install_os()
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO())  # no TTY: StringIO.isatty() is False
    monkeypatch.setattr(mod, "run_cmd", lambda *a, **k: pytest.fail("stored a secret taken from env"))
    monkeypatch.setenv("AGENT_OS_MASTER_KEY", "env-only-value")
    mod.setup_master_key("python-exe")
    out = capsys.readouterr().out
    assert "AGENT_OS_MASTER_KEY is set in the environment" in out
    assert "m3 secret set AGENT_OS_MASTER_KEY" in out
    assert "env-only-value" not in out
