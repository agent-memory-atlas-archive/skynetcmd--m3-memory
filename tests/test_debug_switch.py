"""M3_DEBUG: one reader (m3_core.config.env, CONFIG_DESIGN §2 read path 3), and the
lines go to stderr: stdout is a tool's result channel."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


def test_debug_lines_go_to_stderr_only_when_switched_on(monkeypatch, capsys):
    from m3_core.config import env

    monkeypatch.delenv("M3_DEBUG", raising=False)
    env.debug("hidden")
    assert capsys.readouterr() == ("", "")
    monkeypatch.setenv("M3_DEBUG", "1")
    env.debug("shown")
    out, err = capsys.readouterr()
    assert out == "" and "DEBUG shown" in err


def test_a_blank_switch_is_off(monkeypatch):
    from m3_core.config import env

    monkeypatch.setenv("M3_DEBUG", "  ")
    assert not env.debug_enabled()
