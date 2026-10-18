"""The agent plugins (Claude Code, Codex, Gemini CLI) and `m3 hook`, the one
command their hook files run.

- `m3 hook <name>` runs the named hook from ``hook_launchers.HOOKS`` and hands
  back its exit code (a planted hook script proves it ran, with its arguments);
- every hook line in a plugin names a hook that exists, on the events m3 means;
- each hook line's PATH guard fails loudly when `m3` is missing and passes the
  hook's own exit code through when it is present (POSIX sh, and cmd.exe for
  Codex's ``commandWindows``);
- the Claude plugin's hooks live in ``hooks/claude.json``: a root
  ``hooks/hooks.json`` would also be read by Gemini CLI, which warns about
  Claude's event names in every session;
- the MCP server each plugin starts is the one the Claude plugin starts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import skip_reason
from m3_memory import hook_launchers as hl

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_HOOKS = ROOT / "hooks" / "claude.json"
CODEX_PLUGIN = ROOT / "plugins" / "codex"
CODEX_HOOKS = CODEX_PLUGIN / "hooks" / "hooks.json"
_HOOK_CALL = re.compile(r"\bm3 hook ([a-z-]+)")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _handlers(path: Path) -> dict[str, list[tuple[str | None, dict]]]:
    """event -> [(matcher, handler)] for a hooks file."""
    out: dict[str, list[tuple[str | None, dict]]] = {}
    for event, groups in _load(path)["hooks"].items():
        for group in groups:
            for h in group["hooks"]:
                out.setdefault(event, []).append((group.get("matcher"), h))
    return out


def _hook_named(cmd: str) -> str:
    names = _HOOK_CALL.findall(cmd)
    assert len(names) == 1, f"expected one `m3 hook <name>` in {cmd!r}"
    return names[0]


# ── m3 hook ─────────────────────────────────────────────────────────────────


def test_m3_hook_dispatches_and_returns_the_hook_exit_code(monkeypatch):
    from m3_memory import cli

    seen = []
    monkeypatch.setattr(hl, "run", lambda name, argv=None: seen.append((name, argv)) or 7)
    monkeypatch.setattr(sys, "argv", ["m3", "hook", "codex-capture", "--flag", "x"])
    with pytest.raises(SystemExit) as done:
        cli.main()
    assert done.value.code == 7
    assert seen == [("codex-capture", ["--flag", "x"])]


def test_m3_hook_refuses_an_unknown_name(monkeypatch, capsys):
    from m3_memory import cli

    monkeypatch.setattr(sys, "argv", ["m3", "hook", "no-such-hook"])
    with pytest.raises(SystemExit) as done:
        cli.main()
    assert done.value.code == 2
    assert "claude-capture" in capsys.readouterr().err, "the error lists the hooks there are"


@pytest.mark.parametrize(("body", "code"), [("sys.exit(5)", 5), ("sys.exit()", 0), ("pass", 0), ("sys.exit('x')", 1)])
def test_run_executes_the_hook_script_with_its_arguments(monkeypatch, tmp_path, capsys, body, code):
    from m3_memory import _bin

    hooks = tmp_path / "bin" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "probe.py").write_text(f"import json, sys\nprint(json.dumps(sys.argv[1:]))\n{body}\n", encoding="utf-8")
    monkeypatch.setattr(_bin, "bin_dir", lambda: tmp_path / "bin")
    monkeypatch.setitem(hl.HOOKS, "probe", hl.Hook("hooks/probe.py", ("--agent", "x")))
    monkeypatch.setattr(sys, "argv", list(sys.argv))  # run() rewrites it
    assert hl.run("probe", ["extra"]) == code
    assert json.loads(capsys.readouterr().out) == ["--agent", "x", "extra"]


def test_run_without_a_payload_says_so(monkeypatch, capsys):
    from m3_memory import _bin

    monkeypatch.setattr(_bin, "bin_dir", lambda: None)
    assert hl.run("claude-capture", []) == 1
    assert "m3 reinstall" in capsys.readouterr().err


# ── hook files ──────────────────────────────────────────────────────────────


def test_the_claude_plugin_hooks_are_one_line_each():
    manifest = _load(ROOT / ".claude-plugin" / "plugin.json")
    assert manifest["hooks"] == "./hooks/claude.json"
    assert not (ROOT / "hooks" / "hooks.json").exists(), (
        "Gemini CLI reads <repo>/hooks/hooks.json for the m3 extension and warns about "
        "Claude's Stop/PreCompact in every session; the Claude hooks belong in hooks/claude.json"
    )
    h = _handlers(CLAUDE_HOOKS)
    assert [_hook_named(x["command"]) for _m, x in h["SessionStart"]] == ["claude-session-start"]
    assert [(m, _hook_named(x["command"])) for m, x in h["PreCompact"]] == [("auto", "claude-capture")]
    assert [_hook_named(x["command"]) for _m, x in h["Stop"]] == ["claude-capture"]
    assert set(h) == {"SessionStart", "PreCompact", "Stop"}


def test_the_codex_plugin_hooks_are_one_line_each():
    h = _handlers(CODEX_HOOKS)
    assert set(h) == {"SessionStart", "Stop"}
    assert [(m, _hook_named(x["command"])) for m, x in h["SessionStart"]] == [("startup|resume", "codex-session-start")]
    assert [_hook_named(x["command"]) for _m, x in h["Stop"]] == ["codex-capture"]
    for handlers in h.values():
        for _m, x in handlers:
            assert _hook_named(x["commandWindows"]) == _hook_named(x["command"])
            assert 0 < x["timeout"] <= 60, "Codex hook timeouts are seconds"


@pytest.mark.parametrize("path", [CLAUDE_HOOKS, CODEX_HOOKS], ids=lambda p: p.parent.parent.name)
def test_every_plugin_hook_line_names_a_listed_hook(path):
    for handlers in _handlers(path).values():
        for _m, x in handlers:
            for key in ("command", "commandWindows"):
                if key in x:
                    assert _hook_named(x[key]) in hl.HOOKS, (path, x[key])


def _commands(key: str) -> list[tuple[str, str]]:
    out = []
    for path in (CLAUDE_HOOKS, CODEX_HOOKS):
        for event, handlers in _handlers(path).items():
            for _m, x in handlers:
                if key in x:
                    out.append((f"{path.parent.parent.name}:{event}", x[key]))
    return out


def _guard_cases(tmp_path: Path, cmd: str, run) -> None:
    """Missing m3 -> exit 1 with a repair line; present m3 -> its exit code and args."""
    empty = tmp_path / "empty"
    empty.mkdir()
    r = run(cmd, empty)
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "not on PATH" in r.stderr and "pipx install m3-memory" in r.stderr, r.stderr
    if "capture" in _hook_named(cmd):
        assert "NOT captured" in r.stderr, "a capture hook that did not run says the turn was not captured"


needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason=skip_reason("dep", "no POSIX sh on PATH"))


@needs_sh
@pytest.mark.parametrize(("where", "cmd"), _commands("command"), ids=[w for w, _c in _commands("command")])
def test_posix_hook_line_guards_and_passes_the_exit_code(tmp_path, where, cmd):
    sh = shutil.which("sh")
    assert sh

    def run(c: str, path_dir: Path) -> subprocess.CompletedProcess:
        env = dict(os.environ, PATH=os.pathsep.join([str(path_dir), str(Path(sh).parent)]))
        return subprocess.run(
            [sh, "-c", c],
            input="{}",
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            cwd=tmp_path,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    _guard_cases(tmp_path, cmd, run)
    fake = tmp_path / "fake"
    fake.mkdir()
    exe = fake / "m3"
    exe.write_bytes(b'#!/bin/sh\necho "args: $*"\nexit 7\n')
    exe.chmod(0o755)
    r = run(cmd, fake)
    assert r.returncode == 7, (r.returncode, r.stderr)
    assert r.stdout.strip() == f"args: hook {_hook_named(cmd)}"


@pytest.mark.requires_windows
@pytest.mark.parametrize(
    ("where", "cmd"), _commands("commandWindows"), ids=[w for w, _c in _commands("commandWindows")]
)
def test_windows_hook_line_guards_and_passes_the_exit_code(tmp_path, where, cmd):
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"

    def run(c: str, path_dir: Path) -> subprocess.CompletedProcess:
        env = dict(os.environ, PATH=os.pathsep.join([str(path_dir), str(system32)]))
        return subprocess.run(
            ["cmd.exe", "/d", "/s", "/c", c],
            input="{}",
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            cwd=tmp_path,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    _guard_cases(tmp_path, cmd, run)
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "m3.cmd").write_bytes(b"@echo args: %*\r\n@exit /b 7\r\n")
    r = run(cmd, fake)
    assert r.returncode == 7, (r.returncode, r.stderr)
    assert r.stdout.strip() == f"args: hook {_hook_named(cmd)}"


# ── manifests ───────────────────────────────────────────────────────────────


def _claude_server() -> dict:
    return _load(ROOT / ".claude-plugin" / "plugin.json")["mcpServers"]["memory"]


def test_codex_and_gemini_start_the_server_the_claude_plugin_starts():
    claude = _claude_server()
    codex = _load(CODEX_PLUGIN / ".mcp.json")["mcpServers"]["memory"]
    gemini = _load(ROOT / "gemini-extension.json")["mcpServers"]["memory"]
    for server in (codex, gemini):
        assert server["command"] == claude["command"] == "m3", "a command on PATH (a relative one resolves per cwd)"
        assert not server.get("args"), "bare `m3` is the MCP stdio server"


def test_the_codex_plugin_layout():
    manifest = _load(CODEX_PLUGIN / ".codex-plugin" / "plugin.json")
    assert manifest["name"] == "m3"
    for key in ("skills", "mcpServers", "hooks"):
        rel = manifest[key]
        assert rel.startswith("./"), f"{key}: Codex resolves plugin paths from ./"
        assert (CODEX_PLUGIN / rel).exists(), f"{key} -> {rel} is missing"
    market = _load(ROOT / ".agents" / "plugins" / "marketplace.json")
    (entry,) = [p for p in market["plugins"] if p["name"] == "m3"]
    assert entry["source"] == {"source": "local", "path": "./plugins/codex"}
    assert (ROOT / entry["source"]["path"] / ".codex-plugin" / "plugin.json").is_file()
    skills = sorted(p.parent.name for p in (CODEX_PLUGIN / "skills").glob("*/SKILL.md"))
    assert skills == sorted(["m3-guide", *(f"m3-{p.stem}" for p in (ROOT / "commands").glob("*.md"))])


def test_the_gemini_extension_ships_context_only():
    ext = _load(ROOT / "gemini-extension.json")
    assert ext["name"] == "m3" and re.fullmatch(r"[a-z0-9-]+", ext["name"])
    assert ext["contextFileName"] == "GEMINI.md" and (ROOT / "GEMINI.md").is_file()
    # Gemini also reads these from the extension root; none may carry Gemini config.
    assert not list((ROOT / "commands").rglob("*.toml")), "root commands/ holds Claude's .md commands only"
    assert not (ROOT / "hooks" / "hooks.json").exists()
    assert not (ROOT / "agents").exists(), (
        "Gemini CLI loads <repo>/agents/*.md as extension subagents and reports Claude's "
        "subagents as invalid in every session; they live in claude/agents/"
    )


def test_the_claude_plugin_lists_every_subagent():
    listed = _load(ROOT / ".claude-plugin" / "plugin.json")["agents"]
    on_disk = sorted(f"./claude/agents/{p.name}" for p in (ROOT / "claude" / "agents").glob("*.md"))
    assert sorted(listed) == on_disk and on_disk
