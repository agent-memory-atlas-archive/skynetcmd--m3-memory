"""The writer scan counts only this user's processes.

Another user's m3 cannot hold this user's store. On a shared Linux machine the
scan used to count it anyway, so a new user's `m3 setup` waited 30 s for the
other user's cognitive loop, offered to kill it (the default answer), and asked
for a sudo password to do so. Only a process that is definitely someone else's
is skipped: one whose owner the OS will not reveal (an elevated process of our
own on Windows) is still counted, so a real blocker is never missed.
"""

from __future__ import annotations

import os
import sys

import pytest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

_LOOP = ["/home/u/.local/share/pipx/venvs/m3-memory/bin/python", "/home/u/.m3/bin/m3_cognitive_loop.py"]


class _Uids:
    def __init__(self, real):
        self.real = real


class _Proc:
    def __init__(self, pid, cmdline, *, uid=None, user=None, deny=False):
        self.info = {"pid": pid, "name": "python", "cmdline": cmdline}
        self._uid, self._user, self._deny = uid, user, deny

    def uids(self):
        if self._deny:
            raise PermissionError("access denied")
        return _Uids(self._uid)

    def username(self):
        if self._deny:
            raise PermissionError("access denied")
        return self._user


@pytest.fixture
def platform():
    import importlib

    return importlib.import_module("m3_memory._platform")


def test_posix_owner_is_compared_by_uid(platform, monkeypatch):
    monkeypatch.setattr(platform, "current_uid", lambda: 1000)
    assert platform.owned_by_current_user(_Proc(1, [], uid=1000)) is True
    assert platform.owned_by_current_user(_Proc(1, [], uid=1001)) is False


def test_windows_owner_is_compared_by_account_name(platform, monkeypatch):
    import getpass

    monkeypatch.setattr(platform, "current_uid", lambda: None)
    monkeypatch.setattr(getpass, "getuser", lambda: "alice")
    assert platform.owned_by_current_user(_Proc(1, [], user="DESKTOP-1\\Alice")) is True
    assert platform.owned_by_current_user(_Proc(1, [], user="DESKTOP-1\\bob")) is False


def test_an_owner_the_os_will_not_reveal_is_unknown(platform, monkeypatch):
    monkeypatch.setattr(platform, "current_uid", lambda: 1000)
    assert platform.owned_by_current_user(_Proc(1, [], deny=True)) is None


def test_the_scan_skips_only_another_users_writer(monkeypatch, tmp_path):
    import m3_halt
    import psutil

    procs = [
        _Proc(101, _LOOP, uid=1000),  # mine
        _Proc(102, _LOOP, uid=2000),  # another user's: must not count
        _Proc(103, _LOOP, deny=True),  # owner unreadable: still counted
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))
    monkeypatch.setattr(m3_halt, "owned_by_current_user", lambda p: {101: True, 102: False, 103: None}[p.info["pid"]])
    monkeypatch.setattr(m3_halt, "_is_venv_launcher_stub", lambda p: False)
    found = {p.pid for p in m3_halt.scan_db_writer_processes(str(tmp_path))}
    assert found == {101, 103}
