"""The sandbox never lets a test resolve a real DSN from the keyring or vault.

conftest's _no_warehouse_secret blocks the DSN names at the TIER level, below
everything that resolves secrets. Without it, resolve_pg_dsn(WAREHOUSE) read
SkyPC's real PG_URL from the OS keyring inside a test (2026-10-08).
"""
from __future__ import annotations

import pytest


@pytest.mark.parametrize("name", ["PG_URL", "M3_CDW_PG_URL", "M3_PRIMARY_PG_URL", "M3_PG_URL"])
def test_dsn_names_never_come_from_the_real_keyring(monkeypatch, name):
    import auth_utils

    # Simulate the developer's keyring HOLDING the DSN.
    monkeypatch.setattr(auth_utils, "safe_keyring_get_password",
                        lambda svc, key: "postgresql://real:secret@prod.example/db")
    assert auth_utils._keyring_tier(name) == (None, None)


def test_resolve_pg_dsn_cannot_reach_a_real_warehouse(monkeypatch):
    import auth_utils
    from m3_core.secret_store import PgRole, resolve_pg_dsn

    monkeypatch.setattr(auth_utils, "safe_keyring_get_password",
                        lambda svc, key: "postgresql://real:secret@prod.example/db")
    for var in ("M3_CDW_PG_URL", "PG_URL"):
        monkeypatch.delenv(var, raising=False)
    assert resolve_pg_dsn(PgRole.WAREHOUSE) == ""


def test_other_names_still_resolve(monkeypatch):
    import auth_utils

    monkeypatch.setattr(auth_utils, "safe_keyring_get_password", lambda svc, key: "token-value")
    assert auth_utils._keyring_tier("SOME_OTHER_TOKEN")[0] == "token-value"
