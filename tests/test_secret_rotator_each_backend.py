"""Secret rotation works on EVERY backend: the backup and the audit event land in
the live store.

It refused on PostgreSQL (require_sqlite_backend) and wrote through the SQLite
pool, though the vault itself (set_api_key) already lived in the active
backend. The crypto edges are stubbed; the rows are what this pins.
"""
from __future__ import annotations

import uuid

import pytest


@pytest.fixture
def vault_row(each_backend):
    """One synchronized_secrets row, removed afterwards with its backup.

    The PG test store persists across runs and tests, and a leftover row is not
    inert: auth_utils' salt guard reads any synchronized_secrets row as "the
    vault holds secrets" and refuses to mint a salt, which failed
    test_serve_auth_pg_live in the next file (2026-10-10).
    """
    from memory.backends import dialect

    key = f"ROT_PROBE_{uuid.uuid4().hex[:8]}"
    p = dialect().param()
    with each_backend.connection() as conn:
        conn.execute(f"INSERT INTO synchronized_secrets (service_name, encrypted_value) VALUES ({p}, {p})",
                     (key, "old-cipher"))
        conn.commit()
    try:
        yield key
    finally:
        with each_backend.connection() as conn:
            for table in ("synchronized_secrets", "secret_backups"):
                conn.execute(f"DELETE FROM {table} WHERE service_name = {p}", (key,))
            conn.commit()


def test_rotation_backs_up_and_logs_in_the_live_store(each_backend, vault_row, monkeypatch):
    import secret_rotator as sr
    from m3_core.context import M3Context
    from memory.backends import dialect

    key = vault_row
    p = dialect().param()

    written: dict = {}
    monkeypatch.setattr(sr, "ROTATION_TARGETS", [key])
    monkeypatch.setattr(sr, "set_api_key", lambda name, value: written.setdefault(name, value))
    monkeypatch.setattr(M3Context, "get_secret", lambda self, name: "old-plain")

    sr.rotate_secrets(dry_run=False)

    assert key in written and len(written[key]) == 64, "observed: no new secret stored"
    with each_backend.connection() as conn:
        backup = conn.execute(
            f"SELECT encrypted_value FROM secret_backups WHERE service_name = {p}", (key,)).fetchone()
        logged = conn.execute(
            f"SELECT count(*) FROM activity_logs WHERE response LIKE {p}", (f"%{key}%",)).fetchone()
    assert backup is not None and backup[0] == "old-cipher", (
        f"observed: backup {backup!r} on {each_backend.name}. inspect: secret_rotator.py")
    assert logged[0] >= 1, f"observed: no audit event on {each_backend.name}. inspect: audit_trail.log_event"
