"""The secrets file is plaintext (user decision, m3 c7082b20), so doctor warns when
it sits where it can leave the machine: a git working tree or a synced folder
(CONFIG_DESIGN §10). The synced-folder locations are OS facts (_platform)."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import simulate_os


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.delenv("OneDrive", raising=False)
    monkeypatch.delenv("OneDriveConsumer", raising=False)
    monkeypatch.delenv("OneDriveCommercial", raising=False)
    return h


def _roots():
    from m3_memory._platform import synced_folder_roots

    return [(s, Path(p)) for s, p in synced_folder_roots()]


def test_windows_roots_come_from_the_onedrive_variables_and_home(home, monkeypatch):
    od = home / "OneDrive - Contoso"
    od.mkdir()
    (home / "Dropbox").mkdir()
    monkeypatch.setenv("OneDriveCommercial", str(od))
    simulate_os(monkeypatch, "Windows")
    assert _roots() == [("OneDrive", od), ("Dropbox", home / "Dropbox")]


def test_macos_roots_include_icloud_and_each_cloudstorage_provider(home, monkeypatch):
    (home / "Library" / "Mobile Documents").mkdir(parents=True)
    (home / "Library" / "CloudStorage" / "OneDrive-Personal").mkdir(parents=True)
    simulate_os(monkeypatch, "Darwin")
    assert _roots() == [
        ("iCloud Drive", home / "Library" / "Mobile Documents"),
        ("OneDrive", home / "Library" / "CloudStorage" / "OneDrive-Personal"),
    ]


def test_linux_roots_are_the_usual_home_folders_and_only_existing_ones(home, monkeypatch):
    (home / "Dropbox").mkdir()
    simulate_os(monkeypatch, "Linux")
    assert _roots() == [("Dropbox", home / "Dropbox")]


@pytest.fixture
def secrets_file():
    from m3_core.secret_store import secrets_file_path

    p = Path(secrets_file_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('M3_PRIMARY_PG_URL = "postgresql://x/y"\n', encoding="utf-8")
    return p


def _section(capsys):
    from m3_memory.install.sections import _secrets_file_exposure_section

    _secrets_file_exposure_section()
    return capsys.readouterr().out


def test_silent_when_the_secrets_file_is_in_neither(secrets_file, monkeypatch, capsys):
    from m3_memory import _platform

    monkeypatch.setattr(_platform, "synced_folder_roots", lambda: [])
    assert _section(capsys) == ""


def test_a_secrets_file_in_a_git_tree_is_named(secrets_file, monkeypatch, capsys):
    from m3_memory import _platform

    monkeypatch.setattr(_platform, "synced_folder_roots", lambda: [])
    (secrets_file.parent / ".git").mkdir()
    out = _section(capsys)
    assert str(secrets_file) in out and "git working tree" in out and "secrets.file" in out
    assert "postgresql://x/y" not in out, "the value is never printed"


def test_a_secrets_file_in_a_synced_folder_is_named(secrets_file, monkeypatch, capsys):
    from m3_memory import _platform

    monkeypatch.setattr(_platform, "synced_folder_roots", lambda: [("OneDrive", str(secrets_file.parent.parent))])
    out = _section(capsys)
    assert "OneDrive" in out and "uploads it" in out


def test_no_secrets_file_is_silent(monkeypatch, capsys):
    from m3_memory import _platform

    monkeypatch.setattr(_platform, "synced_folder_roots", lambda: [("OneDrive", "/")])
    assert _section(capsys) == ""
