"""Where the scheduled sync finds the warehouse.

A launchd/systemd/Task Scheduler job does not see the shell's environment, so a
warehouse configured only as M3_POSTGRES_SERVER in a shell rc was invisible to
it and the sync skipped silently. The DSN (env or stored secret) names the host
too. Addresses here are RFC 5737/6761 reserved; nothing is contacted.
"""

from __future__ import annotations

import importlib
import logging

import pytest


@pytest.fixture
def live(monkeypatch):
    """The m3_sdk and sync_all the code will import NOW. A module captured at
    collection can be orphaned by a later re-import, and a patch on it is then
    invisible to the code under test."""
    return importlib.import_module("m3_sdk"), importlib.import_module("sync_all")


def test_the_dsn_host_wins_over_a_host_setting(monkeypatch, live, m3_settings):
    """The probe tests the address sync connects to: a stale host setting (a LAN
    address kept after the warehouse moved) skipped or ran sync on the wrong path."""
    # Through m3_settings, not setenv: sync.warehouse_host is read from the
    # settings snapshot, and an env var set after the snapshot was built is unseen
    # (the test passed or failed depending on what ran before it).
    m3_sdk, sync_all = live
    m3_settings({"sync.warehouse_host": "192.0.2.10"})
    monkeypatch.setattr(m3_sdk, "resolve_warehouse_dsn", lambda: "postgresql://u@db.example.invalid:6543/w")
    assert sync_all.warehouse_target() == ("db.example.invalid", 6543, "warehouse DSN")


def test_a_host_setting_answers_without_a_dsn(monkeypatch, live, m3_settings):
    m3_sdk, sync_all = live
    m3_settings({"sync.warehouse_host": "192.0.2.10"})
    monkeypatch.setattr(m3_sdk, "resolve_warehouse_dsn", lambda: None)
    assert sync_all.warehouse_target() == ("192.0.2.10", 5432, "M3_POSTGRES_SERVER")


def test_without_the_host_env_the_dsn_supplies_host_and_port(monkeypatch, live):
    m3_sdk, sync_all = live
    monkeypatch.setattr(m3_sdk, "resolve_warehouse_dsn", lambda: "postgresql://u:p@db.example.invalid:6543/warehouse")
    assert sync_all.warehouse_target() == ("db.example.invalid", 6543, "warehouse DSN")


def test_a_dsn_without_a_host_is_reported_not_used(monkeypatch, caplog, live):
    m3_sdk, sync_all = live
    monkeypatch.setattr(m3_sdk, "resolve_warehouse_dsn", lambda: "postgresql:///warehouse")
    with caplog.at_level(logging.WARNING):
        assert sync_all.warehouse_target() is None
    assert "no host" in caplog.text


def test_nothing_configured_is_none(monkeypatch, live):
    m3_sdk, sync_all = live
    monkeypatch.setattr(m3_sdk, "resolve_warehouse_dsn", lambda: None)
    assert sync_all.warehouse_target() is None


def test_the_probe_dials_hostaddr_not_the_certificate_name(monkeypatch, live):
    """With verify-full, host names the certificate and may not resolve on this
    machine; probing it skipped every sync as "unreachable"."""
    m3_sdk, sync_all = live
    monkeypatch.setattr(
        m3_sdk,
        "resolve_warehouse_dsn",
        lambda: "postgresql://u@db.example.invalid:5432/w?hostaddr=192.0.2.7&sslmode=verify-full",
    )
    assert sync_all.warehouse_target() == ("192.0.2.7", 5432, "warehouse DSN")


def test_a_host_list_is_reachable_when_any_host_answers(monkeypatch, live):
    _m3_sdk, sync_all = live
    tried = []

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(addr, timeout):
        tried.append(addr[0])
        if addr[0] != "b.example.invalid":
            raise OSError("refused")
        return _Conn()

    monkeypatch.setattr(sync_all.socket, "create_connection", fake)
    assert sync_all.is_reachable("a.example.invalid, b.example.invalid", 5432)
    assert tried == ["a.example.invalid", "b.example.invalid"]
    assert not sync_all.is_reachable("a.example.invalid", 5432)
