"""One secret chain (m3_core.secret_store.explain_secret) and its vault policy.

get_api_key and explain_pg_dsn each walked env -> keyring -> secrets file ->
vault on their own; both now delegate. The policy comes from the registry's
``bootstrap`` flag: such a secret is never read from the vault (CONFIG_DESIGN
§10), except D1's primary DSN, which still works and warns until N+3.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def tiers(monkeypatch):
    """Fake tiers keyed by name; record which names the vault was asked for."""
    import auth_utils

    store = {"keyring": {}, "file": {}, "vault": {}}
    asked_vault = []

    def make(kind):
        def read(name):
            if kind == "vault":
                asked_vault.append(name)
            return store[kind].get(name), None
        return read

    monkeypatch.setattr(auth_utils, "_keyring_tier", make("keyring"))
    monkeypatch.setattr(auth_utils, "_file_tier", make("file"))
    monkeypatch.setattr(auth_utils, "_vault_tier", make("vault"))
    # Cleared, not assumed absent: a developer shell may hold a real ANTHROPIC_API_KEY.
    for var in ("AGENT_OS_MASTER_KEY", "LM_API_TOKEN", "LM_STUDIO_API_KEY", "M3_DYNAMIC_PROBE",
                "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    store["asked_vault"] = asked_vault
    return store


def test_a_bootstrap_secret_is_never_read_from_the_vault(tiers):
    import auth_utils
    from m3_core.secret_store import explain_named_secret

    tiers["vault"]["AGENT_OS_MASTER_KEY"] = "from-vault"
    assert auth_utils.get_api_key("AGENT_OS_MASTER_KEY") is None
    r = explain_named_secret("AGENT_OS_MASTER_KEY")
    assert tiers["asked_vault"] == [], "the vault was consulted for a bootstrap secret"
    assert any("not consulted" in e and "bootstrap" in e for e in r.errors), r.errors


def test_an_unregistered_dynamic_name_still_uses_the_vault(tiers):
    import auth_utils

    tiers["vault"]["M3_DYNAMIC_PROBE"] = "from-vault"
    assert auth_utils.get_api_key("M3_DYNAMIC_PROBE") == "from-vault"


def test_the_lm_alias_comes_from_the_registry(tiers, monkeypatch):
    import auth_utils
    from m3_core.secret_store import explain_named_secret

    monkeypatch.setenv("LM_STUDIO_API_KEY", " tok ")
    assert auth_utils.get_api_key("LM_API_TOKEN") == "tok"
    assert explain_named_secret("LM_API_TOKEN").source == "env:LM_STUDIO_API_KEY"


def test_the_order_is_env_keyring_file_vault(tiers, monkeypatch):
    from m3_core.secret_store import explain_named_secret

    tiers["vault"]["ANTHROPIC_API_KEY"] = "v"
    assert explain_named_secret("ANTHROPIC_API_KEY").source == "vault:ANTHROPIC_API_KEY"
    tiers["file"]["ANTHROPIC_API_KEY"] = "f"
    assert explain_named_secret("ANTHROPIC_API_KEY").source == "secrets-file:ANTHROPIC_API_KEY"
    tiers["keyring"]["ANTHROPIC_API_KEY"] = "k"
    assert explain_named_secret("ANTHROPIC_API_KEY").source == "keyring:ANTHROPIC_API_KEY"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "e")
    assert explain_named_secret("ANTHROPIC_API_KEY").source == "env:ANTHROPIC_API_KEY"


def test_a_role_dsn_resolves_through_its_role(tiers, monkeypatch):
    """The registry name M3_CDW_PG_URL reaches PG_URL's stored copy, as the role does."""
    from m3_core.secret_store import explain_named_secret

    for var in ("M3_CDW_PG_URL", "PG_URL"):
        monkeypatch.delenv(var, raising=False)
    tiers["vault"]["PG_URL"] = "postgresql://u:p@wh/db"
    assert explain_named_secret("M3_CDW_PG_URL").source == "vault:PG_URL"
