"""The device-salt cohesion check runs on EVERY backend and warns, never raises.

It had never run: it imported auth_utils.get_salt_path, which did not exist,
and returned at the ImportError. It is SQLite-pool-only no longer, and a
mismatch is a WARNING with observed/inspect (user decision, 2026-10-10).
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture
def salt(tmp_path, monkeypatch, each_backend):
    import auth_utils

    path = tmp_path / ".agent_os_salt"
    path.write_bytes(b"A" * 16)
    monkeypatch.setattr(auth_utils, "device_salt_path", lambda: str(path))
    # The table comes from the store's migrations (056 / pg_064), not from here.
    with each_backend.connection() as conn:  # the PG test store persists: start clean
        conn.execute("DELETE FROM m3_system_cohesion")
        conn.commit()
    try:
        yield path
    finally:
        with each_backend.connection() as conn:
            conn.execute("DELETE FROM m3_system_cohesion")
            conn.commit()


def test_records_then_warns_on_a_different_salt(each_backend, salt, caplog):
    from m3_core.context import verify_salt_cohesion

    with each_backend.connection() as conn:
        verify_salt_cohesion(conn, "probe store")  # first use records the hash
        n = conn.execute("SELECT COUNT(*) FROM m3_system_cohesion").fetchone()[0]
    assert n == 1, f"observed: {n} rows on {each_backend.name}"

    with caplog.at_level(logging.WARNING, logger="m3_core.context"), each_backend.connection() as conn:
        verify_salt_cohesion(conn, "probe store")
    assert not caplog.records, f"observed: a warning for a matching salt: {caplog.records[0].getMessage()}"

    salt.write_bytes(b"B" * 16)  # a different machine's salt
    with caplog.at_level(logging.WARNING, logger="m3_core.context"), each_backend.connection() as conn:
        verify_salt_cohesion(conn, "probe store")  # must not raise
    msgs = [r.getMessage() for r in caplog.records]
    assert any("salt cohesion" in m and "observed:" in m and "inspect:" in m for m in msgs), (
        f"observed: {msgs!r} on {each_backend.name}"
    )
