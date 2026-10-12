"""`m3 secret set NAME [--keyring]`: m3 ASKS for the value (hidden, twice); it is
never taken from the command line or printed. --keyring is a switch (user
design, 2026-10-12): the value goes to the OS keyring and leaves the file."""

from __future__ import annotations

import argparse
import sys

import pytest

NAME = "M3_PRIMARY_PG_URL"
VALUE = "postgresql://u:hunter2@h/db"


@pytest.fixture
def keyring(monkeypatch):
    """An in-memory keyring (the sandbox blocks real keyring writes)."""
    import auth_utils

    store: dict[str, str] = {}
    monkeypatch.setattr(auth_utils, "_keyring_store", lambda name, value: store.__setitem__(name, value))
    monkeypatch.setattr(
        auth_utils,
        "_keyring_tier",
        lambda name, *, service="system": (store.get(name) if service == "system" else None, None),
    )
    return store


@pytest.fixture
def person(monkeypatch):
    from m3_memory import _platform

    monkeypatch.setattr(_platform, "console_has_person", lambda: True)
    answers: list[str] = []
    monkeypatch.setattr("getpass.getpass", lambda prompt="": answers.pop(0))
    return answers


def _run(keyring_flag=False):
    from m3_memory import cli

    return cli._secret_set(argparse.Namespace(name=NAME, keyring=keyring_flag))


def test_the_value_is_asked_for_and_stored_in_the_file(person, capsys):
    from m3_core.secret_store import get_secret

    person += [VALUE, VALUE]
    assert _run() == 0
    assert get_secret(NAME) == VALUE
    out = capsys.readouterr().out
    assert "m3 now asks for the value" in out and "hunter2" not in out


def test_keyring_moves_it_out_of_the_file_and_leaves_a_pointer(person, keyring, capsys):
    """The file keeps listing the secret (m3 0be66297): a pointer replaces the value."""
    from m3_core.secret_store import KeyringRef, explain_named_secret, get_secret, read_secret_entries, set_secret

    set_secret(NAME, "postgresql://old@h/db")
    person += [VALUE, VALUE]
    assert _run(keyring_flag=True) == 0
    assert keyring[NAME] == VALUE and get_secret(NAME) is None
    assert read_secret_entries()[NAME] == KeyringRef("system", NAME)
    assert explain_named_secret(NAME).value == VALUE, "the pointer must resolve to the keyring copy"
    out = capsys.readouterr().out
    assert "read back OK" in out and "value line there was replaced" in out and "hunter2" not in out


@pytest.mark.parametrize("answers", [["", ""], [VALUE, VALUE + "x"]], ids=["empty", "mismatch"])
def test_nothing_is_stored_on_a_bad_entry(person, answers, capsys):
    from m3_core.secret_store import get_secret

    person += answers
    assert _run() == 2
    assert get_secret(NAME) is None and "nothing stored" in capsys.readouterr().err


def test_no_one_at_the_console_is_refused(monkeypatch, capsys):
    from m3_memory import _platform

    monkeypatch.setattr(_platform, "console_has_person", lambda: False)
    assert _run() == 2
    assert "no one is at the console" in capsys.readouterr().err


def test_an_unknown_name_is_refused(person, capsys):
    from m3_memory import cli

    assert cli._secret_set(argparse.Namespace(name="NOT_A_SECRET", keyring=False)) == 2
    assert "not one of m3's secrets" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["m3", "secret", "set", NAME, "--keyring", VALUE],
        ["m3", "secret", "set", NAME, f"--keyring={VALUE}"],
        ["m3", "secret", "set", NAME, VALUE],
    ],
    ids=["after-switch", "switch=value", "positional"],
)
def test_a_value_on_the_command_line_is_refused_and_never_echoed(monkeypatch, capsys, argv):
    from m3_memory import cli

    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as done:
        cli.main()
    err = capsys.readouterr().err
    assert done.value.code == 2 and "hunter2" not in err
    assert "asks" in err, err


def test_a_pasted_cmd_comment_is_explained(monkeypatch, capsys):
    from m3_memory import cli

    monkeypatch.setattr(sys, "argv", ["m3", "secret", "set", NAME, "--keyring", "#", "you", "will"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "cmd.exe does not treat # as a comment" in capsys.readouterr().err
