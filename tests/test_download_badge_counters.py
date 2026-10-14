"""The public download/clone counters (user decision 2026-10-13: the per-day history
is private; the repo keeps only a running total and the last complete day counted)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import gen_download_badges as g  # noqa: E402


def test_only_complete_days_after_the_cursor_are_added():
    counter = {"total": 100, "through": "2026-10-10"}
    days = [("2026-10-09", 5), ("2026-10-10", 7), ("2026-10-11", 3), ("2026-10-12", 4), ("2026-10-13", 99)]
    new, warnings = g.advance_counter(counter, days, today="2026-10-13", window_days=180, label="PyPI")
    assert new == {"total": 107, "through": "2026-10-12"}  # today's partial 99 is never counted
    assert warnings == []


def test_a_rerun_the_same_day_adds_nothing():
    counter = {"total": 107, "through": "2026-10-12"}
    days = [("2026-10-11", 3), ("2026-10-12", 4), ("2026-10-13", 120)]
    assert g.advance_counter(counter, days, "2026-10-13", 180, "PyPI")[0] == counter


def test_a_gap_longer_than_the_window_is_reported_not_hidden():
    counter = {"total": 10, "through": "2026-09-01"}
    days = [("2026-09-20", 2), ("2026-09-21", 1)]
    new, warnings = g.advance_counter(counter, days, "2026-09-22", 14, "GitHub clones")
    assert new == {"total": 13, "through": "2026-09-21"}
    assert len(warnings) == 1 and "18 day(s)" in warnings[0] and "every 14 days" in warnings[0]


def test_a_counter_cannot_be_advanced_without_a_seed():
    with pytest.raises(ValueError, match="seed it with --history-dir"):
        g.advance_counter(None, [("2026-10-12", 1)], "2026-10-13", 180, "PyPI")


def test_a_seed_comes_from_the_private_history_complete_days_only(tmp_path):
    h = tmp_path / "pypi-history.json"
    h.write_text(json.dumps({"2026-10-11": 3, "2026-10-12": 4, "2026-10-13": 50}), encoding="utf-8")
    assert g.seed_counter(h, today="2026-10-13") == {"total": 7, "through": "2026-10-12"}
    assert g.seed_counter(tmp_path / "missing.json", "2026-10-13") is None


def test_the_monthly_figure_is_the_30_complete_days_before_today():
    days = [("2026-09-12", 1000), ("2026-09-13", 1), ("2026-10-12", 2), ("2026-10-13", 500)]
    # 2026-09-13 .. 2026-10-12 inclusive: today's partial 500 and the 31st day back are out
    assert g.last_30_days(days, today="2026-10-13") == 3
