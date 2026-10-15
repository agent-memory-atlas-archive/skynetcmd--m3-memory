"""A registered embed service is moved to the configured port: `install` never
rewrites an existing registration, so a port chosen for a second account on a
shared machine never reached the service (measured 2026-10-15)."""

from __future__ import annotations

import pytest

from conftest import simulate_os

_RUST_UNIT = """[Service]
ExecStart=/v/m3_core_rs/m3-embed-server
Environment="M3_EMBED_GGUF=/m/bge.gguf"
{env}
"""
_PY_UNIT = """# comment mentioning --port 9999 is ignored
[Service]
ExecStart=/v/bin/m3-embed-inproc /p/bin/embed_server_inproc.py --port {port}
"""


@pytest.mark.parametrize(
    "text, port",
    [
        (_RUST_UNIT.format(env=""), 8082),
        (_RUST_UNIT.format(env='Environment="M3_EMBED_SERVER_PORT=8083"'), 8083),
        (_PY_UNIT.format(port=8091), 8091),
    ],
)
def test_the_registered_port_is_read_from_the_unit(monkeypatch, tmp_path, text, port):
    import install_schedules as sched

    simulate_os(monkeypatch, "Linux")
    unit = tmp_path / "systemd" / "user" / "m3-embed-server.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(text, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert sched.registered_embed_port() == port


def test_a_plist_port_is_read(monkeypatch, tmp_path):
    import install_schedules as sched

    simulate_os(monkeypatch, "Darwin")
    monkeypatch.setattr(sched.os.path, "expanduser", lambda p: str(tmp_path / p.replace("~/", "")))
    plist = tmp_path / "Library" / "LaunchAgents" / "com.skynetcmd.m3-embed-server.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text(
        "<dict><key>EnvironmentVariables</key><dict><key>M3_EMBED_SERVER_PORT</key><string>8084</string></dict></dict>",
        encoding="utf-8",
    )
    assert sched.registered_embed_port() == 8084


def test_no_definition_is_none(monkeypatch, tmp_path):
    import install_schedules as sched

    simulate_os(monkeypatch, "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert sched.registered_embed_port() is None


def test_the_shared_config_follows_the_configured_port(tmp_path, monkeypatch, m3_settings):
    """The clients' address kept 8082 after the port moved, so every client used
    another account's server and the doctor called that healthy."""
    import json

    from m3_memory import embedder_admin

    m3_settings({"embed.server.port": 8083})
    cfg = tmp_path / ".embed_config.json"
    cfg.write_text(json.dumps({"fallback_url": "http://127.0.0.1:8082", "gguf_path": "/m.gguf"}), encoding="utf-8")
    embedder_admin.seed_shared_config(str(tmp_path))
    assert json.loads(cfg.read_text(encoding="utf-8"))["fallback_url"] == "http://127.0.0.1:8083"
    for kept in ("http://gpu-box:8082", "http://127.0.0.1:9999"):
        cfg.write_text(json.dumps({"fallback_url": kept}), encoding="utf-8")
        embedder_admin.seed_shared_config(str(tmp_path))
        assert json.loads(cfg.read_text(encoding="utf-8"))["fallback_url"] == kept, "an address the user set stays"


def test_the_doctor_checks_the_configured_port(m3_settings):
    """A fixed 8082 in the checks probed another account's server and called it
    this account's embedder, healthy."""
    from doctor import embed_server_probe, shared_embedder_probe

    m3_settings({"embed.server.port": 8083})
    assert embed_server_probe._health_port() == 8083
    assert shared_embedder_probe._port() == 8083
    assert shared_embedder_probe._default_url() == "http://127.0.0.1:8083"
