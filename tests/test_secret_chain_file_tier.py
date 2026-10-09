"""get_api_key's chain: env > keyring > user-only secrets file > vault (plan 2B.1)."""
from __future__ import annotations

import logging
import sys

import pytest


@pytest.fixture
def auth(monkeypatch):
    import auth_utils

    monkeypatch.setattr(auth_utils, "_keyring_read", lambda *a, **k: (None, None))
    # no vault value in these tests unless one is planted
    monkeypatch.setattr(auth_utils, "_vault_db_path", lambda: "/nonexistent/vault.db")
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    return auth_utils


def test_file_tier_answers_when_env_and_keyring_do_not(auth):
    from m3_core import secret_store

    secret_store.set_secret("OPENCLAW_GATEWAY_TOKEN", "from-file")
    assert auth.get_api_key("OPENCLAW_GATEWAY_TOKEN") == "from-file"


def test_env_beats_the_file(auth, monkeypatch):
    from m3_core import secret_store

    secret_store.set_secret("OPENCLAW_GATEWAY_TOKEN", "from-file")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "from-env")
    assert auth.get_api_key("OPENCLAW_GATEWAY_TOKEN") == "from-env"


def test_keyring_beats_the_file(auth, monkeypatch):
    from m3_core import secret_store

    secret_store.set_secret("OPENCLAW_GATEWAY_TOKEN", "from-file")
    monkeypatch.setattr(auth, "_keyring_read", lambda *a, **k: ("from-keyring", None))
    assert auth.get_api_key("OPENCLAW_GATEWAY_TOKEN") == "from-keyring"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_unsafe_file_is_reported_not_silently_skipped(auth, caplog):
    import os

    from m3_core import secret_store

    p = secret_store.set_secret("OPENCLAW_GATEWAY_TOKEN", "from-file")
    os.chmod(p, 0o644)
    with caplog.at_level(logging.ERROR):
        assert auth.get_api_key("OPENCLAW_GATEWAY_TOKEN") is None
    assert "chmod 600" in caplog.text
