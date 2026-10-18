"""config_root_section: which m3_config.toml is authoritative, and every place
that would read another. With a non-default config root, a process that does not
see M3_CONFIG_ROOT reads ~/.m3/config instead: two configs, possibly two stores.
Each check is driven with a planted surface and must fire; a clean setup is silent
under --brief (setup/upgrade) so the report is not noise."""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

from m3_memory.install import sections
from m3_memory.install.env_sources import EnvFound


@pytest.fixture
def surfaces(tmp_path, monkeypatch):
    """A custom config root, a default root, and no other surface; tests plant one."""
    custom = tmp_path / "custom-cfg"
    default = tmp_path / "default-cfg"
    custom.mkdir()
    default.mkdir()
    (custom / "m3_config.toml").write_text("schema_version = 1\n", encoding="utf-8")
    state = SimpleNamespace(custom=custom, default=default, found=[], mcp_files=[], unchecked=[])

    cfg = importlib.import_module("m3_core.config")
    roots = importlib.import_module("m3_memory._roots")
    es = importlib.import_module("m3_memory.install.env_sources")
    monkeypatch.setattr(
        cfg,
        "locate",
        lambda: SimpleNamespace(root=custom, file=custom / "m3_config.toml", tier="env:M3_CONFIG_ROOT"),
    )
    monkeypatch.setattr(roots, "default_config_root", lambda: str(default))

    def fake_scan(names, unchecked=None):
        if unchecked is not None:
            unchecked.extend(state.unchecked)
        return list(state.found)

    monkeypatch.setattr(es, "scan", fake_scan)
    monkeypatch.setattr(es, "mcp_config_files", lambda: list(state.mcp_files))
    return state


def _run(capsys, brief=False):
    n = sections.config_root_section(brief=brief)
    return n, capsys.readouterr().out


def test_a_clean_custom_root_reports_the_file_and_no_problem(surfaces, capsys):
    n, out = _run(capsys)
    assert n == 0
    assert str(surfaces.custom / "m3_config.toml") in out
    assert "located by M3_CONFIG_ROOT" in out
    assert "[!]" not in out


def test_brief_is_silent_when_nothing_is_wrong(surfaces, capsys):
    assert _run(capsys, brief=True) == (0, "")


def test_a_place_naming_another_root_is_a_split(surfaces, capsys, tmp_path):
    rc = tmp_path / ".bashrc"
    surfaces.found.append(EnvFound("M3_CONFIG_ROOT", str(tmp_path / "elsewhere"), str(rc), "rc"))
    n, out = _run(capsys, brief=True)
    assert n == 1
    assert f"M3_CONFIG_ROOT in {rc}" in out and "read another config" in out


def test_another_tools_mcp_entry_is_not_a_surface(surfaces, capsys, tmp_path):
    surfaces.found.append(
        EnvFound("M3_CONFIG_ROOT", str(tmp_path / "x"), "cfg.json", "mcp", server="other", m3_server=False)
    )
    assert _run(capsys, brief=True) == (0, "")


def test_an_m3_mcp_entry_without_the_locator_reads_the_default(surfaces, capsys, tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"mcpServers": {"memory": {"command": "m3", "args": [], "env": {}}}}), encoding="utf-8")
    surfaces.mcp_files.append(f)
    n, out = _run(capsys, brief=True)
    assert n == 1
    assert "has no M3_CONFIG_ROOT" in out and str(surfaces.default) in out


def test_a_second_config_file_at_the_default_is_reported_ignored(surfaces, capsys):
    (surfaces.default / "m3_config.toml").write_text("schema_version = 1\n", encoding="utf-8")
    n, out = _run(capsys, brief=True)
    assert n == 1
    assert "also exists and is ignored" in out


def test_at_the_default_root_no_locator_is_needed(surfaces, capsys, monkeypatch, tmp_path):
    """At ~/.m3/config every process finds the file without M3_CONFIG_ROOT: an MCP
    entry lacking it is fine, and no advice to set it is printed."""
    cfg = importlib.import_module("m3_core.config")
    d = surfaces.default
    (d / "m3_config.toml").write_text("schema_version = 1\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "locate", lambda: SimpleNamespace(root=d, file=d / "m3_config.toml", tier="default"))
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"mcpServers": {"memory": {"command": "m3", "args": []}}}), encoding="utf-8")
    surfaces.mcp_files.append(f)
    n, out = _run(capsys)
    assert n == 0
    assert "located by the default location" in out
    assert "set M3_CONFIG_ROOT" not in out


def test_an_unreadable_place_is_named_and_the_all_clear_is_qualified(surfaces, capsys):
    """A place the sweep could not read is said, never folded into "every surface
    names the root" (an all-clear the check did not earn, §3)."""
    surfaces.unchecked.append("/home/u/.zshrc (PermissionError)")
    n, out = _run(capsys)
    assert n == 0
    assert "could not check /home/u/.zshrc (PermissionError)" in out
    assert "every readable m3 surface found names" in out
