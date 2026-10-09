"""A legacy vault row is re-encrypted on read, on every backend (plan 2B.1, R3).

Reading a secret that was stored in the legacy format (Fernet, or AES-GCM under
the old PBKDF2 iteration count) is meant to rewrite it in the current format.
The rewrite ran on the READ-ONLY connection it had just opened for the SELECT,
with a hardcoded '?', so it always failed ("attempt to write a readonly
database" on SQLite; '?' is rejected on PostgreSQL), was swallowed at debug,
and the "will be auto-migrated" WARNING repeated on every read (m3 2a82d0f2).
"""
from __future__ import annotations

import uuid

import pytest

_MASTER = "r3-test-master-key"


@pytest.fixture
def auth(monkeypatch, each_backend):
    import auth_utils

    # A fixed device salt: without one, inserting rows first makes m3 (rightly)
    # refuse to mint a salt for a vault that already holds secrets.
    monkeypatch.setenv("M3_AGENT_OS_SALT_HEX", "ab" * 16)
    monkeypatch.setattr(auth_utils, "get_master_key", lambda: _MASTER)
    monkeypatch.setattr(auth_utils, "safe_keyring_get_password", lambda *a, **k: None)
    return auth_utils


def _put_raw(auth, service, encrypted):
    from memory.backends import active_backend, dialect

    p = dialect().param()
    with active_backend().connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO synchronized_secrets (service_name, encrypted_value, version, origin_device, updated_at) "
            f"VALUES ({p}, {p}, {p}, {p}, {p})",
            (service, encrypted, 1, "test", "2026-01-01T00:00:00Z"))
        conn.commit()


def _get_raw(service):
    from memory.backends import active_backend, dialect

    p = dialect().param()
    with active_backend().connection() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT encrypted_value FROM synchronized_secrets WHERE service_name = {p}", (service,))
        return cur.fetchone()[0]


def _legacy_iterations_token(auth, value: str) -> str:
    """AES-GCM, as written before PBKDF2 went from 100k to 600k iterations."""
    import base64

    key = auth._derive_raw_key(_MASTER, auth._PBKDF2_LEGACY_ITERATIONS)
    with auth._crypto_lock:
        auth.crypto.unlock_key()
        try:
            return base64.b64encode(auth.crypto.encrypt(value.encode("utf-8"), key)).decode("ascii")
        finally:
            auth.crypto.lock_key()


def test_legacy_iterations_row_is_rewritten(auth, monkeypatch):
    service = f"M3_R3_PROBE_{uuid.uuid4().hex[:8].upper()}"
    monkeypatch.delenv(service, raising=False)
    _put_raw(auth, service, _legacy_iterations_token(auth, "old-kdf-value"))

    assert auth.get_api_key(service) == "old-kdf-value"
    rewritten = _get_raw(service)
    assert auth._decrypt_token(rewritten, _MASTER) == "old-kdf-value", "row still needs the legacy KDF"


def test_legacy_fernet_row_is_rewritten_in_the_current_format(auth, monkeypatch):
    """Pre-91fdea1e rows hold the Fernet token string itself ('gAAAA...').
    _decrypt_token base64-decoded it before handing it to Fernet, so such a row
    could not be read at all: the secret silently resolved to None."""
    service = f"M3_R3_PROBE_{uuid.uuid4().hex[:8].upper()}"
    monkeypatch.delenv(service, raising=False)
    legacy = auth._get_fernet(_MASTER).encrypt(b"s3cret-value").decode("ascii")
    assert legacy.startswith("gAAAA")
    _put_raw(auth, service, legacy)

    assert auth.get_api_key(service) == "s3cret-value"

    rewritten = _get_raw(service)
    assert not rewritten.startswith("gAAAA"), "legacy row was not re-encrypted"
    assert auth._decrypt_token(rewritten, _MASTER) == "s3cret-value"
