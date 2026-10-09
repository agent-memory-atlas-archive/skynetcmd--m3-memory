"""m3_core.secret_store.resolve_pg_dsn / explain_pg_dsn: the one DSN owner (plan 2B.1).

The keyring, secrets-file and vault tiers are replaced by fakes keyed by name,
so no real keyring or vault is touched.
"""
from __future__ import annotations

import logging

import pytest

PRIMARY = "postgresql://u:p@primary.example/m3"
WAREHOUSE = "postgresql://u:p@warehouse.example/cdw"


@pytest.fixture
def tiers(monkeypatch):
    """Fake tiers: dicts of name -> value; an Exception value is that tier's error."""
    import auth_utils

    store = {"keyring": {}, "file": {}, "vault": {}}

    def make(kind):
        def read(name):
            v = store[kind].get(name)
            if isinstance(v, Exception):
                return None, f"{kind}: {type(v).__name__}: {v}"
            return v, None
        return read

    monkeypatch.setattr(auth_utils, "_keyring_tier", make("keyring"))
    monkeypatch.setattr(auth_utils, "_file_tier", make("file"))
    monkeypatch.setattr(auth_utils, "_vault_tier", make("vault"))
    for var in ("M3_PRIMARY_PG_URL", "M3_PG_URL", "M3_CDW_PG_URL", "PG_URL"):
        monkeypatch.delenv(var, raising=False)
    return store


def _explain(role):
    from m3_core.secret_store import PgRole, explain_pg_dsn
    return explain_pg_dsn(PgRole(role))


def test_env_wins_and_is_named(tiers, monkeypatch):
    tiers["keyring"]["M3_PRIMARY_PG_URL"] = "postgresql://from-keyring/x"
    monkeypatch.setenv("M3_PG_URL", f"  {PRIMARY}  ")
    r = _explain("primary")
    assert (r.dsn, r.source) == (PRIMARY, "env:M3_PG_URL")


@pytest.mark.parametrize("tier,source", [("keyring", "keyring"), ("file", "secrets-file")])
def test_keyring_then_file(tiers, tier, source):
    tiers[tier]["M3_PRIMARY_PG_URL"] = PRIMARY
    r = _explain("primary")
    assert (r.dsn, r.source) == (PRIMARY, f"{source}:M3_PRIMARY_PG_URL")


def test_keyring_beats_the_file(tiers):
    tiers["keyring"]["M3_PRIMARY_PG_URL"] = PRIMARY
    tiers["file"]["M3_PRIMARY_PG_URL"] = "postgresql://from-file/x"
    assert _explain("primary").dsn == PRIMARY


def test_primary_from_the_vault_works_but_warns_every_time(tiers, caplog):
    """D1: a deprecated fallback until N+3, never silent."""
    tiers["vault"]["M3_PRIMARY_PG_URL"] = PRIMARY
    with caplog.at_level(logging.WARNING):
        r1 = _explain("primary")
        r2 = _explain("primary")
    assert r1.dsn == r2.dsn == PRIMARY and r1.source == "vault:M3_PRIMARY_PG_URL"
    assert caplog.text.count("deprecated") == 2
    # Actionable (§5): names the key and a repair that keeps the DSN out of history.
    assert "python -m keyring set system M3_PRIMARY_PG_URL" in caplog.text
    assert PRIMARY not in caplog.text, "the warning echoed the DSN"


def test_warehouse_from_the_legacy_vault_key_is_not_a_warning(tiers, caplog):
    tiers["vault"]["PG_URL"] = WAREHOUSE
    with caplog.at_level(logging.WARNING):
        r = _explain("warehouse")
    assert (r.dsn, r.source) == (WAREHOUSE, "vault:PG_URL")
    assert "deprecated" not in caplog.text


def test_roles_never_cross(tiers, monkeypatch):
    monkeypatch.setenv("M3_CDW_PG_URL", WAREHOUSE)
    tiers["keyring"]["PG_URL"] = WAREHOUSE
    assert _explain("primary").dsn == "", "the warehouse DSN reached the primary role"
    monkeypatch.delenv("M3_CDW_PG_URL")
    monkeypatch.setenv("M3_PRIMARY_PG_URL", PRIMARY)
    tiers["keyring"].clear()
    tiers["vault"]["M3_PRIMARY_PG_URL"] = PRIMARY
    assert _explain("warehouse").dsn == "", "the primary DSN reached the warehouse role"


def test_a_miss_explains_what_each_tier_hit(tiers):
    """m3 2f4bad6c: a broken keyring must not read as 'not set'."""
    tiers["keyring"]["M3_PRIMARY_PG_URL"] = RuntimeError("keyring backend crashed")
    tiers["vault"]["M3_PRIMARY_PG_URL"] = ValueError("salt missing")
    r = _explain("primary")
    assert r.dsn == "" and r.source is None
    assert any("keyring backend crashed" in e for e in r.errors), r.errors
    assert any("salt missing" in e for e in r.errors), r.errors


def test_warehouse_migrator_finds_a_stored_dsn(tiers):
    """It read env only, so a DSN the installer stored in the keyring was 'missing'."""
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
    assert "keyring backend crashed" in err and "--dsn" in err, err


def test_resolve_returns_the_string(tiers, monkeypatch):
    from m3_core.secret_store import PgRole, resolve_pg_dsn

    assert resolve_pg_dsn(PgRole.PRIMARY) == ""
    monkeypatch.setenv("M3_PRIMARY_PG_URL", PRIMARY)
    assert resolve_pg_dsn(PgRole.PRIMARY) == PRIMARY
