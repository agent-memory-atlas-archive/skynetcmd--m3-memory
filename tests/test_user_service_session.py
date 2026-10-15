"""A shell without a usable systemd user session (measured on a Proxmox host,
2026-10-15, `su bhaba` from root): m3 reruns itself into a running manager the
shell lost, names the one-time fix when there is none, and never retires the old
embedder where the new service could not start."""

from __future__ import annotations

import os

import pytest

from conftest import simulate_os


@pytest.mark.parametrize("socket", [("bus",), ("systemd", "private")])
def test_the_manager_is_found_on_either_socket(monkeypatch, tmp_path, socket):
    """No user D-Bus installed: only systemd/private exists, and systemctl
    --user works through it."""
    from m3_memory import _platform

    simulate_os(monkeypatch, "Linux")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert _platform.user_service_runtime_dir() is None, "no socket, no manager"
    path = tmp_path.joinpath(*socket)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    assert _platform.user_service_runtime_dir() == str(tmp_path)


def test_there_is_no_user_manager_off_linux(monkeypatch, tmp_path):
    from m3_memory import _platform

    (tmp_path / "bus").write_text("", encoding="utf-8")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    for name in ("Windows", "Darwin"):
        simulate_os(monkeypatch, name)
        assert _platform.user_service_runtime_dir() is None


def test_m3_reruns_itself_into_a_running_session_the_shell_lost(monkeypatch, tmp_path):
    from m3_memory import _platform, cli

    simulate_os(monkeypatch, "Linux")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(_platform, "user_service_runtime_dir", lambda: str(tmp_path))
    ran = []
    monkeypatch.setattr(_platform, "reexec", lambda argv, env=None: ran.append((argv, env)))
    cli._join_user_session()
    ((argv, env),) = ran
    assert env["XDG_RUNTIME_DIR"] == str(tmp_path) and argv[0]
    assert "XDG_RUNTIME_DIR" not in os.environ, "this process's environment is never changed"


@pytest.mark.parametrize("case", ["set", "no-manager", "windows"])
def test_m3_does_not_rerun_otherwise(monkeypatch, tmp_path, case):
    from m3_memory import _platform, cli

    simulate_os(monkeypatch, "Windows" if case == "windows" else "Linux")
    if case == "set":
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    else:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(_platform, "user_service_runtime_dir", lambda: None if case == "no-manager" else str(tmp_path))
    monkeypatch.setattr(_platform, "reexec", lambda *a, **k: pytest.fail("reran"))
    cli._join_user_session()


def test_setup_names_the_fix_once_when_there_is_no_manager(monkeypatch, capsys):
    from m3_memory import _platform, setup_wizard

    simulate_os(monkeypatch, "Linux")
    monkeypatch.setattr(_platform, "user_service_runtime_dir", lambda: None)
    setup_wizard._check_user_services()
    out = capsys.readouterr()
    text = out.out + out.err
    assert "loginctl enable-linger" in text and text.count("enable-linger") == 1


def test_a_working_session_says_nothing(monkeypatch, tmp_path, capsys):
    from m3_memory import _platform, setup_wizard

    simulate_os(monkeypatch, "Linux")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(_platform, "user_service_runtime_dir", lambda: str(tmp_path))
    setup_wizard._check_user_services()
    out = capsys.readouterr()
    assert out.out + out.err == ""


def test_the_old_embedder_is_kept_where_this_process_cannot_start_the_new_one(monkeypatch, tmp_path):
    """A manager runs but this process lacks XDG_RUNTIME_DIR: retiring the old
    server would leave no embedder at all."""
    import m3_core.seams as seams
    from doctor import job_inventory as ji

    simulate_os(monkeypatch, "Linux")
    monkeypatch.setattr(seams, "user_service_runtime_dir", lambda: str(tmp_path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert ji._service_manager_reachable() is False
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert ji._service_manager_reachable() is True
