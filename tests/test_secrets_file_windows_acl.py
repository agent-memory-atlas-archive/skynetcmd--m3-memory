"""The secrets file's user-only rule on Windows: the DACL, not mode bits.

atomic_write's 0600 means nothing on Windows; the file takes its directory's
inheritable ACL. So a write refuses BEFORE any plaintext lands when a new file
there would be readable by others, and a read refuses such a file.
"""

from __future__ import annotations

import subprocess

import pytest

windows_only = pytest.mark.requires_windows  # Windows DACLs

USER = "S-1-5-21-1-2-3-1001"


@pytest.mark.parametrize(
    "sddl,expected",
    [
        # The profile default (measured on SkyPC): user, SYSTEM, Administrators.
        (f"O:{USER}D:(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICIID;FA;;;{USER})", []),
        # C:\Users\Public: interactive/service/batch logons may read and write.
        ("O:SYD:PAI(A;OICI;FA;;;BA)(A;OICIIO;FA;;;CO)(A;OICI;FA;;;SY)(A;;0x1200af;;;IU)", ["IU (Interactive users)"]),
        # Inherit-only and deny aces do not grant access to the object itself.
        (f"O:{USER}D:(A;OICIIO;FA;;;BU)(D;;FA;;;WD)(A;;FA;;;{USER})", []),
        # Another user's SID is foreign, by SID.
        (f"O:{USER}D:(A;;FA;;;{USER})(A;;FR;;;S-1-5-21-1-2-3-1002)", ["S-1-5-21-1-2-3-1002"]),
        # No DACL at all grants everyone.
        (f"O:{USER}D:NO_ACCESS_CONTROL", ["Everyone (no DACL)"]),
    ],
)
def test_foreign_principals(sddl, expected):
    from m3_core.win_acl import foreign_principals

    assert foreign_principals(sddl, [USER]) == expected


def test_the_builtin_administrator_is_recognised_by_its_alias():
    """SDDL writes the RID-500 account as LA, never as its SID (measured on
    SkyPC; GitHub's Windows runner runs as that account)."""
    from m3_core.win_acl import foreign_principals

    admin, sddl = "S-1-5-21-1-2-3-500", "O:LAD:PAI(A;;FA;;;LA)(A;;FA;;;SY)"
    assert foreign_principals(sddl, [admin, "LA"]) == []
    assert foreign_principals(sddl, [admin]) == ["LA"], "by its SID alone, its own ace looks foreign"
    assert foreign_principals(sddl, [USER]) == ["LA"], "for anyone else, the Administrator is not the user"
    with pytest.raises(TypeError, match="aliases"):
        foreign_principals(sddl, admin)  # a bare str would match by substring


@windows_only
def test_la_resolves_to_this_machines_administrator_and_names_only_that_user():
    from m3_core.win_acl import _alias_sid, current_user_sid, user_aliases

    admin = _alias_sid("LA")
    assert admin and admin.startswith("S-1-5-21-") and admin.endswith("-500"), admin
    assert user_aliases(admin) == ["LA"]
    me = current_user_sid()
    assert ("LA" in user_aliases(me)) == (me == admin)
    assert user_aliases("S-1-5-18") == []


def _grant_users_read(path):
    # Inheritance flags only on a directory: on a FILE, icacls reports success
    # for an (OI)(CI) grant and adds nothing (measured 2026-10-08).
    flags = "(OI)(CI)" if path.is_dir() else ""
    subprocess.run(["icacls", str(path), "/grant", f"*S-1-5-32-545:{flags}R"], check=True, capture_output=True)


@windows_only
def test_a_write_into_a_shared_directory_is_refused_before_any_plaintext(monkeypatch, tmp_path):
    from m3_core import secret_store

    shared = tmp_path / "shared-config"
    shared.mkdir()
    _grant_users_read(shared)
    monkeypatch.setenv("M3_CONFIG_ROOT", str(shared))
    with pytest.raises(secret_store.SecretsFileError) as exc:
        secret_store.set_secret("M3_PRIMARY_PG_URL", "postgresql://u:pw@h/db")
    msg = str(exc.value)
    assert "NOT written" in msg and "Users" in msg and "icacls" in msg, msg
    assert [p.name for p in shared.iterdir() if not p.name.endswith(".lock")] == [], (
        "plaintext (or the ACL probe) was left behind"
    )


@windows_only
def test_a_private_directory_works_and_the_file_reads_back(monkeypatch, tmp_path):
    from m3_core import secret_store

    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    secret_store.set_secret("M3_SERVE_TOKEN", "1")
    assert secret_store.get_secret("M3_SERVE_TOKEN") == "1"


@windows_only
def test_a_file_others_can_open_is_refused_on_read(monkeypatch, tmp_path):
    from m3_core import secret_store

    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    p = secret_store.set_secret("M3_SERVE_TOKEN", "1")
    _grant_users_read(p)
    with pytest.raises(secret_store.SecretsFileError, match=r"Users.*icacls"):
        secret_store.read_secrets()


def test_fix_hint_names_the_user_not_a_cmd_variable(monkeypatch):
    """%USERNAME% expands in cmd.exe only; pasted into PowerShell it grants a
    principal that does not exist. Every icacls hint comes from one owner."""
    import getpass

    from m3_core.win_acl import icacls_user_only

    monkeypatch.setattr(getpass, "getuser", lambda: "alex")
    assert icacls_user_only("C:/x/f") == 'icacls "C:/x/f" /inheritance:r /grant:r "alex:F" /grant:r "SYSTEM:F"'
    assert '"alex:(OI)(CI)F"' in icacls_user_only("C:/x", directory=True)
