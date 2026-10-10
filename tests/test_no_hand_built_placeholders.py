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
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]

ALLOWED = {
    # The SQLite dialect renders the token: the owner.
    "bin/memory/backends/sqlite_backend.py": "INHERENT: SqliteDialect renders `?`; it is the owner of the token",
    # Payload-less SQLite fallback for seam_dialect() (installer bootstrap).
    "bin/m3_core/paths.py": "INHERENT: bootstrap fallback when the memory package is absent; SQLite is then the only backend",
    # Standalone end-to-end script that opens the SQLite file with sqlite3.connect.
    "bin/test_memory_bridge.py": "PARITY GAP: the end-to-end bridge script exercises SQLite only (m3 parity task)",
    # SQLite by contract: refuse to run on another backend.
    "bin/backfill_content_hash.py": "PARITY GAP: content_hash backfill refuses PostgreSQL (m3 parity task; verify PG need)",
    # Fallback dialect used only when the payload is absent (SQLite); see its docstring.
    "bin/embed_backfill.py": "INHERENT: payload-less bootstrap fallback dialect; SQLite only exists then",
    # The query around it is SQLite SQL (json_extract), so PostgreSQL fails loud
    # on the whole statement; porting it is a query port, not a placeholder fix.
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


# A '?' written into SQL text is the same defect as a hand-built list.
# INLINE_ALLOWED lists the files that write one legitimately, each with its reason
# (ALLOWED's files are exempt too). Docstrings do not count.
INLINE_ALLOWED = {
    "bin/migrate_entity_vocab.py": "PARITY GAP: v1->v2 vocabulary rename refuses PostgreSQL (m3 parity task; verify PG need)",
    "bin/migrate_memory.py": "INHERENT: the SQLite migrator; PostgreSQL's is migrate_pg with the same schema_versions contract",
    "bin/build_kg_variant.py": "PARITY GAP: KG-variant builder refuses PostgreSQL (m3 parity task)",
    "m3_memory/install/sections.py": "PARITY GAP: the installer shows store stats for SQLite only (m3 parity task)",
    "bin/chatlog_ingest.py": "INHERENT: reads OpenCode's own SQLite store (a host app's file, not m3's)",
    "bin/chatlog_embed_sweeper.py": "INHERENT here: on PostgreSQL the cognitive loop's embed pass sweeps both stores (embed_backfill --target chatlog), verified 2026-10-10",
    # SQLite pool users: get_sqlite_conn() is SQLite whatever the backend. Whether
    # these should reach the active backend is a separate audit (m3 task), not a
    # placeholder fix; listed so the ratchet counts only seam connections.
    "bin/memory/search_routing.py": "INHERENT: the SQLite fused FTS5 + sqlite-vec builder; PostgreSQL's sibling is search_pg",
    "bin/m3_core/context.py": "INHERENT: owns the SQLite pool (which refuses other backends)",
}
_INLINE_SQL = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|WHERE|VALUES)\b", re.IGNORECASE)
_INLINE_QMARK = re.compile(r"(=|<|>|\(|,|\bIN|\bLIKE|\bLIMIT|\bOFFSET|\bMATCH|\bGLOB)\s*\?")
# A fragment built up piecewise carries no SQL keyword (" AND type = ?",
# "enrich_run_id = ?"); the two that reached PostgreSQL that way did so unseen.
_INLINE_FRAGMENT = re.compile(
    r"\b[a-z_][a-z0-9_.]*\s*(=|<>|!=|<=|>=|<|>|\bLIKE|\bGLOB|\bMATCH|\bIN)\s*\(?\s*\?", re.IGNORECASE)


def _inline_qmarks(tree: ast.AST) -> list[int]:
    docstrings = {id(n.value) for n in ast.walk(tree)
                  if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
            and ((_INLINE_SQL.search(n.value) and _INLINE_QMARK.search(n.value))
                 or _INLINE_FRAGMENT.search(n.value))]


def test_no_inline_qmark_placeholders():
    hits = []
    for top in ("bin", "m3_memory"):
        for p in (_ROOT / top).rglob("*.py"):
            rel = p.relative_to(_ROOT).as_posix()
            if (p.name.startswith(("bench_", "benchmark_")) or "__pycache__" in p.parts
                    or rel in ALLOWED or rel in INLINE_ALLOWED):
                continue
            hits += [f"{rel}:{ln}" for ln in _inline_qmarks(ast.parse(p.read_text(encoding="utf-8")))]
    assert not hits, ("observed: SQL text with an inline '?' bind: " + ", ".join(hits)
                      + ". inspect: render it with dialect().param(), or list the file in "
                      "INLINE_ALLOWED with the reason it is SQLite by contract")


def test_inline_allowlist_has_no_stale_entries():
    stale = [rel for rel in INLINE_ALLOWED
             if not (_ROOT / rel).exists()
             or not _inline_qmarks(ast.parse((_ROOT / rel).read_text(encoding="utf-8")))]
    assert not stale, f"remove from INLINE_ALLOWED: {stale}"


def test_no_parameter_defaults_to_a_placeholder_literal():
    """A `_p="?"` default hands SQLite syntax to any caller that forgets the
    argument, and PostgreSQL then fails. The dialect renders the bind; a helper
    that needs one takes it with no default, so an omission is a TypeError."""
    hits = []
    for top in ("bin", "m3_memory"):
        for p in (_ROOT / top).rglob("*.py"):
            rel = p.relative_to(_ROOT).as_posix()
            if (p.name.startswith(("bench_", "benchmark_")) or "__pycache__" in p.parts
                    or rel.startswith("bin/memory/backends/") or rel in ALLOWED or rel in INLINE_ALLOWED):
                continue
            for fn in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                a = fn.args
                pos = list(zip([x.arg for x in a.args][len(a.args) - len(a.defaults):], a.defaults, strict=True))
                kw = [(k.arg, d) for k, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is not None]
                for name, d in pos + kw:
                    if isinstance(d, ast.Constant) and d.value in ("?", "%s"):
                        hits.append(f"{rel}:{fn.lineno} {fn.name}({name}={d.value!r})")
    assert not hits, ("observed: a parameter defaults to a bind placeholder: " + ", ".join(hits)
                      + ". inspect: take it with no default, or call dialect().param()")


# Functional parity (user rule, 2026-10-10): a SQLite-only path is allowed only
# when the limitation is VERIFIED inherent; anything else is a parity gap to port.
_PARITY_GAP_BUDGET = 5


def test_every_exemption_is_inherent_or_a_named_parity_gap():
    vague = [f"{k}: {v}" for k, v in {**ALLOWED, **INLINE_ALLOWED}.items()
             if not v.startswith(("INHERENT", "PARITY GAP"))]
    assert not vague, ("observed: an exemption that neither proves the limit inherent nor names "
                       "the parity gap: " + "; ".join(vague)
                       + ". inspect: prefix INHERENT: <verification> or PARITY GAP: <what PostgreSQL loses>")
    gaps = sum(1 for v in {**ALLOWED, **INLINE_ALLOWED}.values() if v.startswith("PARITY GAP"))
    assert gaps <= _PARITY_GAP_BUDGET, f"observed: {gaps} parity gaps (budget {_PARITY_GAP_BUDGET}); a new one needs a port, not an entry"
    assert gaps == _PARITY_GAP_BUDGET, f"observed: {gaps} parity gaps; lower _PARITY_GAP_BUDGET to lock in the port"
