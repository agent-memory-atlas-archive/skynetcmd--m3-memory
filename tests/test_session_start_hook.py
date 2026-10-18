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
def test_each_agent_gets_the_status_lines(hook, agent, monkeypatch):
    monkeypatch.setattr(hook, "_recall_menu", lambda agent, cwd: "")
    out = hook.build(agent, {"source": "startup", "session_id": "s1"})
    assert out == {"systemMessage": "capture: WORKING\nm3 2026.10.18.0 · up to date"}


def test_a_fresh_start_gets_the_recall_menu_as_context(hook, monkeypatch):
    seen = []
    monkeypatch.setattr(hook, "_recall_menu", lambda agent, cwd: seen.append((agent, cwd)) or "m3: recent sessions…")
    out = hook.build("codex", {"source": "startup", "cwd": "/work/site"})
    assert out["hookSpecificOutput"] == {"hookEventName": "SessionStart", "additionalContext": "m3: recent sessions…"}
    assert seen == [("codex", "/work/site")]


@pytest.mark.parametrize("source", ["resume", "compact", "fork"])
def test_no_menu_when_the_session_already_has_its_context(hook, monkeypatch, source):
    monkeypatch.setattr(hook, "_recall_menu", lambda agent, cwd: pytest.fail("asked for a menu"))
    assert "hookSpecificOutput" not in hook.build("claude", {"source": source})


def test_recall_unavailable_is_shown_to_the_person(hook, monkeypatch):
    monkeypatch.setattr(
        hook, "_recall_menu", lambda agent, cwd: "m3 recall unavailable: store locked; inspect: `m3 doctor`"
    )
    out = hook.build("gemini", {"source": "startup"})
    assert "hookSpecificOutput" not in out
    assert out["systemMessage"].endswith("m3 recall unavailable: store locked; inspect: `m3 doctor`")


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
