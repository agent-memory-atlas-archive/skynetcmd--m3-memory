"""Touch ID for sudo: offered once, only at the Mac, verified by reading back.

Hermetic: the PAM files are temp files and `sudo tee` is a stub; nothing here
touches /etc/pam.d.
"""

from __future__ import annotations

import pytest

from conftest import simulate_os
from m3_memory import setup_wizard, touchid


@pytest.fixture
def mac(monkeypatch, tmp_path):
    pam = tmp_path / "pam_tid.so.2"
    pam.write_bytes(b"")
    template = tmp_path / "sudo_local.template"
    template.write_text("# sudo_local\n#auth       sufficient     pam_tid.so\n", encoding="utf-8")
    monkeypatch.setattr(touchid, "_is_mac", lambda: True)
    monkeypatch.setattr(touchid, "PAM_TID", str(pam))
    monkeypatch.setattr(touchid, "TEMPLATE", str(template))
    monkeypatch.setattr(touchid, "SUDO_LOCAL", str(tmp_path / "sudo_local"))
    monkeypatch.setattr(touchid, "_sensor_present", lambda: True)
    for k in ("SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _fake_sudo_tee(monkeypatch, write=True, rc=0):
    def run(argv, input=None, **_kw):
        assert argv[:3] == ["sudo", "tee", "-a"]
        if write:
            with open(argv[3], "a", encoding="utf-8") as fh:
                fh.write(input)
        return type("R", (), {"returncode": rc, "stderr": ""})()

    monkeypatch.setattr(touchid.subprocess, "run", run)


def test_status_reads_the_real_states(mac, monkeypatch):
    assert touchid.status() == "available"
    (mac / "sudo_local").write_text("#auth sufficient pam_tid.so\n", encoding="utf-8")
    assert touchid.status() == "available", "a commented line is not enabled"
    (mac / "sudo_local").write_text("auth       sufficient     pam_tid.so\n", encoding="utf-8")
    assert touchid.status() == "enabled"
    (mac / "sudo_local").unlink()
    monkeypatch.setattr(touchid, "_sensor_present", lambda: False)
    assert touchid.status() == "no-sensor"
    monkeypatch.setattr(touchid, "_is_mac", lambda: False)
    assert touchid.status() == "unsupported"


def test_enable_appends_one_line_and_checks_the_file(mac, monkeypatch):
    _fake_sudo_tee(monkeypatch)
    ok, detail = touchid.enable()
    assert ok, detail
    assert (mac / "sudo_local").read_text(encoding="utf-8") == touchid.LINE + "\n"


def test_enable_does_not_claim_success_the_file_does_not_show(mac, monkeypatch):
    _fake_sudo_tee(monkeypatch, write=False)  # sudo "succeeds", nothing written
    ok, detail = touchid.enable()
    assert not ok and "no active pam_tid line" in detail


def test_setup_offers_only_at_the_mac_and_remembers_a_no(mac, monkeypatch):
    simulate_os(monkeypatch, "Darwin")
    monkeypatch.setattr(setup_wizard, "_stdin_is_interactive", lambda: True)
    monkeypatch.setenv("M3_CONFIG_ROOT", str(mac / "config"))
    asked = []
    monkeypatch.setattr(setup_wizard, "_ask_yes_no", lambda q, default=True: asked.append(q) or False)

    monkeypatch.setenv("SSH_CONNECTION", "192.0.2.2 5000 192.0.2.1 22")
    setup_wizard._offer_touchid_sudo()
    assert asked == [], "no fingerprint reaches an SSH session"

    monkeypatch.delenv("SSH_CONNECTION")
    setup_wizard._offer_touchid_sudo()
    setup_wizard._offer_touchid_sudo()
    assert len(asked) == 1, "a no is remembered"
    assert not (mac / "sudo_local").exists()


def test_enable_by_name_overrides_an_earlier_no(mac, monkeypatch, capsys):
    marker = mac / "config" / touchid.DECLINED
    marker.parent.mkdir(exist_ok=True)
    marker.write_text("", encoding="utf-8")
    _fake_sudo_tee(monkeypatch)
    assert touchid.main("enable") == 0
    assert touchid.status() == "enabled"
    assert not marker.exists(), "the no is withdrawn by asking for it"
    assert touchid.TEST in capsys.readouterr().out


def test_enable_when_already_on_runs_no_sudo(mac, monkeypatch, capsys):
    (mac / "sudo_local").write_text(touchid.LINE + "\n", encoding="utf-8")
    monkeypatch.setattr(touchid.subprocess, "run", lambda *a, **k: pytest.fail("sudo must not run"))
    assert touchid.main("enable") == 0
    assert "already on" in capsys.readouterr().out


def test_enable_without_a_sensor_says_so_and_fails(mac, monkeypatch, capsys):
    monkeypatch.setattr(touchid, "_sensor_present", lambda: False)
    monkeypatch.setattr(touchid.subprocess, "run", lambda *a, **k: pytest.fail("sudo must not run"))
    assert touchid.main("enable") == 1
    assert "no Touch ID sensor" in capsys.readouterr().out


def test_the_offer_is_silent_when_there_is_nothing_to_offer(mac, monkeypatch):
    (mac / "sudo_local").write_text(touchid.LINE + "\n", encoding="utf-8")
    touchid.offer(lambda q: pytest.fail("already on: no question"), print)


def test_doctor_tip_only_on_a_mac_that_could_use_it(mac, monkeypatch, capsys):
    from doctor import touchid_probe

    touchid_probe.run(brief=True)
    assert "m3 touchid enable" in capsys.readouterr().out
    (mac / "sudo_local").write_text(touchid.LINE + "\n", encoding="utf-8")
    touchid_probe.run(brief=True)
    assert capsys.readouterr().out == "", "on: brief doctor stays quiet"
    monkeypatch.setattr(touchid, "_is_mac", lambda: False)
    touchid_probe.run(brief=False)
    assert capsys.readouterr().out == ""


def test_install_script_asks_on_the_terminal_not_the_download_pipe():
    """Under `curl | bash`, setup's input is the download, so its own offer never fires."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")
    assert "m3 touchid offer </dev/tty" in src


def test_over_ssh_the_offer_points_at_the_command_without_asking(mac, monkeypatch):
    """A Mac upgraded only over SSH never sees the offer (2026-10-17): say
    how to turn it on, ask nothing, and record no refusal."""
    marker = mac / "config" / touchid.DECLINED
    monkeypatch.setattr(touchid, "_declined_marker", lambda: str(marker))
    monkeypatch.setenv("SSH_CONNECTION", "192.0.2.2 5000 192.0.2.1 22")
    asked, said = [], []
    touchid.offer(lambda q: asked.append(q) or True, said.append)
    assert asked == []
    assert said and "m3 touchid enable" in said[0]
    assert not marker.exists()
