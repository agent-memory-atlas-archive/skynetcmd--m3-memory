"""`m3 setup` with "keep the current settings" still repairs agent wiring that
points at an older install (an upgrade from a release without `m3 upgrade`
arrives with hooks on its payload clone), and adds no agent."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


def _plan(sw, *, kept: bool):
    plan = sw.SetupPlan()
    plan.targets = sw.AgentTargets()
    plan.agents_kept = kept
    return plan


def _record_repairs(monkeypatch, sw, wired: bool, agents=()) -> list[str]:
    import doctor.agent_paths_probe as ap
    import doctor.claude_mcp_probe as cm
    import doctor.environment_probe as ep

    from m3_memory import installer

    calls: list[str] = []
    monkeypatch.setattr(installer, "claude_uses_m3", lambda: wired)
    monkeypatch.setattr(installer, "_heal_config_bridge_path", lambda apply: [])
    monkeypatch.setattr(installer, "_scan_agent_configs", lambda: list(agents))
    monkeypatch.setattr(installer, "_heal_agent_settings", lambda path, **k: calls.append(f"heal {path}") or None)
    monkeypatch.setattr(sw, "_register_claude_server", lambda: calls.append("register") or True)
    monkeypatch.setattr(ep, "repair", lambda dry_run=False: calls.append("hooks") or {"actions": []})
    monkeypatch.setattr(ap, "run", lambda brief=False, fix=False: calls.append(f"paths fix={fix}") or 0)
    monkeypatch.setattr(cm, "run", lambda brief=False, fix=False: calls.append(f"claude fix={fix}") or 0)
    for name in ("_wire_claude", "_wire_gemini", "_wire_cursor"):
        monkeypatch.setattr(sw, name, lambda *a, **k: calls.append("ADDED AN AGENT"))
    return calls


def test_keeping_the_settings_runs_the_doctor_repairs(monkeypatch):
    from m3_memory import setup_wizard as sw

    calls = _record_repairs(monkeypatch, sw, wired=True)
    assert sw._step_wire_agents(_plan(sw, kept=True))
    assert calls == ["register", "hooks", "paths fix=True", "claude fix=True"]


def test_an_agent_not_wired_to_m3_is_not_registered(monkeypatch):
    from m3_memory import setup_wizard as sw

    calls = _record_repairs(monkeypatch, sw, wired=False)
    sw._step_wire_agents(_plan(sw, kept=True))
    assert calls == [], "Claude does not use m3: nothing of Claude's is touched"


def test_no_agents_selected_touches_nothing(monkeypatch):
    from m3_memory import setup_wizard as sw

    calls = _record_repairs(monkeypatch, sw, wired=True)
    sw._step_wire_agents(_plan(sw, kept=False))
    assert calls == []


def test_a_local_scope_registration_counts_as_wired(tmp_path):
    """The N5 install registered `memory` for one project directory only."""
    from m3_memory import installer

    f = tmp_path / ".claude.json"
    entry = {"command": "mcp-memory", "args": [], "env": {}}
    f.write_text(json.dumps({"projects": {"/home/u": {"mcpServers": {"memory": entry}}}}), encoding="utf-8")
    assert installer.claude_wired_to_m3(f)
    f.write_text(json.dumps({"mcpServers": {"other": {"command": "npx", "args": ["x"]}}}), encoding="utf-8")
    assert not installer.claude_wired_to_m3(f)
    assert not installer.claude_wired_to_m3(tmp_path / "absent.json")


def test_a_failing_repair_does_not_fail_setup(monkeypatch, capsys):
    from m3_memory import setup_wizard as sw

    _record_repairs(monkeypatch, sw, wired=True)
    monkeypatch.setattr(sw, "_register_claude_server", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert sw._step_wire_agents(_plan(sw, kept=True))
    captured = capsys.readouterr()
    assert "m3 doctor --fix --fix-hooks" in captured.out + captured.err


def test_a_claude_only_user_is_not_told_no_agent_uses_m3(monkeypatch, capsys):
    """The agent scans read settings files; Claude Code keeps its servers in
    ~/.claude.json, so a Claude-only install read 'no agent is set up'."""
    import doctor.agent_paths_probe as ap

    from m3_memory import installer

    monkeypatch.setattr(ap, "_scan_mcpservers_hosts", list)
    monkeypatch.setattr(ap, "_scan_opencode", list)
    monkeypatch.setattr(ap, "_scan_hermes", list)
    monkeypatch.setattr(ap, "_scan_stale_payload", list)
    for wired, expected in ((True, "Claude Code is wired"), (False, "no agent is set up to use m3 yet")):
        monkeypatch.setattr(installer, "claude_wired_to_m3", lambda wired=wired: wired)
        ap.run(brief=True, fix=False)
        assert expected in capsys.readouterr().out


def test_only_an_existing_stale_agent_entry_is_repointed(monkeypatch):
    """Keeping the settings never adds m3 to an agent that does not use it."""
    from m3_memory import setup_wizard as sw

    agents = [("Gemini", "/g/settings.json", True), ("Cursor", "/c/mcp.json", False)]
    calls = _record_repairs(monkeypatch, sw, wired=False, agents=agents)
    sw._step_wire_agents(_plan(sw, kept=True))
    assert calls == ["heal /g/settings.json"]


def test_claude_uses_m3_through_hooks_alone(tmp_path):
    from m3_memory import installer

    settings = tmp_path / "settings.json"
    settings.write_text('{"hooks": {"Stop": [{"hooks": [{"command": "python /x/bin/hooks/chatlog/stop.py"}]}]}}')
    assert installer.claude_uses_m3(tmp_path / "absent.json", settings)
    settings.write_text('{"model": "sonnet"}')
    assert not installer.claude_uses_m3(tmp_path / "absent.json", settings)
