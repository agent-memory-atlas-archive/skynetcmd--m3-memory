"""After `pipx upgrade` nothing said `m3 setup` was still to run, and config.json
kept naming the old version even after setup (measured upgrading 2026.6.1.0).
setup now records its version; until it has, every m3 command but the setup
ones says so in one stderr line."""

from __future__ import annotations

import json

import pytest

from m3_memory import __version__


@pytest.fixture
def record(tmp_path, monkeypatch):
    from m3_memory import installer

    monkeypatch.setattr(installer, "config_dir", lambda: tmp_path)
    return installer, tmp_path / "config.json"


def test_an_upgrade_is_pending_until_setup_records_this_version(record):
    installer, f = record
    assert installer.setup_pending() is None, "nothing recorded: nothing to say"
    f.write_text(json.dumps({"version": "2026.6.1.0", "repo_path": "/old/repo"}), encoding="utf-8")
    assert installer.setup_pending() == ("2026.6.1.0", __version__)
    installer.record_setup_done()
    assert installer.setup_pending() is None
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["version"] == __version__ and data["repo_path"] != "/old/repo"


@pytest.mark.parametrize("command, noted", [("status", True), ("doctor", True), ("setup", False), ("upgrade", False)])
def test_the_note_is_shown_except_by_the_commands_that_finish_setup(record, monkeypatch, capsys, command, noted):
    from m3_memory import cli

    _installer, f = record
    f.write_text(json.dumps({"version": "2026.6.1.0"}), encoding="utf-8")
    if cli._should_note_setup_pending(command):
        cli._note_setup_pending()
    err = capsys.readouterr().err
    assert ("finish with `m3 setup`" in err) is noted
    assert (command in cli._FINISHES_SETUP) is not noted


def test_a_child_of_m3_upgrade_does_not_say_to_run_setup(record, monkeypatch, capsys):
    """`m3 upgrade` runs `m3 stop` and setup runs m3 subcommands; each printed
    "finish with `m3 setup`" while the upgrade was running that setup."""
    from m3_memory import cli
    from m3_memory.wizard import ui

    _installer, f = record
    f.write_text(json.dumps({"version": "2026.6.1.0"}), encoding="utf-8")
    monkeypatch.setattr(ui, "_caller", "upgrade")
    assert cli._should_note_setup_pending("stop") is False
    monkeypatch.setattr(ui, "_caller", None)
    assert cli._should_note_setup_pending("stop") is True
