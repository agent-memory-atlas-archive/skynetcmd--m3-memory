"""The loop's heartbeat: paused state, failure visibility, and headless streams."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import m3_cognitive_loop as L  # noqa: E402


def test_paused_beat_records_state_and_reason_in_utc(tmp_path, monkeypatch):
    hb = tmp_path / "cfg" / ".loop_heartbeat.json"
    monkeypatch.setattr(L, "_loop_heartbeat_path", lambda: str(hb))
    L._write_heartbeat(3, 60, state="paused", reason="thermal")
    data = json.loads(hb.read_text(encoding="utf-8"))
    assert data["state"] == "paused" and data["reason"] == "thermal"
    assert data["ts"].endswith("Z") and data["interval_s"] == 60

    L._write_heartbeat(4, 60)
    data = json.loads(hb.read_text(encoding="utf-8"))
    assert data["state"] == "running" and "reason" not in data


def test_paused_beat_is_accepted_by_the_watchdog(tmp_path, monkeypatch):
    """End to end over the file: the loop writes a paused beat, the watchdog
    reads it as fresh progress."""
    import m3_loop_watchdog as wd

    hb = tmp_path / ".loop_heartbeat.json"
    monkeypatch.setattr(L, "_loop_heartbeat_path", lambda: str(hb))
    monkeypatch.setattr(wd, "HEARTBEAT", hb)
    L._write_heartbeat(1, 60, state="paused", reason="governor-halted")
    got = wd.read_heartbeat()
    assert got["state"] == "paused" and got["age"] is not None and got["age"] < 60


def test_heartbeat_failure_warns_with_path_and_errno_rate_limited(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(L, "_loop_heartbeat_path", lambda: str(blocker / "hb.json"))
    monkeypatch.setattr(L, "_HB_WARNED_AT", 0.0)
    with caplog.at_level(logging.WARNING, logger="m3_cognitive_loop"):
        L._write_heartbeat(1, 60)
        L._write_heartbeat(2, 60)  # suppressed
    hits = [r for r in caplog.records if "Heartbeat write failed" in r.getMessage()]
    assert len(hits) == 1
    msg = hits[0].getMessage()
    assert str(blocker / "hb.json") in msg and "errno=" in msg


def test_headless_streams_bind_to_the_log_file(tmp_path, monkeypatch):
    """Under pythonw (task foreground, no --background) stdout/stderr are None;
    records and prints must reach --log-file."""
    log = tmp_path / "loop.log"
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    handler = logging.StreamHandler(None)  # stream resolves to the (None) stderr
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        assert handler.stream is None
        L._bind_headless_streams(str(log))
        assert handler.stream is sys.stdout
        print("print-reaches-log")
        logging.getLogger("m3_cognitive_loop").warning("record-reaches-log")
        sys.stdout.flush()
        text = log.read_text(encoding="utf-8")
        assert "print-reaches-log" in text and "record-reaches-log" in text
        # stdout IS the log file now, so no second FileHandler is attached.
        assert L._stdout_is_file(str(log)) is True
    finally:
        root.removeHandler(handler)
        f = sys.stdout
        monkeypatch.undo()
        f.close()


def test_headless_streams_noop_with_real_streams(monkeypatch):
    out, err = sys.stdout, sys.stderr
    L._bind_headless_streams(None)
    assert sys.stdout is out and sys.stderr is err
