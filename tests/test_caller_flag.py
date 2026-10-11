"""`m3 --caller upgrade <command>`: the upgrade handshake as a flag (user decision
2026-10-11, m3 c8b803b5; it was the env var M3_SETUP_CALLER)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_an_m3_child_of_an_upgraded_step_gets_the_flag(monkeypatch):
    """setup runs `m3 embedder ...` as a child, and embedder_admin checks the
    caller: the flag has to reach it, since nothing is inherited through env."""
    from m3_memory import setup_wizard
    from m3_memory.wizard import ui

    monkeypatch.setattr(ui, "_caller", None)
    assert "--caller" not in setup_wizard._m3_cli("embedder", "install")
    monkeypatch.setattr(ui, "_caller", "upgrade")
    argv = setup_wizard._m3_cli("embedder", "install")
    assert argv[argv.index("m3_memory.cli") + 1 :] == ["--caller", "upgrade", "embedder", "install"]


def test_m3_help_lists_the_flag_the_upgrader_probes_for():
    """m3_upgrade passes --caller only when the target's `m3 --help` lists it."""
    proc = subprocess.run(
        [sys.executable, "-m", "m3_memory.cli", "--help"], cwd=_ROOT, capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    assert "--caller {upgrade}" in proc.stdout


def test_the_caller_is_never_read_from_env():
    src = (_ROOT / "m3_memory" / "wizard" / "ui.py").read_text(encoding="utf-8")
    assert "os.environ.get(UPGRADE_CALLER_ENV" not in src and "UPGRADE_CALLER_ENV =" not in src
