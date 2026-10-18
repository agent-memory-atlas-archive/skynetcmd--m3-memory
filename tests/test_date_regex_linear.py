"""The search query's long-date regex runs in linear time. With `\\s+,?\\s*` before
the year, a month followed by many spaces was quadratic: 40,000 characters took
7 s of CPU per search (CodeQL py/polynomial-redos)."""

from __future__ import annotations

import time

from memory.fts import _DATE_RE_LONG


def test_a_month_followed_by_many_spaces_is_fast():
    q = "1 jan" + " " * 200_000 + "x"
    t = time.perf_counter()
    list(_DATE_RE_LONG.finditer(q))
    assert time.perf_counter() - t < 1.0  # quadratic took minutes at this size


def test_the_dates_it_matches_are_unchanged():
    def years(q):
        return [m.groups() for m in _DATE_RE_LONG.finditer(q)]

    assert years("3rd january 2024") == [("3", "january", "2024")]
    assert years("3 jan , 2024") == [("3", "jan", "2024")]
    assert years("3rd january, 2024") == [("3", "january", None)]  # as before: comma right after the month
    assert years("12 dec") == [("12", "dec", None)]
    assert years("31 oct,2021") == [("31", "oct", None)]  # a year needs a space first, as before
