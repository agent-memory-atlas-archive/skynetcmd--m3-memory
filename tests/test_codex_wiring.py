"""m3 setup wires Codex CLI: ``[mcp_servers.memory]`` in Codex's config.toml.

Each rule of ``installer._register_codex_mcp`` is planted and checked: present or
absent Codex, a new file, a file with the user's own servers and comments, a
healthy entry, a stale one, the repair-only mode, and an unreadable file. Codex's
config path is a temp dir, so no real config is touched."""

from __future__ import annotations

import tomllib

import pytest


@pytest.fixture
def codex(tmp_path, monkeypatch):
    import importlib

    inst = importlib.import_module("m3_memory.installer")
    home = tmp_path / "codex-home"
    monkeypatch.setattr(inst, "_codex_config_path", lambda: home / "config.toml")
    monkeypatch.setattr(inst.shutil, "which", lambda name: None)
    bridge = tmp_path / "bridge.py"
    bridge.write_text("", encoding="utf-8")
    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    canonical = {"command": str(python), "args": [str(bridge)], "env": {"M3_CONFIG_ROOT": str(tmp_path)}}
    monkeypatch.setattr(inst, "_canonical_memory_server", lambda: dict(canonical))
    return inst, home, canonical


def _read(path):
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_no_codex_means_no_write(codex):
    inst, home, _ = codex
    assert inst._register_codex_mcp() is None
    assert not home.exists()


def test_a_codex_home_gets_the_canonical_entry(codex):
    inst, home, canonical = codex
    home.mkdir()
    msg = inst._register_codex_mcp()
    assert msg and msg.startswith("[+] registered")
    assert _read(home / "config.toml")["mcp_servers"]["memory"] == canonical


def test_the_users_servers_and_comments_are_kept_and_the_file_backed_up(codex):
    inst, home, canonical = codex
    home.mkdir()
    cfg = home / "config.toml"
    cfg.write_text(
        '# my settings\nmodel = "gpt-5"\n\n[mcp_servers.other]\ncommand = "other-server"\n', encoding="utf-8"
    )
    inst._register_codex_mcp()
    text = cfg.read_text(encoding="utf-8")
    assert "# my settings" in text
    data = tomllib.loads(text)
    assert data["model"] == "gpt-5"
    assert data["mcp_servers"]["other"] == {"command": "other-server"}
    assert data["mcp_servers"]["memory"] == canonical
    assert (home / "config.toml.bak").exists(), "an existing file is backed up before the rewrite"


def test_a_healthy_entry_is_left_alone_even_if_customised(codex, tmp_path):
    inst, home, _ = codex
    home.mkdir()
    cfg = home / "config.toml"
    custom = tmp_path / "my-python"
    custom.write_text("", encoding="utf-8")
    cfg.write_text(f'[mcp_servers.memory]\ncommand = "{custom.as_posix()}"\n', encoding="utf-8")
    before = cfg.read_text(encoding="utf-8")
    assert inst._register_codex_mcp() is None
    assert cfg.read_text(encoding="utf-8") == before


def test_a_stale_entry_is_repointed(codex):
    inst, home, canonical = codex
    home.mkdir()
    cfg = home / "config.toml"
    cfg.write_text('[mcp_servers.memory]\ncommand = "/gone/python"\nargs = ["/gone/bridge.py"]\n', encoding="utf-8")
    msg = inst._register_codex_mcp()
    assert msg and msg.startswith("[+] repointed")
    assert _read(cfg)["mcp_servers"]["memory"] == canonical


def test_repair_only_never_adds_an_entry(codex):
    inst, home, _ = codex
    home.mkdir()
    cfg = home / "config.toml"
    cfg.write_text('model = "gpt-5"\n', encoding="utf-8")
    assert inst._register_codex_mcp(repair_only=True) is None
    assert cfg.read_text(encoding="utf-8") == 'model = "gpt-5"\n'


def test_an_unreadable_file_is_reported_and_left_unchanged(codex):
    inst, home, _ = codex
    home.mkdir()
    cfg = home / "config.toml"
    cfg.write_text("[mcp_servers.memory\nbroken", encoding="utf-8")
    msg = inst._register_codex_mcp()
    assert msg and msg.startswith("[!]") and "not wired" in msg
    assert cfg.read_text(encoding="utf-8") == "[mcp_servers.memory\nbroken"


def test_an_unknown_agent_name_is_refused_with_the_valid_names():
    import argparse

    from m3_memory import setup_wizard as w

    args = argparse.Namespace(
        endpoint=None,
        cognitive_loop=False,
        non_interactive=True,
        agents="codex,not-an-agent",
        capture_mode=None,
        install_gpu_embedder=False,
    )
    with pytest.raises(SystemExit, match=r"not-an-agent.*choose from: .*codex"):
        w._gather_plan(w.AgentTargets(), args)


def test_codex_home_moves_the_config(monkeypatch, tmp_path):
    import importlib

    from m3_memory.install.env_sources import EnvFound

    inst = importlib.import_module("m3_memory.installer")
    es = importlib.import_module("m3_memory.install.env_sources")
    monkeypatch.setattr(
        es, "scan", lambda names: [EnvFound("CODEX_HOME", str(tmp_path / "ch"), "environment", "process")]
    )
    assert inst._codex_config_path() == tmp_path / "ch" / "config.toml"
    monkeypatch.setattr(es, "scan", lambda names: [])
    assert inst._codex_config_path().parent.name == ".codex"


def test_with_the_m3_codex_plugin_setup_writes_neither_server_nor_hooks(codex):
    """The plugin brings the server and the hooks; config.toml/hooks.json entries
    as well would start a second server and capture every turn twice."""
    inst, home, _ = codex
    (home / "plugins" / "cache" / "skynetcmd" / "m3" / "2026.10.18.0").mkdir(parents=True)
    assert inst._register_codex_mcp() is None
    assert inst._register_codex_hooks() is None
    assert not (home / "config.toml").exists() and not (home / "hooks.json").exists()


def test_a_leftover_entry_beside_the_plugin_is_reported_with_its_fix(codex):
    inst, home, _ = codex
    (home / "plugins" / "cache" / "skynetcmd" / "m3").mkdir(parents=True)
    (home / "config.toml").write_text('[mcp_servers.memory]\ncommand = "m3"\n', encoding="utf-8")
    msg = inst._register_codex_mcp()
    assert msg and msg.startswith("[!]") and "two m3 servers" in msg and "delete that table" in msg


def test_codex_hooks_are_launcher_lines_and_keep_the_users_entries(codex):
    import json

    from m3_memory import hook_launchers

    inst, home, _ = codex
    home.mkdir()
    mine = {"hooks": [{"type": "command", "command": "my-own-hook"}]}
    (home / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [mine]}}), encoding="utf-8")
    msg = inst._register_codex_hooks()
    assert msg and msg.startswith("[+]") and "/hooks" in msg
    data = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
    start = data["hooks"]["SessionStart"][-1]["hooks"][0]["command"]
    stop = data["hooks"]["Stop"]
    assert hook_launchers.is_launcher_command(start) and start.endswith("codex-session-start.py")
    assert stop[0] == mine and stop[-1]["hooks"][0]["command"].endswith("codex-capture.py")
    assert "SessionEnd" not in data["hooks"], "Codex caps SessionEnd at 3 s: too short for an ingest"
    assert inst._register_codex_hooks() is None, "a current file is left alone"


def test_codex_hooks_repair_only_adds_nothing(codex):
    inst, home, _ = codex
    home.mkdir()
    assert inst._register_codex_hooks(repair_only=True) is None
    assert not (home / "hooks.json").exists()
