"""The weekly audit does not run on a new install's first loop start.

A trial install found a "Weekly Audit Summary" of an empty store, written minutes
after setup as a searchable memory, ranking second for an unrelated query. A loop
that has never run had no stamp, so the ~7-day pass read as due at once.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace


def test_first_start_stamps_and_skips_then_runs_after_an_interval(monkeypatch, tmp_path):
    import m3_cognitive_loop as loop
    import weekly_auditor

    runs_file = tmp_path / "pass_runs.json"
    monkeypatch.setattr(loop, "_loop_pass_runs_path", lambda: str(runs_file))
    calls = []
    monkeypatch.setattr(weekly_auditor, "run_audit", lambda: calls.append(1) or {"ok": True})
    args = SimpleNamespace(audit_min_interval_s=7 * 86400)

    asyncio.run(loop.run_audit_pass(args))
    assert calls == [], "a new install's first loop start must not write an audit"
    assert "audit" in json.loads(runs_file.read_text(encoding="utf-8"))

    asyncio.run(loop.run_audit_pass(args))
    assert calls == [], "still inside the first interval"

    week_ago = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    runs_file.write_text(json.dumps({"audit": week_ago}), encoding="utf-8")
    asyncio.run(loop.run_audit_pass(args))
    assert calls == [1], "after an interval the audit runs"
