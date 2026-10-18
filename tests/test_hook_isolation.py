"""A project's PYTHONPATH cannot make m3's hooks import that project's code.

An agent runs hooks in the user's project, with the user's environment. A
project folder named like an m3 package (m3_memory, memory) on PYTHONPATH
shadowed m3's own code (measured 2026-10-18: the session-start launcher imported
the project's m3_memory). Hook lines and the processes hooks start run Python
with m3_memory.hook_launchers.ISOLATE_FLAGS (-E: ignore PYTHON* variables, -s:
ignore the user site folder).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from m3_memory import hook_launchers as hl

ROOT = Path(__file__).resolve().parents[1]


def _trap(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    for pkg in ("m3_memory", "memory"):
        (project / pkg).mkdir(parents=True)
        (project / pkg / "__init__.py").write_text(f'raise SystemExit("SHADOWED: project {pkg}")\n', encoding="utf-8")
    return project


def test_a_hook_line_ignores_a_shadowing_pythonpath(tmp_path):
    project = _trap(tmp_path)
    launcher = tmp_path / "claude-session-start.py"
    launcher.write_text(hl.launcher_text("claude-session-start"), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(project))
    r = subprocess.run(
        [sys.executable, *hl.ISOLATE_FLAGS, str(launcher)],
        input=json.dumps({"source": "resume"}),
        capture_output=True,
        text=True,
        timeout=120,
        cwd=project,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert "SHADOWED" not in r.stdout + r.stderr, r.stdout + r.stderr
    assert "systemMessage" in json.loads(r.stdout)

    without = subprocess.run(  # the guard can fail: the same run without the flags is shadowed
        [sys.executable, str(launcher)],
        input="{}",
        capture_output=True,
        text=True,
        timeout=120,
        cwd=project,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert "SHADOWED" in without.stdout + without.stderr


def test_every_setup_hook_line_carries_the_flags():
    for name in hl.HOOKS:
        line = hl.command_for(name, "/py/python")
        assert line.startswith("/py/python -E -s "), line


def test_capture_hooks_start_their_ingest_child_isolated(monkeypatch, tmp_path):
    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout='{"written": 1}', stderr="")

    for name in ("claude_code_precompact", "codex_stop", "antigravity_stop"):
        spec = importlib.util.spec_from_file_location(f"_iso_{name}", ROOT / "bin" / "hooks" / "chatlog" / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        mod.run_ingest("/py/python", tmp_path / "chatlog_ingest.py", ["--format", "x"])
    assert seen and all(argv[1:3] == ["-E", "-s"] for argv in seen), seen
