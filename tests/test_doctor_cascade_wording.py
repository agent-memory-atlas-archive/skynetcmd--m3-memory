"""A ❌ must not be paired with reassuring words.

`cascade_probe`'s brief line printed, for shared mode, a fixed sentence:

    ❌ embedding-cascade: broken — shared tier-2 embedder online
                                   (tier-1 appropriately offline, 156ms)

The glyph said broken; the words said "online" and "appropriately offline"; and
nothing named a cause or a remedy. Observed on claude-dev 2026-09-30 while the
shared embedder was being restarted. A reader has to decide which half to
believe, and the reassuring half is the one that sounds authoritative.

The reassuring phrasing is now gated on `summary == "healthy"`. Unhealthy shared
mode reports the tier-2 state and names `m3 doctor --fix`. These tests pin both
halves, because the fix is one `and` away from regressing and the failure mode
is silent — the line still prints, it just lies.
"""
from __future__ import annotations

import io
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "bin"))

from doctor import cascade_probe  # noqa: E402

_REASSURING = ("appropriately offline", "embedder online")


def _run_brief(monkeypatch, summary: str, *, shared: bool = True) -> str:
    """Drive the brief path with a synthetic doctor result.

    `run()` does `from memory.doctor import memory_doctor_impl` INSIDE the
    function, so patching an attribute on cascade_probe does nothing — the first
    draft of this helper did that and every assertion read the
    "unavailable (module not importable)" early-return line instead. Stub the
    import TARGET in sys.modules, which also keeps these tests hermetic: the real
    `memory` package pulls yaml/torch and is not importable on a bare checkout,
    so patching the real module would make this file skip exactly where it is
    cheapest to run.
    """
    payload = {
        "summary": summary,
        "tier_1": {"status": "off", "shared_mode": shared},
        "tier_2": {"status": "ok" if summary == "healthy" else "unreachable"},
        "roundtrip": {"latency_ms": 156},
    }

    async def _fake_impl():
        return payload

    pkg = types.ModuleType("memory")
    pkg.__path__ = []           # mark it a package so `memory.doctor` resolves
    doctor = types.ModuleType("memory.doctor")
    doctor.memory_doctor_impl = _fake_impl
    # setitem so monkeypatch restores sys.modules exactly, including the case
    # where the real modules were already imported by a sibling test.
    monkeypatch.setitem(sys.modules, "memory", pkg)
    monkeypatch.setitem(sys.modules, "memory.doctor", doctor)

    buf = io.StringIO()
    with redirect_stdout(buf):
        cascade_probe.run(brief=True)
    return buf.getvalue().strip()


@pytest.mark.parametrize("summary", ["broken", "degraded", "unknown"])
def test_an_unhealthy_cascade_is_not_described_reassuringly(monkeypatch, summary):
    """THE regression: no reassuring phrase may appear on a non-healthy line."""
    line = _run_brief(monkeypatch, summary)
    assert summary in line, f"the summary must still be stated: {line!r}"
    for phrase in _REASSURING:
        assert phrase not in line, (
            f"{summary!r} line still contains reassuring wording {phrase!r} — a "
            f"reader cannot tell whether this is a problem: {line!r}"
        )


@pytest.mark.parametrize("summary", ["broken", "degraded"])
def test_an_unhealthy_cascade_names_a_remedy(monkeypatch, summary):
    """The error/log idiom: say what to DO, in the line the default run prints."""
    line = _run_brief(monkeypatch, summary)
    assert "m3 doctor --fix" in line, f"no remedy named: {line!r}"


def test_an_unhealthy_cascade_reports_the_tier_that_is_wrong(monkeypatch):
    """`tier-1 off by design` alone explains nothing; tier-2's state is the signal."""
    line = _run_brief(monkeypatch, "broken")
    assert "tier-2" in line and "unreachable" in line, (
        f"the failing tier's state must appear: {line!r}"
    )


def test_a_healthy_shared_cascade_KEEPS_the_reassuring_wording(monkeypatch):
    """The other half. Shared mode with tier-1 off IS correct and must not read
    as a problem — otherwise the fix just trades a false alarm for a false one."""
    line = _run_brief(monkeypatch, "healthy")
    assert "healthy" in line
    assert "appropriately offline" in line, (
        "a correct shared-mode cascade must still be described as fine"
    )
    assert "m3 doctor --fix" not in line, "do not prescribe a fix for a healthy state"


def test_brief_mode_still_emits_exactly_one_line(monkeypatch):
    """`brief` is the default; every probe owes exactly one line there."""
    for summary in ("healthy", "degraded", "broken"):
        line = _run_brief(monkeypatch, summary)
        assert len(line.splitlines()) == 1, f"{summary}: {line!r}"
