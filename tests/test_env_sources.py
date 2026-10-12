"""The one env-location sweep (m3_memory.install.env_sources): every place a name
is set, with its value, including Claude Code's ~/.claude.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from m3_memory.install import env_sources


@pytest.mark.parametrize(
    "text, expected",
    [
        ('export X="1"\n', "1"),
        ("export X=1  # m3\n", "1"),
        ("X=2\n", "2"),
        ("  set X=3\n", "3"),
        ("export X='a b'\n", "a b"),
        ('# export X="1"\n', None),
        ('export X="1"\nexport X=0\n', "0"),
        ("export XY=1\n", None),
        ("export M3_CDW_X=1\n", None),
    ],
)
def test_shell_assignment_reads_the_last_effective_assignment(text, expected):
    assert env_sources.shell_assignment(text, "X") == expected


@pytest.fixture
def home(tmp_path, monkeypatch):
    from m3_memory import installer

    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(env_sources, "_home", lambda: h)
    monkeypatch.setattr(installer, "_client_config_sources", lambda: {})
    monkeypatch.setattr(installer, "_scan_registry_env_deprecations", lambda names, unchecked=None: [])
    monkeypatch.chdir(tmp_path)
    return h


def test_claude_json_server_env_is_found_with_its_value(home):
    """`claude mcp add --env` writes the roots here; the sweep read only
    ~/.claude/settings.json, whose mcpServers Claude Code ignores."""
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {"m3_memory": {"env": {"M3_ENGINE_ROOT": "/srv/eng"}}},
                "projects": {"/p": {"mcpServers": {"m3": {"env": {"M3_ENGINE_ROOT": "/srv/other"}}}}},
            }
        ),
        encoding="utf-8",
    )
    found = env_sources.scan(["M3_ENGINE_ROOT"], include_process_env=False)
    assert {(f.value, f.kind) for f in found} == {("/srv/eng", "mcp"), ("/srv/other", "mcp")}
    assert {Path(f.where) for f in found} == {home / ".claude.json"}


def test_rc_and_dotenv_values_and_a_bare_dotenv_name_sets_nothing(home, tmp_path):
    (home / ".bashrc").write_text('export M3_PROBE_X="from-rc"\n', encoding="utf-8")
    (tmp_path / ".env").write_text("M3_PROBE_X=from-dotenv\nM3_PROBE_BARE\n", encoding="utf-8")
    found = env_sources.scan(["M3_PROBE_X", "M3_PROBE_BARE"], include_process_env=False)
    assert {(f.name, f.value, f.kind) for f in found} == {
        ("M3_PROBE_X", "from-rc", "rc"),
        ("M3_PROBE_X", "from-dotenv", "dotenv"),
    }


def test_an_unreadable_config_is_named_not_silently_clean(home):
    (home / ".claude.json").write_text("{not json", encoding="utf-8")
    unchecked: list[str] = []
    assert env_sources.scan(["M3_ENGINE_ROOT"], include_process_env=False, unchecked=unchecked) == []
    assert any(".claude.json" in u for u in unchecked), unchecked


def test_find_env_var_locations_is_built_on_the_sweep(home, monkeypatch):
    from m3_memory import installer

    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"m": {"env": {"PG_URL": "x"}}}}), encoding="utf-8")
    monkeypatch.setenv("PG_URL", "y")
    assert installer._find_env_var_locations("PG_URL") == sorted(
        [str(home / ".claude.json"), "environment (os.environ['PG_URL'])"]
    )


def test_the_sandbox_points_the_sweep_away_from_the_real_home():
    """Without the test's own fixture, the conftest guard already redirects it."""
    assert env_sources._home() != Path.home(), "the sweep would read the developer's real rc and MCP configs"


@pytest.mark.parametrize(
    "name, entry, ours",
    [
        ("memory", {"command": "py"}, True),
        ("m3_memory", {"command": "anything"}, True),
        ("mine", {"command": "D:/tools/pipx/venvs/m3-memory/Scripts/m3.exe"}, True),
        ("mine", {"command": "python", "args": ["/opt/m3/bin/memory_bridge.py"]}, True),
        ("github", {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]}, False),
        ("m3-lookalike", {"command": "m3x"}, False),
    ],
)
def test_m3s_own_mcp_entry_is_recognised_and_no_other(name, entry, ours):
    """import-env takes a secret only from m3's own entry; another tool's key in
    its own entry is report-only (m3 c7082b20)."""
    assert env_sources.is_m3_server(name, entry) is ours


def test_an_m3_lookalike_file_name_is_not_m3():
    """The exact entry-point rule, not a substring (m3 0ed9169d)."""
    assert not env_sources.is_m3_server("mine", {"command": "python", "args": ["/x/my_memory_bridge_wrapper.py"]})
