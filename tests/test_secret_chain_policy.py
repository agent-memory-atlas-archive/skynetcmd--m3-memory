"""One secret chain (m3_core.secret_store.explain_secret) and its vault policy.

The chain is the secrets file's value -> the keyring entry the file points at,
or ``system/NAME`` when the file does not list the name -> the vault. The
environment is never read for a secret. The vault policy comes
from the registry's ``bootstrap`` flag: such a secret is never read from the
vault, except D1's primary DSN, which still works and warns
through N+3.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def tiers(monkeypatch):
    """A fake keyring keyed "service/key" and a fake vault (recording what it
    was asked for); the secrets file is the sandbox's real one."""
    import auth_utils

    store = {"keyring": {}, "vault": {}}
    asked_vault = []

    def keyring(key, *, service="system"):
        return store["keyring"].get(f"{service}/{key}"), None

    def vault(name):
        asked_vault.append(name)
        return store["vault"].get(name), None

    monkeypatch.setattr(auth_utils, "_keyring_tier", keyring)
    monkeypatch.setattr(auth_utils, "_vault_tier", vault)
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


def test_the_environment_is_never_read_and_a_miss_says_so(tiers, monkeypatch):
    """The LM alias, set in env, is ignored; the miss names it (not its value)
    and the command that stores it under the registry name."""
    import auth_utils
    from m3_core.secret_store import explain_named_secret

    monkeypatch.setenv("LM_STUDIO_API_KEY", "tok-from-env")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    assert auth_utils.get_api_key("LM_API_TOKEN") is None
    assert auth_utils.get_api_key("ANTHROPIC_API_KEY") is None
    r = explain_named_secret("LM_API_TOKEN")
    said = " | ".join(r.errors)
    assert "LM_STUDIO_API_KEY is set in the environment" in said and "m3 secret set LM_API_TOKEN" in said, said
    assert "tok-from-env" not in said


def test_the_order_is_file_keyring_vault(tiers, monkeypatch):
    from m3_core.secret_store import explain_named_secret, set_secret

    monkeypatch.setenv("ANTHROPIC_API_KEY", "e")  # never a tier
    tiers["vault"]["ANTHROPIC_API_KEY"] = "v"
    assert explain_named_secret("ANTHROPIC_API_KEY").source == "vault:ANTHROPIC_API_KEY"
    tiers["keyring"]["system/ANTHROPIC_API_KEY"] = "k"
    assert explain_named_secret("ANTHROPIC_API_KEY").source == "keyring:system/ANTHROPIC_API_KEY"
    set_secret("ANTHROPIC_API_KEY", "f")
    r = explain_named_secret("ANTHROPIC_API_KEY")
    assert (r.value, r.source) == ("f", "secrets-file:ANTHROPIC_API_KEY")


def test_a_keyring_pointer_is_followed_and_an_empty_one_is_named(tiers):
    from m3_core.secret_store import KeyringRef, explain_named_secret, set_secret

    set_secret("OPENAI_API_KEY", KeyringRef("work", "openai"))
    tiers["keyring"]["system/OPENAI_API_KEY"] = "the default entry, not the one named"
    r = explain_named_secret("OPENAI_API_KEY")
    assert r.value == "" and any("points at keyring work/openai" in e for e in r.errors), r
    tiers["keyring"]["work/openai"] = "sk-work"
    r = explain_named_secret("OPENAI_API_KEY")
    assert (r.value, r.source) == ("sk-work", "keyring:work/openai")


def test_a_role_dsn_resolves_through_its_role(tiers):
    """The registry name M3_CDW_PG_URL reaches PG_URL's stored copy, as the role does."""
    from m3_core.secret_store import explain_named_secret

    tiers["vault"]["PG_URL"] = "postgresql://u:p@wh/db"
    assert explain_named_secret("M3_CDW_PG_URL").source == "vault:PG_URL"


@pytest.mark.parametrize(
    "value, shown",
    [
        ("postgresql://u:hunter2@db.example:5432/m3", "postgresql://u:***@db.example:5432/m3"),
        ("postgresql://u@db.example/m3", "postgresql://u@db.example/m3"),  # no password, nothing to hide
        ("sk-ant-0123456789abcdef", "sk-a***ef (23 chars)"),
        ("short-token", "*** (11 chars)"),
    ],
)
def test_a_masked_value_never_shows_the_secret(value, shown):
    from m3_core.secret_store import mask_dsn_passwords, mask_secret

    assert mask_secret(value) == shown
    assert mask_dsn_passwords(f"connect failed: {value}").startswith("connect failed: ")
    assert "hunter2" not in mask_dsn_passwords(f"error: could not reach {value}")


def test_a_missing_secret_with_no_name_says_the_profile_names_none():
    """A profile with no api_key_service used to print "no None in the secrets file"."""
    from m3_core.secret_store import missing_secret_message

    assert "names no API key secret" in missing_secret_message(None)
    assert "None" not in missing_secret_message(None)
    assert "m3 secret set LM_API_TOKEN" in missing_secret_message("LM_API_TOKEN")
