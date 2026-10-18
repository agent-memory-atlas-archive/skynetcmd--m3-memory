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
        # the server does not read this name, so it is not the port
        (_RUST_UNIT.format(env='Environment="M3_EMBED_SERVER_PORT=8083"'), 8082),
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


def test_a_plist_env_port_is_not_the_port(monkeypatch, tmp_path):
    """M3_EMBED_SERVER_PORT in a plist is not read by the server: the default."""
    import install_schedules as sched

    simulate_os(monkeypatch, "Darwin")
    monkeypatch.setattr(sched.os.path, "expanduser", lambda p: str(tmp_path / p.replace("~/", "")))
    plist = tmp_path / "Library" / "LaunchAgents" / "com.skynetcmd.m3-embed-server.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text(
        "<dict><key>EnvironmentVariables</key><dict><key>M3_EMBED_SERVER_PORT</key><string>8084</string></dict></dict>",
        encoding="utf-8",
    )
    assert sched.registered_embed_port() == 8082


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


def _config_file() -> str:
    from m3_core.config import locate

    return str(locate().file).replace("\\", "/")


def test_a_rust_unit_is_pointed_at_m3_config(monkeypatch, tmp_path):
    """The server takes its port only from m3_config.toml (--config); a unit
    without it is given --config, and a stale env port line is dropped."""
    import install_schedules as sched

    simulate_os(monkeypatch, "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(sched, "_run", lambda *a, **k: None)
    unit = tmp_path / "systemd" / "user" / "m3-embed-server.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(_RUST_UNIT.format(env='Environment="M3_EMBED_SERVER_PORT=8083"'), encoding="utf-8")
    assert sched.pin_embed_port(8083) is True
    text = unit.read_text(encoding="utf-8")
    assert f'ExecStart=/v/m3_core_rs/m3-embed-server --config "{_config_file()}"' in text
    assert "M3_EMBED_SERVER_PORT" not in text
    assert sched.registered_embed_port() is None, "the port is the file's now"
    assert sched.pin_embed_port(8085) is True
    assert unit.read_text(encoding="utf-8").count("--config") == 1, "a second run does not stack flags"


def test_a_plist_is_pointed_at_m3_config(monkeypatch, tmp_path):
    import plistlib

    import install_schedules as sched

    simulate_os(monkeypatch, "Darwin")
    monkeypatch.setattr(sched.os.path, "expanduser", lambda p: str(tmp_path / p.replace("~/", "")))
    plist = tmp_path / "Library" / "LaunchAgents" / "com.skynetcmd.m3-embed-server.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.skynetcmd.m3-embed-server",
                "ProgramArguments": ["/b/m3-embed-server", "run"],
                "EnvironmentVariables": {"M3_EMBED_SERVER_PORT": "8084"},
            }
        )
    )
    assert sched.pin_embed_port(8083) is True
    data = plistlib.loads(plist.read_bytes())
    assert data["ProgramArguments"] == ["/b/m3-embed-server", "--config", _config_file(), "run"]
    assert "M3_EMBED_SERVER_PORT" not in data["EnvironmentVariables"]


def test_only_this_accounts_inproc_server_on_that_port_is_stopped(monkeypatch):
    import psutil

    import m3_memory._platform as plat
    from m3_memory import embedder_admin

    class _C:
        def __init__(self, port):
            self.status, self.laddr = "LISTEN", type("A", (), {"port": port})()

    class _P:
        def __init__(self, pid, cmd, port, mine):
            self.info = {"pid": pid, "cmdline": cmd}
            self._port, self.mine, self.stopped = port, mine, False

        def net_connections(self, kind="tcp"):
            return [_C(self._port)]

        def terminate(self):
            self.stopped = True

        def wait(self, timeout=None):
            return 0

    inproc = ["python", "/x/bin/embed_server_inproc.py", "--port", "8083"]
    procs = [
        _P(1, inproc, 8083, True),
        _P(2, inproc, 8083, False),  # another account's
        _P(3, inproc, 8090, True),  # another port
        _P(4, ["/x/m3-embed-server"], 8083, True),  # the service itself
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))
    monkeypatch.setattr(plat, "owned_by_current_user", lambda p: p.mine)
    assert embedder_admin._stop_own_stray_server(8083) == [1]
    assert [p.stopped for p in procs] == [True, False, False, False]


def test_a_port_moved_back_is_followed_but_a_hand_edit_is_kept(tmp_path, m3_settings):
    """A move back to 8082 left the clients on 8092: m3's own earlier value no
    longer looked like m3's. The value m3 last wrote is recorded and followed."""
    import json

    from m3_memory import embedder_admin

    cfg = tmp_path / ".embed_config.json"

    def url():
        return json.loads(cfg.read_text(encoding="utf-8"))["fallback_url"]

    m3_settings({"embed.server.port": 8092})
    embedder_admin.seed_shared_config(str(tmp_path))
    assert url() == "http://127.0.0.1:8092"
    m3_settings({"embed.server.port": 8082})
    embedder_admin.seed_shared_config(str(tmp_path))
    assert url() == "http://127.0.0.1:8082", "moved back"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["fallback_url"] = "http://127.0.0.1:9999"  # a hand edit
    cfg.write_text(json.dumps(data), encoding="utf-8")
    m3_settings({"embed.server.port": 8093})
    embedder_admin.seed_shared_config(str(tmp_path))
    assert url() == "http://127.0.0.1:9999"
