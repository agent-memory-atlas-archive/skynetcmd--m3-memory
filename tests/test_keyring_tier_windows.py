"""auth_utils._keyring_tier: a miss is a miss, and a broken keyring says so.

On Windows it used to probe `cmdkey /list:NAME` on every miss and log that the
credential "exists in Windows Credential Manager" -- for any name, because
cmdkey exits 0 and echoes NAME when nothing is stored. It could never return
the value.

And on every OS a keyring that RAISED came back as (None, None), identical to
"not stored": safe_keyring_get_password swallowed the failure at debug, so the
DSN resolver's "tiers reported" never named it (m3 2f4bad6c). These tests drive
a real keyring backend, not a faked tier, so they cannot pass on a fake alone.
"""
from __future__ import annotations

import keyring
import keyring.backend
import pytest


class _Backend(keyring.backend.KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self, get):
        super().__init__()
        self._get = get

    def get_password(self, service, username):
        return self._get(service, username)

    def set_password(self, service, username, password):
        raise AssertionError("a test wrote the keyring")

    def delete_password(self, service, username):
        raise AssertionError("a test deleted from the keyring")


@pytest.fixture
def backend(monkeypatch):
    """Install a test backend for the duration of one test, then restore."""
    import auth_utils

    real = keyring.get_keyring()
    monkeypatch.setattr(auth_utils, "_KEYRING_CB_OPEN_UNTIL", 0.0)

    def use(get):
        keyring.set_keyring(_Backend(get))

    yield use
    keyring.set_keyring(real)


def test_a_windows_keyring_miss_runs_no_subprocess(monkeypatch, backend):
    import auth_utils

    backend(lambda s, u: None)
    calls = []
    monkeypatch.setattr(auth_utils.subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(auth_utils.sys, "platform", "win32")
    assert auth_utils._keyring_tier("SOME_TOKEN") == (None, None)
    assert calls == [], f"spawned {calls}"


def test_a_crashing_keyring_is_reported_not_read_as_unset(monkeypatch, backend):
    import auth_utils

    def boom(s, u):
        raise RuntimeError("credential manager unavailable")

    backend(boom)
    monkeypatch.setattr(auth_utils.sys, "platform", "win32")
    val, err = auth_utils._keyring_tier("SOME_TOKEN")
    assert val is None and err and "credential manager unavailable" in err, err


def test_the_dsn_miss_names_a_crashing_keyring(monkeypatch, backend):
    """End to end through the owner, with only the sandbox's DSN block lifted."""
    import auth_utils
    from m3_core.secret_store import PgRole, explain_pg_dsn

    def locked(s, u):
        raise RuntimeError("keychain locked")

    backend(locked)
    # Lift the sandbox's DSN block: this test's own backend holds nothing real.
    monkeypatch.setattr(auth_utils, "_keyring_tier", auth_utils._keyring_tier.__wrapped__)
    monkeypatch.setattr(auth_utils, "_file_tier", lambda s: (None, None))
    monkeypatch.setattr(auth_utils, "_vault_tier", lambda s: (None, None))
    for var in ("M3_PRIMARY_PG_URL", "M3_PG_URL"):
        monkeypatch.delenv(var, raising=False)
    r = explain_pg_dsn(PgRole.PRIMARY)
    assert r.dsn == "" and any("keychain locked" in e for e in r.errors), r.errors
