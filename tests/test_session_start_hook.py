"""bin/hooks/session_start.py: every agent's one-line session-start hook.

The output is one JSON object that Claude Code, Codex and Gemini CLI all read;
a part that fails is named in the status line while the others still report;
stdout is always valid JSON (Gemini rejects anything else)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "bin", "hooks", "session_start.py")


@pytest.fixture
def hook(monkeypatch):
    spec = importlib.util.spec_from_file_location("_m3_session_start", os.path.abspath(_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_capture_line", lambda: "capture: WORKING")
    monkeypatch.setattr(mod, "_version_line", lambda: "m3 2026.10.18.0 · up to date")
    return mod


@pytest.mark.parametrize("agent", ["claude", "codex", "gemini"])
def test_each_agent_gets_the_status_lines(hook, agent):
    out = hook.build(agent, {"source": "startup", "session_id": "s1"})
    assert out == {"systemMessage": "capture: WORKING\nm3 2026.10.18.0 · up to date"}


def test_a_failing_part_is_named_and_the_other_still_reports(hook, monkeypatch):
    spec = importlib.util.spec_from_file_location("_m3_session_start_raw", os.path.abspath(_PATH))
    raw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(raw)
    vc = __import__("m3_core.version_check", fromlist=["status_line"])

    def broken(**_):
        raise RuntimeError("record unreadable")

    monkeypatch.setattr(vc, "status_line", broken)
    monkeypatch.setattr(raw, "_capture_line", lambda: "capture: WORKING")
    out = raw.build("codex", {})
    assert out["systemMessage"].startswith("capture: WORKING\n")
    assert "m3 version unknown (RuntimeError: record unreadable)" in out["systemMessage"]


def test_main_prints_one_json_object_even_on_bad_stdin(hook, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert hook.main(["--agent", "gemini"]) == 0
    assert json.loads(capsys.readouterr().out)["systemMessage"].startswith("capture: WORKING")


def test_an_unknown_agent_is_refused():
    spec = importlib.util.spec_from_file_location("_m3_session_start_arg", os.path.abspath(_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with pytest.raises(SystemExit):
        mod.main(["--agent", "not-an-agent"])
