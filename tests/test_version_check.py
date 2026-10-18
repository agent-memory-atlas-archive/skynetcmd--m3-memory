"""The shared version check: one record, read by every agent's session-start hook.

Each status the line can report is planted (checking off, never checked, a newer
release, up to date, a failed check) and the network is never reached: PyPI is a
stub and the record lives in a temp file."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from m3_core import version_check as vc


@pytest.fixture
def record(tmp_path, monkeypatch):
    path = tmp_path / "version_check.json"
    monkeypatch.setattr(vc, "state_path", lambda: path)
    monkeypatch.setattr(vc, "installed_version", lambda: "2026.10.18.0")
    monkeypatch.setattr(vc, "enabled", lambda: True)
    spawned = []
    monkeypatch.setattr(vc, "refresh_in_background", lambda: spawned.append(1) or True)
    return path, spawned


def _write(path, **state):
    path.write_text(json.dumps(state), encoding="utf-8")


NOW = datetime(2026, 10, 18, 18, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "candidate, installed, newer",
    [
        ("2026.10.19.0", "2026.10.18.0", True),
        ("2026.10.18.0", "2026.10.18.0", False),
        ("2026.9.30.0", "2026.10.18.0", False),
        ("2026.10.18.10", "2026.10.18.9", True),  # numeric, not a string sort
        (None, "2026.10.18.0", False),
    ],
)
def test_versions_compare_numerically(candidate, installed, newer):
    assert vc.is_newer(candidate, installed) is newer


def test_checking_off_says_so(record, monkeypatch):
    monkeypatch.setattr(vc, "enabled", lambda: False)
    line = vc.status_line(now=NOW)
    assert "version checking is off" in line and "updates.check true" in line
    assert record[1] == [], "nothing is fetched when checking is off"


def test_never_checked_starts_one_background_check(record):
    line = vc.status_line(now=NOW)
    assert "checking for updates" in line
    assert record[1] == [1]


def test_a_newer_release_names_the_upgrade(record):
    _write(record[0], checked_at="2026-10-18T12:00:00Z", installed="2026.10.18.0", latest="2026.10.19.0", error=None)
    line = vc.status_line(now=NOW)
    assert "2026.10.19.0 is available" in line and "m3 upgrade" in line
    assert record[1] == [], "a fresh record is not re-fetched"


def test_up_to_date_shows_when_it_was_checked(record):
    _write(record[0], checked_at="2026-10-18T15:00:00Z", installed="2026.10.18.0", latest="2026.10.18.0", error=None)
    assert "up to date (checked 3 h ago)" in vc.status_line(now=NOW)


def test_a_day_old_record_is_refreshed_in_the_background(record):
    old = (NOW - timedelta(hours=25)).isoformat()
    _write(record[0], checked_at=old, installed="2026.10.18.0", latest="2026.10.18.0", error=None)
    assert "up to date" in vc.status_line(now=NOW), "the last known result is still shown"
    assert record[1] == [1]


def test_a_failed_check_is_reported_with_its_cause(record):
    _write(
        record[0], checked_at="2026-10-18T17:30:00Z", installed="2026.10.18.0", latest=None, error="URLError: offline"
    )
    assert "last update check failed" in vc.status_line(now=NOW) and "offline" in vc.status_line(now=NOW)


def test_refresh_records_an_error_instead_of_raising(record, monkeypatch):
    def offline(timeout):
        raise OSError("no network")

    monkeypatch.setattr(vc, "fetch_latest", offline)
    state = vc.refresh()
    assert state["latest"] is None and "no network" in state["error"]
    assert json.loads(record[0].read_text(encoding="utf-8"))["error"] == state["error"]
