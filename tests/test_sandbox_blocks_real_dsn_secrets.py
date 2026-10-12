"""The sandbox never lets a test resolve a real DSN from the keyring or vault.

conftest's _no_warehouse_secret blocks the real OS keyring for every test (an
empty python-keyring backend, a stubbed macOS security CLI) and blocks every
registry secret at the TIER level, below everything that resolves secrets.
Without it, resolve_pg_dsn(WAREHOUSE) read SkyPC's real PG_URL from the OS
keyring inside a test (2026-10-08).
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("name", ["PG_URL", "M3_CDW_PG_URL", "M3_PRIMARY_PG_URL", "M3_PG_URL"])
def test_dsn_names_never_come_from_the_real_keyring(monkeypatch, name):
    import auth_utils

    # Simulate the developer's keyring HOLDING the DSN.
    monkeypatch.setattr(
        auth_utils, "_keyring_read", lambda svc, key: ("postgresql://real:secret@prod.example/db", None)
    )
    assert auth_utils._keyring_tier(name) == (None, None)


def test_resolve_pg_dsn_cannot_reach_a_real_warehouse(monkeypatch):
    import auth_utils
    from m3_core.secret_store import PgRole, resolve_pg_dsn

    monkeypatch.setattr(
        auth_utils, "_keyring_read", lambda svc, key: ("postgresql://real:secret@prod.example/db", None)
    )
    for var in ("M3_CDW_PG_URL", "PG_URL"):
        monkeypatch.delenv(var, raising=False)
    assert resolve_pg_dsn(PgRole.WAREHOUSE) == ""


def test_other_names_still_resolve(monkeypatch):
    import auth_utils

    monkeypatch.setattr(auth_utils, "_keyring_read", lambda svc, key: ("token-value", None))
    assert auth_utils._keyring_tier("SOME_OTHER_TOKEN")[0] == "token-value"


def test_no_test_reaches_the_real_keyring_or_keychain():
    """Every secret name, every path to the OS store: the empty backend for
    python keyring, a stubbed macOS security CLI, and the tier block. A test
    once printed the real vault master key once env stopped answering first."""
    import auth_utils
    import keyring
    from m3_core.secret_store import explain_named_secret

    assert type(keyring.get_keyring()).__name__ == "_EmptyKeyring"
    assert auth_utils._macos_keychain_read("AGENT_OS_MASTER_KEY") is None
    for name in ("AGENT_OS_MASTER_KEY", "ANTHROPIC_API_KEY", "a-dynamic-profile-service"):
        assert explain_named_secret(name).source is None, name


def test_no_test_inherits_a_secret_from_the_developers_environment():
    """The sandbox clears every m3 secret's env names, so a real key exported
    in the developer's shell never reaches a test or a child it spawns."""
    import os

    from m3_core.config import REGISTRY, KeyClass

    names = [n for k in REGISTRY if k.klass is KeyClass.SECRET for n in k.env_names]
    assert names and not [n for n in names if n in os.environ]
