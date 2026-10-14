"""A store that is selected but not set up is reported plainly, by every command.

Found in a trial install: after `m3 config set db.backend postgres` with no
database address stored, `m3 doctor` and `m3 status` crashed with a traceback,
and the switch itself said nothing about the missing address or the restart.
"""

from __future__ import annotations

import argparse
import importlib
import sys

import pytest
from m3_core.secret_store import set_secret


def _backends():
    return importlib.import_module("memory.backends")


def _factory(name):
    return importlib.import_module("memory.backends.registry").backend_factory_for(name)


def test_postgres_names_the_missing_address_and_both_ways_out():
    missing = _factory("postgres").not_configured()
    assert missing is not None
    assert "m3 secret set M3_PRIMARY_PG_URL" in missing
    assert "m3 config set db.backend sqlite" in missing


def test_postgres_with_an_address_needs_nothing():
    set_secret("M3_PRIMARY_PG_URL", "postgresql://u:p@127.0.0.1:5432/primary")
    assert _factory("postgres").not_configured() is None


def test_sqlite_needs_nothing():
    assert _factory("sqlite").not_configured() is None


def test_the_missing_address_raises_the_shared_exception():
    pb = importlib.import_module("memory.backends.postgres_backend")
    with pytest.raises(_backends().BackendNotConfigured):
        pb._resolve_dsn()


def test_switching_backend_says_what_is_missing_and_how_to_restart(capsys):
    import config_cli

    config_cli._after_backend_change("postgres")
    out = capsys.readouterr().out
    assert "not usable yet" in out and "m3 secret set M3_PRIMARY_PG_URL" in out
    assert "`m3 stop`, then `m3 setup`" in out


def test_switching_to_sqlite_reports_nothing_missing(capsys):
    import config_cli

    config_cli._after_backend_change("sqlite")
    out = capsys.readouterr().out
    assert "not usable yet" not in out
    assert "`m3 stop`, then `m3 setup`" in out


def test_a_command_hitting_it_prints_the_message_not_a_traceback(monkeypatch, capsys):
    from m3_memory import cli

    def unconfigured(_args: argparse.Namespace) -> int:
        raise _backends().BackendNotConfigured("m3 is set to use PostgreSQL, but no database address is stored")

    monkeypatch.setattr(cli, "_cmd_status", unconfigured)
    monkeypatch.setattr(sys, "argv", ["m3", "status"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "no database address is stored" in err
    assert "Traceback" not in err


def test_other_errors_still_raise(monkeypatch):
    from m3_memory import cli

    def broken(_args: argparse.Namespace) -> int:
        raise RuntimeError("a real bug")

    monkeypatch.setattr(cli, "_cmd_status", broken)
    monkeypatch.setattr(sys, "argv", ["m3", "status"])
    with pytest.raises(RuntimeError, match="a real bug"):
        cli.main()


def test_a_missing_keyring_reads_as_none_not_as_install_advice(monkeypatch):
    """On a headless box the keyring library's error suggests installing a
    fallback package; m3 does not need one, so the lookup note says only that
    there is no keyring."""
    import auth_utils
    import keyring

    def no_backend(*_a, **_k):
        raise keyring.errors.NoKeyringError("No recommended backend was available. Install keyrings.alt")

    monkeypatch.setattr(keyring, "get_password", no_backend)
    value, err = auth_utils._keyring_read("system", "M3_PRIMARY_PG_URL")
    assert value is None
    assert err == "keyring: none on this computer"
