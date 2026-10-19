"""A pass that exits does not end the cognitive loop, and m3 doctor names it.

Passes reach code that calls sys.exit on a configuration problem (the entity
pass, on a missing API key). SystemExit is not an Exception, so it passed every
`except Exception` and ended the process; the service manager restarted the loop
every few seconds and no pass, summaries included, ever ran.
"""

from __future__ import annotations

import asyncio
import json
import logging

import m3_cognitive_loop as cl

REASON = "ERROR: API key not found. observed: no LM_API_TOKEN. fix: `m3 secret set LM_API_TOKEN`"


def _pass(name, fn):
    return {"name": name, "run": fn}


def test_a_pass_that_exits_is_turned_off_and_logged(caplog):
    async def exits(_args):
        raise SystemExit(REASON)

    stopped: dict[str, str] = {}
    caplog.set_level(logging.ERROR, logger=cl.logger.name)
    asyncio.run(cl._run_pass(_pass("entities", exits), None, stopped))  # does not raise
    assert stopped == {"entities": REASON}
    assert "entities pass exited" in caplog.text and "LM_API_TOKEN" in caplog.text


def test_a_pass_that_returns_stays_on():
    ran = []

    async def works(_args):
        ran.append(True)

    stopped: dict[str, str] = {}
    asyncio.run(cl._run_pass(_pass("embed", works), None, stopped))
    assert ran == [True] and stopped == {}


def test_a_keyboard_interrupt_still_stops_the_loop():
    async def interrupted(_args):
        raise KeyboardInterrupt

    try:
        asyncio.run(cl._run_pass(_pass("sync", interrupted), None, {}))
    except KeyboardInterrupt:
        return
    raise AssertionError("a KeyboardInterrupt from a pass was swallowed")


def test_the_heartbeat_names_stopped_passes():
    cl._write_heartbeat(3, 300, stopped={"entities": REASON})
    with open(cl._loop_heartbeat_path(), encoding="utf-8") as f:
        beat = json.load(f)
    assert beat["stopped_passes"] == {"entities": REASON} and beat["state"] == "running"
    cl._write_heartbeat(4, 300)
    with open(cl._loop_heartbeat_path(), encoding="utf-8") as f:
        assert "stopped_passes" not in json.load(f), "a clean cycle must not carry stale stopped passes"


def test_doctor_names_the_stopped_pass_and_its_fix(monkeypatch, capsys):
    from doctor import cognitive_loop_probe as probe

    monkeypatch.setattr(probe, "_installed_active", lambda: (True, True, "systemd"))
    monkeypatch.setattr(probe, "_process_running", lambda: True)
    monkeypatch.setattr(probe, "_entity_stats", lambda: (10, 4))
    cl._write_heartbeat(3, 300, stopped={"entities": REASON})
    probe.run(brief=True)
    out = capsys.readouterr().out
    assert "entities pass is off" in out and "m3 secret set LM_API_TOKEN" in out, out

    cl._write_heartbeat(4, 300)  # healthy: no stopped passes, no warning
    probe.run(brief=True)
    assert "✅ cognitive loop: OK" in capsys.readouterr().out


def test_doctor_down_remedy_points_at_the_log_not_setup(monkeypatch, capsys):
    from doctor import cognitive_loop_probe as probe

    monkeypatch.setattr(probe, "_installed_active", lambda: (True, False, "systemd"))
    monkeypatch.setattr(probe, "_process_running", lambda: False)
    monkeypatch.setattr(probe, "_entity_stats", lambda: (10, 4))
    probe.run(brief=True)
    out = capsys.readouterr().out
    assert "cognitive_loop.log" in out and "m3 doctor --fix" in out and "`m3 setup`" not in out, out
