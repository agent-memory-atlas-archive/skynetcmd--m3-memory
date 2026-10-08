"""Bind placeholders come from the dialect, never from a hand-built '?' list.

`",".join("?" * n)` and `["?"] * n` render SQLite's qmark style. PostgreSQL
rejects '?' and the PG connection shim deliberately does not translate it, so
every copy is a PostgreSQL failure waiting for its first PG caller -- and on
2026-10-08 four were live: the bulk-embed cache lookup (it swallowed the error,
so the cache silently never hit), conversation_search pairing, the chatlog
backfill --type filter, and the LangChain client's chatlog delete (which also
named the SQLite table). `Dialect.placeholder(n)` / `param()` is the
one owner (DESIGN_PHILOSOPHIES §10a: call sites express intent, the dialect
renders SQL).

ALLOWED lists files that are SQLite by contract, each with its reason. Adding
a file there needs a reason a reviewer can check, not "it works on SQLite".
"""
from __future__ import annotations

import ast
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]

ALLOWED = {
    # The SQLite dialect renders the token: the owner.
    "bin/memory/backends/sqlite_backend.py": "SqliteDialect.placeholder, the owner",
    # Payload-less SQLite fallback for seam_dialect() (installer bootstrap).
    "bin/m3_core/paths.py": "seam_dialect's SQLite fallback when memory.backends is absent",
    # Standalone end-to-end script that opens the SQLite file with sqlite3.connect.
    "bin/test_memory_bridge.py": "sqlite3.connect on a SQLite file by construction",
    # SQLite by contract: refuse to run on another backend.
    "bin/backfill_content_hash.py": "calls require_sqlite_backend",
    "bin/m3_enrich_batch.py": "calls require_sqlite_backend",
    # Fallback dialect used only when the payload is absent (SQLite); see its docstring.
    "bin/embed_backfill.py": "payload-less SQLite fallback dialect",
    # The query around it is SQLite SQL (json_extract), so PostgreSQL fails loud
    # on the whole statement; porting it is a query port, not a placeholder fix.
    "bin/enrich/eligibility.py": "SQLite-only query (json_extract); port the query, then remove",
}


def _qmark_lists(tree: ast.AST) -> list[int]:
    lines = []
    for n in ast.walk(tree):
        # "?" * n  /  ["?"] * n
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mult):
            for side in (n.left, n.right):
                if isinstance(side, ast.Constant) and side.value == "?":
                    lines.append(n.lineno)
                if (isinstance(side, ast.List) and len(side.elts) == 1
                        and isinstance(side.elts[0], ast.Constant) and side.elts[0].value == "?"):
                    lines.append(n.lineno)
    return lines


def test_placeholders_come_from_the_dialect():
    hits = []
    for top in ("bin", "m3_memory"):
        for p in (_ROOT / top).rglob("*.py"):
            rel = p.relative_to(_ROOT).as_posix()
            if p.name.startswith(("bench_", "benchmark_")) or "__pycache__" in p.parts or rel in ALLOWED:
                continue
            for ln in _qmark_lists(ast.parse(p.read_text(encoding="utf-8"))):
                hits.append(f"{rel}:{ln}")
    assert not hits, ("hand-built '?' placeholder list; use dialect().placeholder(n) / param(): "
                      + ", ".join(hits))


def test_allowlist_has_no_stale_entries():
    """An entry whose file no longer builds a '?' list must be removed, or the
    allowlist silently grows a pass for a file that later regresses."""
    stale = []
    for rel in ALLOWED:
        p = _ROOT / rel
        if not p.exists() or not _qmark_lists(ast.parse(p.read_text(encoding="utf-8"))):
            stale.append(rel)
    assert not stale, f"remove from ALLOWED: {stale}"
