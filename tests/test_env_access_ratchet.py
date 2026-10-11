"""The environment-access inventory is a ratchet (plan 2D.2 rule 2).

bin/check_env_access.py is the one counter (plan "Pre-Registered Metrics"). Two
budgets: production violations (reads, writes, whole-env copies and dotenv loads of
m3 names outside the config package) and test-side env writes of non-OS names
(tests that configure m3 through the environment instead of the ``m3_settings``
fixture, plan 2D.2 rule 1). Each may only go down: a rise fails, and a fall fails
too until the budget is lowered to lock it in, like the OS-seam ratchet.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parents[1] / "bin"

# Current floors. Lower these as readers move to the registry; never raise them.
# 2026-10-11 (re-baseline at 7687acd3): 611 production violations, 1080 test writes.
_BUDGET = 359
_TESTS_BUDGET = 1055


def _cea():
    sys.path.insert(0, str(_BIN))
    try:
        import check_env_access
    finally:
        sys.path.pop(0)
    return check_env_access


def _production_violations() -> int:
    findings, unparsed = _cea().scan_repo()
    assert not unparsed, f"files the gate could not scan (unscanned is not clean): {unparsed}"
    return sum(f.verdict == "violation" for f in findings)


def _test_writes() -> int:
    findings, unparsed = _cea().scan_tests()
    assert not unparsed, f"test files the gate could not scan: {unparsed}"
    return len(findings)


def test_production_env_access_does_not_grow_and_the_budget_is_not_stale():
    n = _production_violations()
    assert n <= _BUDGET, (
        f"production env-access violations rose from {_BUDGET} to {n}. Read the setting through the "
        "config registry (m3_core.config) instead; run `python bin/check_env_access.py` to see the new site."
    )
    assert n == _BUDGET, f"production env-access violations are down to {n}; lower _BUDGET to lock it in"


def test_test_env_writes_do_not_grow_and_the_budget_is_not_stale():
    n = _test_writes()
    assert n <= _TESTS_BUDGET, (
        f"test env writes of non-OS names rose from {_TESTS_BUDGET} to {n}. Configure m3 through the "
        "m3_settings fixture; run `python bin/check_env_access.py --tests` to see the new site."
    )
    assert n == _TESTS_BUDGET, f"test env writes are down to {n}; lower _TESTS_BUDGET to lock it in"


def test_the_ratchet_can_fail(tmp_path, monkeypatch):
    """A planted production read and a planted test write each raise the count."""
    cea = _cea()
    for root in ("bin", "m3_memory", "tests"):
        (tmp_path / root).mkdir()
    (tmp_path / "bin" / "planted.py").write_text("import os\nos.getenv('M3_PLANTED')\n", encoding="utf-8")
    (tmp_path / "tests" / "test_planted.py").write_text(
        "def test_x(monkeypatch):\n    monkeypatch.setenv('M3_PLANTED', '1')\n", encoding="utf-8"
    )
    monkeypatch.setattr(cea, "REPO", tmp_path)
    prod, _ = cea.scan_repo()
    tests, _ = cea.scan_tests()
    assert sum(f.verdict == "violation" for f in prod) == 1
    assert len(tests) == 1
