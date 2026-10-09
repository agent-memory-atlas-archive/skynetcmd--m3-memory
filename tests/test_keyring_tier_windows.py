"""auth_utils._keyring_tier on Windows: a keyring miss is a miss.

It used to probe `cmdkey /list:NAME` on every miss and log that the credential
"exists in Windows Credential Manager" -- for any name, because cmdkey exits 0
and echoes NAME when nothing is stored. It could never return the value.
"""
from __future__ import annotations


def test_a_windows_keyring_miss_runs_no_subprocess(monkeypatch):
    import auth_utils

    calls = []
    monkeypatch.setattr(auth_utils, "safe_keyring_get_password", lambda svc, key: None)
    monkeypatch.setattr(auth_utils.subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(auth_utils.sys, "platform", "win32")
    assert auth_utils._keyring_tier("SOME_TOKEN") == (None, None)
    assert calls == [], f"spawned {calls}"


def test_a_windows_keyring_error_is_reported(monkeypatch):
    import auth_utils

    def boom(svc, key):
        raise OSError("credential manager unavailable")

    monkeypatch.setattr(auth_utils, "safe_keyring_get_password", boom)
    monkeypatch.setattr(auth_utils.sys, "platform", "win32")
    val, err = auth_utils._keyring_tier("SOME_TOKEN")
    assert val is None and "credential manager unavailable" in err
