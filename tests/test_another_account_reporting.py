"""On a shared machine, another account's server is never reported as ours.

Found in a trial install beside an existing m3 user: the new account's
dashboard could not bind, yet the start check printed "Started ... (serving)"
because the other account's dashboard answered on the same port, and setup's
summary called the other account's embedder this install's "local server ...
with the speed-up module".
"""

from __future__ import annotations

import os
import sys
import types

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (os.path.join(_ROOT, "bin"), _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import install_schedules as isch  # noqa: E402

from m3_memory import _ports, embedder_admin, installer, rust_core_install  # noqa: E402


def test_start_check_does_not_count_another_accounts_answer(monkeypatch):
    monkeypatch.setattr(_ports, "held_by_another_owner", lambda *a, **k: True)
    detail = isch._confirm_service_live("m3-dashboard.service", deadline_s=0.1)
    assert detail is not True
    assert "another account" in detail


def test_start_check_still_polls_when_the_port_is_ours(monkeypatch):
    monkeypatch.setattr(_ports, "held_by_another_owner", lambda *a, **k: False)
    detail = isch._confirm_service_live("m3-dashboard.service", deadline_s=0.1)
    assert detail is True or "another account" not in detail


def _shared_mode(monkeypatch):
    monkeypatch.setattr(embedder_admin, "shared_mode_enabled", lambda: True)
    fake = types.ModuleType("m3_core_rs")
    fake.EmbeddedEmbedder = object
    fake.__version__ = "3.10.1"
    monkeypatch.setitem(sys.modules, "m3_core_rs", fake)


def test_tier_summary_names_another_accounts_server(monkeypatch):
    _shared_mode(monkeypatch)
    monkeypatch.setattr(embedder_admin, "_port_held_by_someone_else", lambda *a, **k: True)
    tier = rust_core_install.active_embedder_tier()
    assert tier["summary"] == "another account's server on port 8082"
    assert tier["others_server"] is True
    assert "speed-up module" not in tier["summary"]


def test_headline_names_another_accounts_server(monkeypatch):
    _shared_mode(monkeypatch)
    monkeypatch.setattr(embedder_admin, "_port_held_by_someone_else", lambda *a, **k: True)
    monkeypatch.setattr(rust_core_install, "active_embedder_tier", lambda: {"native": True})
    assert installer.status_summary()["embedder"] == "another account's server (port 8082)"


def test_start_check_reason_is_recognised_by_its_caller(monkeypatch):
    """The caller drops the "check its log" advice for this reason; the two
    must share the one phrase."""
    monkeypatch.setattr(_ports, "held_by_another_owner", lambda *a, **k: True)
    detail = isch._confirm_service_live("m3-dashboard.service", deadline_s=0.1)
    assert isch._HELD_BY_OTHER in detail


def test_a_failed_ownership_check_is_reported_not_read_as_ours(monkeypatch):
    """An error in the check must not turn into "this account's server, OK"."""
    from doctor import shared_embedder_probe as p

    def boom(*_a, **_k):
        raise OSError("probe broke")

    monkeypatch.setattr(embedder_admin, "_port_held_by_someone_else", boom)
    whose = p._server_is_someone_elses()
    from doctor import embed_server_probe as esp

    assert esp._held_by_someone_else() == whose, "both probes ask the one owner"
    assert isinstance(whose, str) and "probe broke" in whose


def _stub_probe(monkeypatch, p, *, others: bool, started: list):
    monkeypatch.setattr(p, "_read_config", lambda: {"disable_inproc_embedder": True})
    from m3_core.tier2_client import Diagnosis

    monkeypatch.setattr(p, "_server_health", lambda *a, **k: (Diagnosis("ok", "serving"), {}))
    monkeypatch.setattr(p, "_classify_embed_model", lambda cfg, body: (None, []))
    monkeypatch.setattr(p, "_keepalive", lambda: ("rust-service", False))
    monkeypatch.setattr(p, "_detect_inproc_env_leak", lambda: [])
    monkeypatch.setattr(p, "_server_is_someone_elses", lambda: others)
    monkeypatch.setattr(p, "_rust_service_state", lambda: "stopped")
    monkeypatch.setattr(p, "_fix_start_rust_service", lambda: started.append(1) or True)


def test_doctor_does_not_restart_a_service_that_cannot_bind(monkeypatch, capsys):
    """Found in a trial install: with another account on the port, this
    account's service failed to bind and was restarted every few seconds,
    loading the model each time; `doctor --fix` would have started it again."""
    from doctor import shared_embedder_probe as p

    started: list = []
    _stub_probe(monkeypatch, p, others=True, started=started)
    assert p.run(brief=True, fix=True) == 0
    assert started == []
    assert "another account's" in capsys.readouterr().out


def test_doctor_still_restarts_this_accounts_stopped_service(monkeypatch):
    """The guard must be able to fail: with no other owner, a stopped service is
    the problem it always was, and --fix starts it."""
    from doctor import shared_embedder_probe as p

    started: list = []
    _stub_probe(monkeypatch, p, others=False, started=started)
    p.run(brief=True, fix=True)
    assert started == [1]


def test_setup_stops_the_service_another_account_blocks(monkeypatch, tmp_path, capsys):
    calls: list = []
    monkeypatch.setattr(embedder_admin, "_confirm_started", lambda *a, **k: True)
    monkeypatch.setattr(embedder_admin, "_wait_serving", lambda *a, **k: True)
    monkeypatch.setattr(embedder_admin, "_port_held_by_someone_else", lambda *a, **k: True)
    monkeypatch.setattr(embedder_admin, "_service_cmd_quiet", lambda b, g, sub, *e: calls.append(sub) or 0)
    rc = embedder_admin._verify_started_or_explain(tmp_path / "bin", tmp_path / "m.gguf")
    assert rc == embedder_admin.EXIT_REGISTERED_NOT_RUNNING
    assert calls == ["stop"]
    assert "stopped so it does not keep retrying" in capsys.readouterr().err


def test_a_service_that_exited_on_a_taken_port_gets_the_port_fix(monkeypatch, tmp_path, capsys):
    """Core 3.10.14 exits at once when the port is taken, so setup sees a
    stopped service, not a running one; the advice to stop the holder cannot
    work when it is another account's."""
    monkeypatch.setattr(embedder_admin, "_confirm_started", lambda *a, **k: False)
    monkeypatch.setattr(embedder_admin, "_port_held_by_someone_else", lambda *a, **k: True)
    monkeypatch.setattr(embedder_admin, "_port_in_use", lambda *a, **k: True)
    rc = embedder_admin._verify_started_or_explain(tmp_path / "bin", tmp_path / "m.gguf", start_rc=1)
    assert rc == embedder_admin.EXIT_REGISTERED_NOT_RUNNING
    err = capsys.readouterr().err
    assert "m3 config set embed.server.port" in err
    assert "Stop it, then run" not in err


def test_a_port_held_by_this_account_keeps_the_find_and_stop_advice(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(embedder_admin, "_confirm_started", lambda *a, **k: False)
    monkeypatch.setattr(embedder_admin, "_port_held_by_someone_else", lambda *a, **k: False)
    monkeypatch.setattr(embedder_admin, "_port_in_use", lambda *a, **k: True)
    monkeypatch.setattr(embedder_admin, "_port_holder_hint", lambda port: "  (holder)")
    embedder_admin._verify_started_or_explain(tmp_path / "bin", tmp_path / "m.gguf", start_rc=1)
    assert "Stop it, then run" in capsys.readouterr().err


def _esp_status(monkeypatch, state: str, whose):
    from doctor import embed_server_probe as esp

    class _R:
        def __init__(self, rc, out):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    monkeypatch.setattr(esp, "_resolve_binary", lambda: "/bin/m3-embed-server")
    monkeypatch.setattr(
        esp.subprocess,
        "run",
        lambda cmd, **kw: (
            _R(2, "USAGE: m3-embed-server <install|status>") if cmd[1] == "doctor" else _R(0, state + "\n")
        ),
    )
    monkeypatch.setattr(esp, "_held_by_someone_else", lambda: whose)
    # The probe also scans the real process table for outdated servers.
    monkeypatch.setattr(embedder_admin, "stale_embed_servers", lambda: [])
    return esp


def test_a_stopped_server_behind_another_account_gets_the_port_fix(monkeypatch, capsys):
    """Found in a trial: doctor said "registered but stopped. fix: m3 embedder
    start", which fails again while another account holds the port."""
    esp = _esp_status(monkeypatch, "stopped", True)
    esp.run(brief=True)
    out = capsys.readouterr().out
    assert "m3 config set embed.server.port" in out
    assert "m3 embedder start" not in out


def test_a_failed_owner_check_is_not_reported_ok(monkeypatch, capsys):
    esp = _esp_status(monkeypatch, "running", "OSError: probe broke")
    esp.run(brief=True)
    out = capsys.readouterr().out
    assert "✅" not in out and "could not check who holds the port" in out
