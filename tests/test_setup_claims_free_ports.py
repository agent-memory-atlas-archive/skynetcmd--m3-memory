"""On a shared machine another account's m3 holds the default ports; setup picks
the next free ones for this account instead of failing and asking the user to
choose (measured upgrading a second account from the public release)."""

from __future__ import annotations

import socket

import pytest


def test_first_free_port_skips_a_port_in_use():
    from m3_memory._ports import first_free_port

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        busy = s.getsockname()[1]
        free = first_free_port(busy)
        assert free is not None and free != busy


@pytest.fixture
def setup_env(monkeypatch):
    import m3_memory._platform as plat
    import m3_memory._ports as ports
    from m3_memory import setup_wizard as sw

    recorded = {}
    monkeypatch.setattr(plat, "current_uid", lambda: 1002)
    monkeypatch.setattr(ports, "first_free_port", lambda start, **k: start)
    monkeypatch.setattr(sw, "_record_settings", lambda values, what: recorded.update(values) or True)
    import m3_core.config.settings as settings

    monkeypatch.setattr(settings, "set_from_cli", lambda values: None)
    return sw, ports, plat, recorded


def test_default_ports_held_by_another_account_move_to_free_ones(setup_env, monkeypatch):
    sw, ports, _plat, recorded = setup_env
    monkeypatch.setattr(ports, "held_by_another_owner", lambda port: port in (8082, 8088))
    plan = sw.SetupPlan()
    sw._claim_free_ports(plan)
    assert recorded == {"embed.server.port": "8083", "dashboard.port": "8089"}
    assert plan.dashboard_port == 8089


def test_free_or_own_ports_are_left_alone(setup_env, monkeypatch):
    sw, ports, _plat, recorded = setup_env
    monkeypatch.setattr(ports, "held_by_another_owner", lambda port: False)
    sw._claim_free_ports(sw.SetupPlan())
    assert recorded == {}


def test_a_port_the_user_chose_is_never_changed(setup_env, monkeypatch):
    sw, ports, _plat, recorded = setup_env
    monkeypatch.setattr(ports, "held_by_another_owner", lambda port: True)
    plan = sw.SetupPlan()
    plan.dashboard_port = 9000
    sw._claim_free_ports(plan)
    assert "dashboard.port" not in recorded


def test_windows_shares_one_embedder_by_design(setup_env, monkeypatch):
    sw, ports, plat, recorded = setup_env
    monkeypatch.setattr(plat, "current_uid", lambda: None)
    monkeypatch.setattr(ports, "held_by_another_owner", lambda port: True)
    sw._claim_free_ports(sw.SetupPlan())
    assert recorded == {}


def test_the_chosen_ports_are_written_through_the_real_writer(monkeypatch, tmp_path):
    """A stubbed writer hid that values reach it as text: the int raised."""
    import tomllib

    import m3_memory._platform as plat
    import m3_memory._ports as ports
    from m3_memory import setup_wizard as sw

    cfg = tmp_path / "m3_config.toml"
    import m3_core.config.loader as loader

    real_locate = loader.locate
    monkeypatch.setattr(loader, "locate", lambda path=None: real_locate(str(cfg)))
    monkeypatch.setattr(plat, "current_uid", lambda: 1002)
    monkeypatch.setattr(ports, "held_by_another_owner", lambda port: port == 8082)
    monkeypatch.setattr(ports, "first_free_port", lambda start, **k: start)
    sw._claim_free_ports(sw.SetupPlan())
    assert tomllib.loads(cfg.read_text(encoding="utf-8"))["embed"]["server"]["port"] == 8083
