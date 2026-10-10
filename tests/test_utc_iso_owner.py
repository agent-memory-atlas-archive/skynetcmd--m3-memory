"""One owner for UTC ISO-8601 stamps: m3_core.runtime.utc_iso.

Two defects motivated it (plan item R7 and a sibling):
  * `datetime.strftime("%Y-%m-%dT%H:%M:%fZ")` in m3_enrich_batch and
    release_orphan_claims: in Python `%f` is six digits of MICROSECONDS, not
    SQLite's `SS.SSS`, so the stamp lost its seconds ("12:34:567890Z"), would
    not parse, and ordered randomly within a minute against the `%SZ` cutoff.
  * `iso_utc_timestamp()` used bare `isoformat()`, which DROPS the fraction when
    microsecond == 0, so the width varied and "...:05Z" sorted after
    "...:05.000001Z" ('Z' > '.') although it is earlier.
These stamps are compared as strings, so every one must be fixed-width at its
precision.
"""

from __future__ import annotations

import ast
import pathlib
import re
from datetime import UTC, datetime

import pytest

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"


def _freeze(monkeypatch, when: datetime) -> None:
    from m3_core import runtime

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return when if tz is not None else when.replace(tzinfo=None)

    monkeypatch.setattr(runtime, "datetime", _Frozen)


@pytest.mark.parametrize("micro", [0, 1, 123456])
@pytest.mark.parametrize(
    "timespec,pattern",
    [
        ("seconds", r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"),
        ("milliseconds", r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z"),
        ("microseconds", r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z"),
    ],
)
def test_fixed_width_and_parseable(monkeypatch, micro, timespec, pattern):
    from m3_core import runtime

    when = datetime(2026, 10, 8, 12, 34, 5, micro, tzinfo=UTC)
    _freeze(monkeypatch, when)
    s = runtime.utc_iso(timespec)
    assert re.fullmatch(pattern, s), s
    assert datetime.fromisoformat(s).replace(microsecond=0) == when.replace(microsecond=0)


def test_default_is_seconds_matching_the_stored_format(monkeypatch):
    from m3_core import runtime

    _freeze(monkeypatch, datetime(2026, 10, 8, 12, 34, 5, 999999, tzinfo=UTC))
    assert runtime.utc_iso() == "2026-10-08T12:34:05Z"
    assert runtime.utc_iso() == datetime(2026, 10, 8, 12, 34, 5, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_iso_utc_timestamp_is_fixed_width_at_microsecond_zero(monkeypatch):
    from m3_core import runtime

    _freeze(monkeypatch, datetime(2026, 10, 8, 12, 34, 5, 0, tzinfo=UTC))
    early = runtime.iso_utc_timestamp()
    _freeze(monkeypatch, datetime(2026, 10, 8, 12, 34, 5, 1, tzinfo=UTC))
    later = runtime.iso_utc_timestamp()
    assert early == "2026-10-08T12:34:05.000000Z"
    assert early < later


def test_no_python_strftime_uses_sqlite_style_percent_f():
    """`%f` after `%M:` is SQLite's seconds-with-millis; in Python it is micros."""
    hits = []
    for p in _BIN.rglob("*.py"):
        if p.name.startswith(("bench_", "benchmark_")) or "__pycache__" in p.parts:
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "strftime"
                and n.args
                and isinstance(n.args[0], ast.Constant)
                and isinstance(n.args[0].value, str)
                and "%M:%f" in n.args[0].value
            ):
                hits.append(f"{p.relative_to(_BIN.parent)}:{n.lineno}")
    assert not hits, f"Python strftime with SQLite-style %M:%f: {hits}"
