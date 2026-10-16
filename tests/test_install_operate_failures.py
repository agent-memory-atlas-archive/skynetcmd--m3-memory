"""INSTALL / OPERATE surface: writers refuse what they cannot read, and every
reported success is checked.

- A malformed ~/.claude/settings.json is never merged into {} and rewritten
  (that replaced the user's whole file): every m3 writer reads it through
  m3_core.config.claude_settings.read_json_object and refuses.
- claude_mcp_probe never reports "single m3 server" when none is live.
- setup's plugin-disable write is backed up, atomic, order-preserving, and
  reports failure; a failed `claude mcp add` never silently leaves zero servers.
- Elevated PowerShell helpers hide their window and tell a cancelled UAC from
  a failed command; schtasks deletes are re-queried, not trusted.

Every file lives under tmp_path; no test touches the real home or runs
`claude` / `schtasks`.
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from m3_core.config import claude_settings as cs  # noqa: E402

MALFORMED = '{"hooks": {"Stop": []}, "permissions": {"allow": ["Bash"]},'  # truncated


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    return tmp_path


# ── the one reader ──────────────────────────────────────────────────────────


def test_reader_absent_and_empty_are_empty_objects(tmp_path):
    assert cs.read_json_object(tmp_path / "nope.json") == {}
    (tmp_path / "e.json").write_text("  \n", encoding="utf-8")
    assert cs.read_json_object(tmp_path / "e.json") == {}


def test_reader_names_path_and_parse_error(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(MALFORMED, encoding="utf-8")
    with pytest.raises(cs.SettingsUnreadable) as ei:
        cs.read_json_object(p)
    msg = str(ei.value)
    assert str(p) in msg and "not valid JSON" in msg and "line 1" in msg and "did not rewrite" in msg


def test_reader_refuses_a_non_object(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(cs.SettingsUnreadable, match="list, not an object"):
        cs.read_json_object(p)


def test_plugin_disabled_asks_for_the_current_key():
    assert cs.plugin_server_disabled({"disabledMcpServers": ["plugin:m3:memory"]})
    # Only the pre-rename key disabled leaves a current plugin's server live.
    assert not cs.plugin_server_disabled({"disabledMcpServers": ["plugin:m3:m3"]})


# ── generate_configs.install_claude_settings ────────────────────────────────


def test_install_claude_settings_refuses_a_malformed_file(tmp_path):
    import generate_configs

    p = tmp_path / "settings.json"
    p.write_text(MALFORMED, encoding="utf-8")
    # dry_run: computes the canonical settings without writing templates.
    with pytest.raises(cs.SettingsUnreadable):
        generate_configs.install_claude_settings(settings_path=str(p), dry_run=True)
    assert p.read_text(encoding="utf-8") == MALFORMED


def test_write_json_reports_failure(tmp_path, capsys):
    import generate_configs

    target = tmp_path / "missing-dir" / "x.json"
    os.makedirs(target, exist_ok=True)  # a directory where the file should go
    assert generate_configs._write_json(str(target), {"a": 1}) is False
    assert str(target) in capsys.readouterr().out


@pytest.mark.parametrize(
    "os_name,path,want",
    [
        ("Windows", "C:/Users/bhaba/x/pythonw.exe", "C:/Users/bhaba/x/pythonw.exe"),
        ("Windows", "C:/Users/Jo Smith/x/pythonw.exe", '"C:/Users/Jo Smith/x/pythonw.exe"'),
        ("Linux", "/home/u/v/bin/python3", "/home/u/v/bin/python3"),
        ("Linux", "/home/Jo Smith/v/bin/python3", "'/home/Jo Smith/v/bin/python3'"),
    ],
)
def test_hook_paths_are_quoted_only_when_needed(monkeypatch, os_name, path, want):
    import generate_configs

    monkeypatch.setattr(generate_configs, "_is_windows", lambda: os_name == "Windows")
    assert generate_configs._quote_cmd_path(path) == want


def test_m3_command_ownership_matches_native_windows_paths():
    import generate_configs

    assert generate_configs._is_m3_command(r"C:\m3\bin\hooks\chatlog\claude_code_precompact.py", "")
    assert not generate_configs._is_m3_command("my-own-chatlog-tool.exe", "")


# ── claude_mcp_probe ────────────────────────────────────────────────────────


def test_probe_fix_refuses_a_malformed_settings_file(home, monkeypatch):
    from doctor import claude_mcp_probe as p

    sp = home / ".claude" / "settings.json"
    sp.write_text(MALFORMED, encoding="utf-8")
    monkeypatch.setattr(p, "_claude_dir", lambda: str(home / ".claude"))
    changed, backup, detail = p._disable_plugin_server_in_settings()
    assert changed is False and backup is None
    assert str(sp) in detail and "not valid JSON" in detail
    assert sp.read_text(encoding="utf-8") == MALFORMED


def test_probe_fix_keeps_key_order(home, monkeypatch):
    from doctor import claude_mcp_probe as p

    sp = home / ".claude" / "settings.json"
    sp.write_text(json.dumps({"zeta": 1, "alpha": 2}), encoding="utf-8")
    monkeypatch.setattr(p, "_claude_dir", lambda: str(home / ".claude"))
    changed, _backup, _ = p._disable_plugin_server_in_settings()
    assert changed is True
    assert list(json.loads(sp.read_text(encoding="utf-8"))) == ["zeta", "alpha", "disabledMcpServers"]


@pytest.mark.parametrize("enabled", [False, True])
def test_probe_never_reports_one_server_when_none_is_live(monkeypatch, capsys, enabled):
    from doctor import claude_mcp_probe as p

    monkeypatch.setattr(p, "_local_servers", lambda: [])
    monkeypatch.setattr(p, "_direct_servers", lambda: {})
    monkeypatch.setattr(
        p,
        "_plugin_state",
        lambda: {"installed": True, "enabled": enabled, "server_disabled": enabled, "active": False},
    )
    assert p.run(brief=True) == 0
    out = capsys.readouterr().out
    assert "✅" not in out and "no m3 server is live" in out and "m3 setup" in out
    p.run(brief=False)
    out = capsys.readouterr().out
    assert "OK" not in out and "no m3 server is live" in out


# ── setup_wizard ────────────────────────────────────────────────────────────


def test_setup_plugin_disable_refuses_malformed_and_says_so(home, capsys):
    from m3_memory import setup_wizard as sw

    sp = home / ".claude" / "settings.json"
    sp.write_text(MALFORMED, encoding="utf-8")
    assert sw._disable_claude_plugin_server() is False
    assert sp.read_text(encoding="utf-8") == MALFORMED
    out = capsys.readouterr()
    assert str(sp) in (out.out + out.err)


def test_setup_plugin_disable_backs_up_and_keeps_order(home):
    from m3_memory import setup_wizard as sw

    sp = home / ".claude" / "settings.json"
    sp.write_text(json.dumps({"zeta": 1, "alpha": 2}), encoding="utf-8")
    assert sw._disable_claude_plugin_server() is True
    data = json.loads(sp.read_text(encoding="utf-8"))
    assert list(data)[:2] == ["zeta", "alpha"]
    assert set(cs.PLUGIN_SERVER_IDS) <= set(data["disabledMcpServers"])
    assert (home / ".claude" / "settings.json.bak").is_file()


@pytest.mark.parametrize(
    "msg,ok", [("[+] added memory", True), ("[!] x is not valid JSON", False), ("[X] boom", False)]
)
def test_registrar_warnings_are_not_success(msg, ok):
    from m3_memory import setup_wizard as sw

    assert sw._report_registration(msg) is ok


def test_unresolvable_roots_refuse_registration(monkeypatch, capsys):
    from m3_memory import installer as inst
    from m3_memory import setup_wizard as sw

    monkeypatch.setattr(sw.shutil, "which", lambda name: "/usr/bin/claude")

    def boom():
        raise ValueError("[paths] engine_root must be a non-empty path string")

    monkeypatch.setattr(inst, "_canonical_memory_env", boom)
    ran: list = []
    monkeypatch.setattr(sw.subprocess, "run", lambda *a, **k: ran.append(a) or SimpleNamespace(returncode=0))
    assert sw._register_claude_server() is False
    assert ran == []  # nothing removed, nothing added
    out = capsys.readouterr()
    assert "engine_root" in (out.out + out.err)


def test_failed_add_restores_the_previous_registration(home, monkeypatch, capsys):
    from m3_memory import installer as inst
    from m3_memory import setup_wizard as sw

    prior = {"command": "m3", "env": {"M3_ENGINE_ROOT": "/old"}}
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"m3_memory": prior}}), encoding="utf-8")
    monkeypatch.setattr(sw.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(inst, "_canonical_memory_env", lambda: {"M3_ENGINE_ROOT": "/new"})
    monkeypatch.setattr(sw, "_disable_claude_plugin_server", lambda: True)
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        failed_add = cmd[:3] == ["claude", "mcp", "add"]
        return SimpleNamespace(returncode=1 if failed_add else 0, stdout="", stderr="boom" if failed_add else "")

    monkeypatch.setattr(sw.subprocess, "run", fake_run)
    assert sw._register_claude_server() is False
    restore = [c for c in calls if c[:3] == ["claude", "mcp", "add-json"]]
    assert restore and restore[0][-2] == "m3_memory" and json.loads(restore[0][-1]) == prior
    out = capsys.readouterr()
    assert "restored the previous Claude registration" in (out.out + out.err)


def test_failed_add_with_nothing_to_restore_says_none_registered(home, monkeypatch, capsys):
    from m3_memory import installer as inst
    from m3_memory import setup_wizard as sw

    monkeypatch.setattr(sw.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(inst, "_canonical_memory_env", lambda: {"M3_ENGINE_ROOT": "/new"})
    monkeypatch.setattr(
        sw.subprocess,
        "run",
        lambda cmd, *a, **k: SimpleNamespace(
            returncode=1 if cmd[:3] == ["claude", "mcp", "add"] else 0, stdout="", stderr=""
        ),
    )
    assert sw._register_claude_server() is False
    out = capsys.readouterr()
    text = out.out + out.err
    assert "NO m3 server is registered" in text and "M3_ENGINE_ROOT=/new" in text


def test_elevated_powershell_is_hidden_and_catches_a_cancelled_uac():
    from m3_memory import setup_wizard as sw

    ps = sw._elevated_ps("-FilePath 'x.exe'")
    assert "-WindowStyle Hidden" in ps and "-ErrorAction Stop" in ps
    assert f"catch {{ exit {sw._UAC_CANCELLED_RC} }}" in ps


def test_cancelled_uac_is_not_reported_as_a_failed_command(monkeypatch, capsys):
    from m3_memory import setup_wizard as sw

    monkeypatch.setattr(sw.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=sw._UAC_CANCELLED_RC))
    assert sw._runas_schedule_repair_windows("s.py") is False
    out = capsys.readouterr()
    assert "UAC dialog was cancelled" in (out.out + out.err)


def test_schtasks_delete_is_verified_by_requery(monkeypatch, capsys):
    """The elevated shell exits with its LAST delete's code; a task left behind
    by an earlier delete must still be reported."""
    from m3_memory import setup_wizard as sw

    def fake_run(cmd, *a, **k):
        if cmd[0] == "powershell":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        name = cmd[3]  # schtasks /Query /TN <name>
        if name == "m3-left":
            return SimpleNamespace(returncode=0, stdout="m3-left Ready", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="ERROR: The system cannot find the file specified.")

    monkeypatch.setattr(sw.subprocess, "run", fake_run)
    assert sw._runas_delete_tasks_windows(["m3-gone", "m3-left"]) is False
    out = capsys.readouterr()
    assert "m3-left" in (out.out + out.err)
    assert sw._runas_delete_tasks_windows(["m3-gone"]) is True


def test_access_denied_on_requery_is_not_absence(monkeypatch):
    from m3_memory import setup_wizard as sw

    monkeypatch.setattr(
        sw.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="ERROR: Access is denied."),
    )
    assert sw._schtask_exists("m3-x") is True


# ── environment_probe ───────────────────────────────────────────────────────


def test_chatlog_config_failure_is_unknown_not_ok(monkeypatch, capsys):
    from doctor import environment_probe as ep

    monkeypatch.setattr(ep, "_load_settings", lambda: {"hooks": {}})

    class _Broken:
        @staticmethod
        def resolve_config():
            raise RuntimeError("bad chatlog config")

    monkeypatch.setitem(sys.modules, "chatlog_config", _Broken)
    res = ep.check()
    assert res["status"] == "unknown" and "bad chatlog config" in res["config_error"]
    assert ep.run(brief=True) == 0
    out = capsys.readouterr().out
    assert "✅" not in out and "SKIPPED" in out and "bad chatlog config" in out


def test_extract_paths_handles_quoted_paths_with_spaces_and_posix_interpreters():
    from doctor import environment_probe as ep

    assert ep._extract_paths('"C:/Users/Jo Smith/v/pythonw.exe" "C:/Users/Jo Smith/b/hook.py"') == [
        "C:/Users/Jo Smith/v/pythonw.exe",
        "C:/Users/Jo Smith/b/hook.py",
    ]
    assert ep._extract_paths("/home/u/venv/bin/python3 /home/u/b/hook.py") == [
        "/home/u/venv/bin/python3",
        "/home/u/b/hook.py",
    ]
    assert ep._is_interpreter("/home/u/venv/bin/python3") and not ep._is_interpreter("/x/hook.py")
