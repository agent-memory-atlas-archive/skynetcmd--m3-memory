"""The secrets file is plaintext, so doctor warns when
it sits where it can leave the machine: a git working tree or a synced folder
(CONFIG_DESIGN §10). The synced-folder locations are OS facts (_platform)."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import simulate_os
from m3_memory import _platform as _plat

# The real seam function, before the autouse fake below replaces it per test.
_REAL_VOLUME_ENCRYPTION = _plat.volume_encryption


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
    from m3_core.secret_store import set_secret

    set_secret("M3_PRIMARY_PG_URL", "postgresql://x/y")  # the real writer: [secrets] table, user-only
    return p


@pytest.fixture(autouse=True)
def _disk_not_checked(monkeypatch):
    """Each test chooses the disk-encryption fact itself (this box's real C: has
    BitLocker off, which would fire in every test); the default is "not checked"."""
    from m3_memory import _platform

    monkeypatch.setattr(_platform, "volume_encryption", lambda path: None)


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


def _fake_volume(monkeypatch, encrypted, detail="C: BitLocker is off"):
    from m3_memory import _platform

    monkeypatch.setattr(
        _platform,
        "volume_encryption",
        lambda path: _platform.VolumeEncryption(encrypted, detail, "Windows Credential Manager"),
    )
    monkeypatch.setattr(_platform, "synced_folder_roots", lambda: [])


def test_an_unencrypted_disk_names_each_secret_and_the_command(secrets_file, monkeypatch, capsys):
    _fake_volume(monkeypatch, False)
    out = _section(capsys)
    assert "encrypted (C: BitLocker is off)" in out and "is not" in out
    assert "m3 secret set M3_PRIMARY_PG_URL --keyring" in out
    assert "NOT the secret" in out and "asks you for the value" in out
    assert "postgresql://x/y" not in out


def test_an_encrypted_disk_is_silent(secrets_file, monkeypatch, capsys):
    _fake_volume(monkeypatch, True, "C: BitLocker is on")
    assert _section(capsys) == ""


def test_an_unreadable_status_says_so_once(secrets_file, monkeypatch, capsys):
    _fake_volume(monkeypatch, None, "C: BitLocker status not read (code '5')")
    out = _section(capsys)
    assert "could not tell" in out and "code '5'" in out and "m3 secret set" not in out


@pytest.mark.parametrize(
    "os_name, probe, expected",
    [
        ("Windows", (0, "1"), True),
        ("Windows", (0, "2"), False),
        ("Windows", (0, "5"), None),
        ("Darwin", (0, "true"), True),
        ("Darwin", (1, "false"), False),
        ("Linux", None, "not checked"),
    ],
)
def test_the_os_seam_reads_bitlocker_and_filevault(monkeypatch, os_name, probe, expected):
    from m3_memory import _platform

    simulate_os(monkeypatch, os_name)
    monkeypatch.setattr(_platform, "_probe", lambda argv: probe)
    got = _REAL_VOLUME_ENCRYPTION("C:/x" if os_name == "Windows" else "/x")
    if expected == "not checked":
        assert got is None
    else:
        assert got is not None and got.encrypted is expected


def test_brief_doctor_says_it_in_one_line_and_points_at_verbose(secrets_file, monkeypatch, capsys):
    from m3_memory.install.sections import _secrets_file_exposure_section

    _fake_volume(monkeypatch, False)
    _secrets_file_exposure_section(brief=True)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1, lines
    assert lines[0].lstrip().startswith("[!]") and "m3 doctor --verbose" in lines[0]
    assert "m3 secret set" not in lines[0]


def test_brief_unknown_status_is_information_not_a_warning(secrets_file, monkeypatch, capsys):
    from m3_memory.install.sections import _secrets_file_exposure_section

    _fake_volume(monkeypatch, None, "C: BitLocker status not read (code '5')")
    _secrets_file_exposure_section(brief=True)
    assert capsys.readouterr().out.strip().startswith("[i]")
