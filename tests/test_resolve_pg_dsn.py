"""m3_core.secret_store.resolve_pg_dsn / explain_pg_dsn: the one DSN owner.

The keyring and vault tiers are replaced by fakes keyed by name, so no real
keyring or vault is touched; the secrets file is the sandbox's own. The
environment is never a tier.
"""

from __future__ import annotations

import logging

import pytest

PRIMARY = "postgresql://u:p@primary.example/m3"
WAREHOUSE = "postgresql://u:p@warehouse.example/cdw"


@pytest.fixture
def tiers(monkeypatch):
    """Fake keyring (``system/<name>`` entries keyed by name) and vault: an
    Exception value is that tier's error."""
    import auth_utils

    store = {"keyring": {}, "vault": {}}

    def make(kind):
        def read(name, *, service="system"):
            v = store[kind].get(name) if service == "system" else None
            if isinstance(v, Exception):
                return None, f"{kind}: {type(v).__name__}: {v}"
            return v, None

        return read

    monkeypatch.setattr(auth_utils, "_keyring_tier", make("keyring"))
    monkeypatch.setattr(auth_utils, "_vault_tier", make("vault"))
    return store


def _explain(role):
    from m3_core.secret_store import PgRole, explain_pg_dsn

    return explain_pg_dsn(PgRole(role))


def test_env_is_never_read_and_the_miss_names_it(tiers, monkeypatch):
    monkeypatch.setenv("M3_PG_URL", PRIMARY)
    r = _explain("primary")
    assert r.dsn == "" and r.source is None
    said = " | ".join(r.errors)
    assert "M3_PG_URL is set in the environment" in said and "m3 secret set M3_PRIMARY_PG_URL" in said, said
    assert PRIMARY not in said and "u:p@" not in said


def test_file_then_keyring(tiers):
    from m3_core.secret_store import set_secret

    tiers["keyring"]["M3_PRIMARY_PG_URL"] = PRIMARY
    r = _explain("primary")
    assert (r.dsn, r.source) == (PRIMARY, "keyring:system/M3_PRIMARY_PG_URL")
    set_secret("M3_PRIMARY_PG_URL", "postgresql://from-file/x")
    r = _explain("primary")
    assert (r.dsn, r.source) == ("postgresql://from-file/x", "secrets-file:M3_PRIMARY_PG_URL")


def test_primary_from_the_vault_works_but_warns_every_time(tiers, caplog):
    """D1: a deprecated fallback through N+3, never silent."""
    tiers["vault"]["M3_PRIMARY_PG_URL"] = PRIMARY
    with caplog.at_level(logging.WARNING):
        r1 = _explain("primary")
        r2 = _explain("primary")
    assert r1.dsn == r2.dsn == PRIMARY and r1.source == "vault:M3_PRIMARY_PG_URL"
    assert caplog.text.count("deprecated") == 2
    # Actionable (§5): names the key and a repair that keeps the DSN out of history.
    assert "m3 secret set M3_PRIMARY_PG_URL" in caplog.text
    assert PRIMARY not in caplog.text, "the warning echoed the DSN"


def test_warehouse_from_the_legacy_vault_key_is_not_a_warning(tiers, caplog):
    tiers["vault"]["PG_URL"] = WAREHOUSE
    with caplog.at_level(logging.WARNING):
        r = _explain("warehouse")
    assert (r.dsn, r.source) == (WAREHOUSE, "vault:PG_URL")
    assert "deprecated" not in caplog.text


def test_roles_never_cross(tiers):
    from m3_core.secret_store import set_secret

    set_secret("M3_CDW_PG_URL", WAREHOUSE)
    tiers["keyring"]["PG_URL"] = WAREHOUSE
    assert _explain("primary").dsn == "", "the warehouse DSN reached the primary role"
    tiers["keyring"].clear()
    set_secret("M3_PRIMARY_PG_URL", PRIMARY)
    tiers["vault"]["M3_PRIMARY_PG_URL"] = PRIMARY
    assert _explain("warehouse").dsn == WAREHOUSE, "the warehouse lost its own DSN"


def test_a_miss_explains_what_each_tier_hit(tiers):
    """A broken keyring must not read as 'not set'."""
    tiers["keyring"]["M3_PRIMARY_PG_URL"] = RuntimeError("keyring backend crashed")
    tiers["vault"]["M3_PRIMARY_PG_URL"] = ValueError("salt missing")
    r = _explain("primary")
    assert r.dsn == "" and r.source is None
    assert any("keyring backend crashed" in e for e in r.errors), r.errors
    assert any("salt missing" in e for e in r.errors), r.errors


def test_warehouse_migrator_finds_a_stored_dsn(tiers):
    """It read env only, so a DSN the installer stored was 'missing'."""
    import migrate_warehouse_to_schema as mw

    tiers["keyring"]["M3_CDW_PG_URL"] = WAREHOUSE
    assert mw._resolve_dsn(None) == WAREHOUSE
    assert mw._resolve_dsn("postgresql://explicit/x") == "postgresql://explicit/x"


def test_warehouse_migrator_miss_names_each_tier(tiers, capsys):
    import migrate_warehouse_to_schema as mw

    tiers["keyring"]["M3_CDW_PG_URL"] = RuntimeError("keyring backend crashed")
    with pytest.raises(SystemExit) as exc:
        mw._resolve_dsn(None)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "keyring backend crashed" in err and "--dsn" in err and "m3 secret set M3_CDW_PG_URL" in err, err


def test_resolve_returns_the_string(tiers):
    from m3_core.secret_store import PgRole, resolve_pg_dsn, set_secret

    assert resolve_pg_dsn(PgRole.PRIMARY) == ""
    set_secret("M3_PRIMARY_PG_URL", f"  {PRIMARY}  ")
    assert resolve_pg_dsn(PgRole.PRIMARY) == PRIMARY
