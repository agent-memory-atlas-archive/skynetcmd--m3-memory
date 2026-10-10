"""memory_search honours include_scratchpad (default: off), through the seam.

The schema always offered it and the search never read it, so ephemeral
scratchpad rows always came back. The exclusion is one Dialect primitive
(scope_predicates' exclude_types) and every candidate-fetch path passes the
same row filter, so it is tested once at the seam, once per backend end to end,
and the call sites are held to the shared filter statically.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import re
import sqlite3
import uuid
from pathlib import Path

import pytest

_SEARCH = Path(__file__).resolve().parents[1] / "bin" / "memory" / "search.py"


# ── the seam primitive ─────────────────────────────────────────────────────────
def test_exclude_types_drops_those_types_and_keeps_null():
    dl = importlib.import_module("memory.backends.dialect")
    sql, params = dl.dialect_for("sqlite").scope_predicates(exclude_types=("scratchpad",))
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE memory_items (id TEXT, type TEXT)")
    db.executemany("INSERT INTO memory_items VALUES (?, ?)",
                   [("a", "note"), ("b", "scratchpad"), ("c", None)])
    rows = db.execute(f"SELECT id FROM memory_items mi WHERE 1=1{sql} ORDER BY id", params).fetchall()
    assert [r[0] for r in rows] == ["a", "c"]


def test_exclude_types_renders_the_backends_placeholder():
    dl = importlib.import_module("memory.backends.dialect")
    sql, params = dl.dialect_for("postgres").scope_predicates(exclude_types=("scratchpad", "log"))
    assert sql == " AND (mi.type IS NULL OR mi.type NOT IN (%s, %s))" and params == ["scratchpad", "log"]
    assert dl.dialect_for("sqlite").scope_predicates() == ("", [])


# ── every candidate-fetch path passes the one row filter ──────────────────────
def test_search_call_sites_pass_the_shared_row_filter():
    tree = ast.parse(_SEARCH.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "scope_predicates"]
    assert len(calls) >= 4, "the search paths moved; re-check this gate"
    for c in calls:
        assert not c.args and len(c.keywords) == 1 and c.keywords[0].arg is None, (
            f"search.py:{c.lineno} spells out scope_predicates arguments; pass the shared row "
            "filter (**_row_filter) so a new field cannot be missed on one path")


# ── end to end, on each backend ───────────────────────────────────────────────
@pytest.fixture
def note_and_scratchpad():
    mc = importlib.import_module("memory_core")
    word = f"zq{uuid.uuid4().hex[:12]}"
    ids = []
    for typ in ("note", "scratchpad"):
        out = asyncio.run(mc.memory_write_impl(type=typ, content=f"{typ} about {word}",
                                               title=f"{typ} {word}", embed=False))
        ids.append(re.search(r"[0-9a-f-]{36}", str(out)).group(0))
    yield word, ids[0], ids[1]
    for mid in ids:
        mc.memory_delete_impl(mid, hard=True)


def _ids(text: str) -> set[str]:
    return set(re.findall(r"\[([0-9a-f-]{36})\]", text))


def test_search_hides_scratchpad_unless_asked(each_backend, note_and_scratchpad):
    mc = importlib.import_module("memory_core")
    word, note, scratch = note_and_scratchpad
    run = lambda **kw: _ids(asyncio.run(mc.memory_search_impl(word, k=5, search_mode="keyword", **kw)))  # noqa: E731
    assert run() == {note}, f"default search on {each_backend.name}"
    assert run(include_scratchpad=True) == {note, scratch}
    assert run(type_filter="scratchpad") == {scratch}, "an explicit type_filter wins"
