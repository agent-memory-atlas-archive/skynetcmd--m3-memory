"""m3_core.secret_store: the user-only secrets file tier (plan 2B.1, CONFIG_DESIGN §10)."""

from __future__ import annotations

import os
import stat
import sys

import pytest

posix_only = pytest.mark.requires_posix  # POSIX mode bits


def test_round_trip_including_awkward_values():
    from m3_core import secret_store as secrets

    tricky = 'postgresql://u:p"a\\ss\nw@h/db'
    secrets.set_secret("M3_PRIMARY_PG_URL", tricky)
    secrets.set_secret("M3_SERVE_TOKEN", "x")
    assert secrets.get_secret("M3_PRIMARY_PG_URL") == tricky
    assert secrets.read_secrets() == {"M3_PRIMARY_PG_URL": tricky, "M3_SERVE_TOKEN": "x"}


def test_no_file_is_empty_not_an_error():
    from m3_core import secret_store as secrets

    assert not secrets.secrets_file_path().exists()
    assert secrets.read_secrets() == {}


def test_malformed_file_is_loud():
    from m3_core import secret_store as secrets

    p = secrets.secrets_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[secrets\nX = 1", encoding="utf-8")
    if sys.platform != "win32":
        p.chmod(0o600)
    with pytest.raises(secrets.SecretsFileError, match="not valid TOML"):
        secrets.read_secrets()


def test_a_non_string_entry_is_named_but_its_value_is_not_echoed():
    from m3_core import secret_store as secrets

    p = secrets.secrets_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[secrets]\nGOOD = "ok"\nBAD = 424242\n', encoding="utf-8")
    if sys.platform != "win32":
        p.chmod(0o600)
    with pytest.raises(secrets.SecretsFileError) as exc:
        secrets.read_secrets()
    assert "BAD" in str(exc.value) and "424242" not in str(exc.value)


def test_bad_names_are_refused():
    from m3_core import secret_store as secrets

    with pytest.raises(ValueError):
        secrets.set_secret("BAD NAME", "x")


@posix_only
def test_new_file_is_user_only_and_a_loose_one_is_refused():
    from m3_core import secret_store as secrets

    p = secrets.set_secret("M3_SERVE_TOKEN", "1")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    os.chmod(p, 0o640)
    with pytest.raises(secrets.SecretsFileError, match="chmod 600"):
        secrets.read_secrets()


# ── names (CONFIG_DESIGN §10; user decision 2026-10-09) ──────────────────────
def _plant(secrets, text):
    p = secrets.secrets_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    if sys.platform != "win32":
        p.chmod(0o600)
    return p


def test_an_unknown_name_is_refused_without_its_value(monkeypatch):
    from m3_core import secret_store as secrets

    monkeypatch.setattr("slm_intent.profile_secret_names", lambda: (set(), []))
    _plant(secrets, '[secrets]\nM3_SERVE_TOKN = "s3cret-typo"\n')
    with pytest.raises(secrets.SecretsFileError) as exc:
        secrets.read_secrets()
    assert "unknown secret name(s): M3_SERVE_TOKN" in str(exc.value)
    assert "s3cret-typo" not in str(exc.value)


def test_an_unknown_name_is_refused_before_anything_is_written(monkeypatch):
    from m3_core import secret_store as secrets

    monkeypatch.setattr("slm_intent.profile_secret_names", lambda: (set(), []))
    with pytest.raises(secrets.SecretsFileError, match="unknown secret name"):
        secrets.set_secret("NOT_A_SECRET_NAME", "v")
    assert not secrets.secrets_file_path().exists()


def test_a_profile_declared_name_is_accepted(monkeypatch):
    """A profile for a new provider works without a code change."""
    from m3_core import secret_store as secrets

    monkeypatch.setattr("slm_intent.profile_secret_names", lambda: ({"MISTRAL_API_KEY"}, []))
    secrets.set_secret("MISTRAL_API_KEY", "k")
    assert secrets.get_secret("MISTRAL_API_KEY") == "k"


def test_an_unreadable_profile_is_named_in_the_error(monkeypatch):
    from m3_core import secret_store as secrets

    monkeypatch.setattr("slm_intent.profile_secret_names", lambda: (set(), ["broken (ValueError: x)"]))
    _plant(secrets, '[secrets]\nMISTRAL_API_KEY = "k"\n')
    with pytest.raises(secrets.SecretsFileError, match="could not be read: broken"):
        secrets.read_secrets()


def test_the_shipped_profiles_declare_only_known_or_listed_names():
    """Every api_key_service a shipped profile names resolves through the chain."""
    from slm_intent import profile_secret_names

    names, unreadable = profile_secret_names()
    assert not unreadable, unreadable
    assert {"LM_API_TOKEN", "XAI_API_KEY"} <= names


@posix_only
def test_a_file_another_user_owns_is_refused(monkeypatch):
    """Planted foreign owner (plan 2C.1): the loader refuses, naming both uids."""
    from m3_core import secret_store as secrets

    p = secrets.set_secret("M3_SERVE_TOKEN", "1")
    real = os.getuid()
    monkeypatch.setattr(secrets.os, "getuid", lambda: real + 1)
    with pytest.raises(secrets.SecretsFileError, match=rf"owned by uid {real}, not you \({real + 1}\)"):
        secrets.read_secrets()
    assert str(p) in str(secrets.secrets_file_path())
