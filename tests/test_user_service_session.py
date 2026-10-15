"""m3 setup names a missing systemd user session once, with its fix, and uses the
user manager when a `su` shell merely dropped XDG_RUNTIME_DIR (measured on a
Proxmox host, 2026-10-15: every service printed the raw systemctl error)."""

from __future__ import annotations

import os

from conftest import simulate_os


def test_the_bus_is_found_under_the_runtime_dir(monkeypatch, tmp_path):
    from m3_memory import _platform

    simulate_os(monkeypatch, "Linux")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert _platform.user_service_bus() is None, "no socket, no session"
    (tmp_path / "bus").write_text("", encoding="utf-8")
    assert _platform.user_service_bus() == os.path.join(str(tmp_path), "bus")


def test_there_is_no_user_bus_off_linux(monkeypatch, tmp_path):
    from m3_memory import _platform

    (tmp_path / "bus").write_text("", encoding="utf-8")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    for name in ("Windows", "Darwin"):
        simulate_os(monkeypatch, name)
        assert _platform.user_service_bus() is None


def test_setup_names_the_fix_once_when_there_is_no_session(monkeypatch, capsys):
    from m3_memory import _platform, setup_wizard

    simulate_os(monkeypatch, "Linux")
    monkeypatch.setattr(_platform, "user_service_bus", lambda: None)
    setup_wizard._check_user_services()
    out = capsys.readouterr()
    text = out.out + out.err
    assert "loginctl enable-linger" in text and text.count("enable-linger") == 1


def test_setup_names_the_export_when_the_shell_lost_a_running_session(monkeypatch, tmp_path, capsys):
    from m3_memory import _platform, setup_wizard

    simulate_os(monkeypatch, "Linux")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(_platform, "user_service_bus", lambda: str(tmp_path / "bus"))
    setup_wizard._check_user_services()
    out = capsys.readouterr()
    assert f"export XDG_RUNTIME_DIR={tmp_path}" in out.out + out.err
    assert "XDG_RUNTIME_DIR" not in os.environ, "this process's environment is never changed"


def test_a_working_session_says_nothing(monkeypatch, tmp_path, capsys):
    from m3_memory import _platform, setup_wizard

    simulate_os(monkeypatch, "Linux")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(_platform, "user_service_bus", lambda: str(tmp_path / "bus"))
    setup_wizard._check_user_services()
    out = capsys.readouterr()
    assert out.out + out.err == ""
