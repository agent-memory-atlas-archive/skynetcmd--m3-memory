"""OS-seam primitives added 2026-10-10 decide on the capability, on every OS.

current_uid, new_process_group_kwargs and the console-key readers each moved a
copied OS fact out of business logic (secret_store, config_parity,
embed_server_gpu, chatlog_status). Both branches of each run here wherever the
suite runs.
"""

from __future__ import annotations

import os
import subprocess


def test_current_uid_follows_os_getuid(monkeypatch):
    from m3_memory import _platform

    monkeypatch.setattr(os, "getuid", lambda: 4242, raising=False)
    assert _platform.current_uid() == 4242
    monkeypatch.delattr(os, "getuid", raising=False)
    assert _platform.current_uid() is None


def test_new_process_group_kwargs_follows_the_flag(monkeypatch):
    from m3_memory import _platform

    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    assert _platform.new_process_group_kwargs() == {"creationflags": 0x200}
    monkeypatch.delattr(subprocess, "CREATE_NEW_PROCESS_GROUP", raising=False)
    assert _platform.new_process_group_kwargs() == {}


def test_read_key_uses_msvcrt_where_it_exists(monkeypatch):
    from m3_memory import _platform

    class _Console:
        def __init__(self):
            self.keys = [b"Q"]

        def kbhit(self):
            return bool(self.keys)

        def getch(self):
            return self.keys.pop(0)

    monkeypatch.setattr(_platform, "_msvcrt", lambda: _Console())
    assert _platform.read_key(1.0) == "q"


def test_read_key_without_a_tty_times_out(monkeypatch):
    from m3_memory import _platform

    if _platform._msvcrt() is not None:
        monkeypatch.setattr(_platform, "_msvcrt", lambda: None)
    monkeypatch.setattr(os, "isatty", lambda fd: False)
    assert _platform.read_key(0.01) is None
