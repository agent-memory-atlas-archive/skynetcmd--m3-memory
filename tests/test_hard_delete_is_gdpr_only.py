"""GDPR erasure is the only hard delete of memory content (user rule 2026-10-13).

Every production statement that physically removes memory content -- a literal
`DELETE FROM` one of the protected tables, or ANY `DELETE FROM {dynamic}` (a table
name built at runtime could be one of them) -- must be on the allow-list below with
its reason. A new site fails here: make it a soft delete (memory.deletion), or
route it through gdpr_forget. A listed site that no longer exists fails too, so
the list cannot go stale.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_PROTECTED = re.compile(
    r"DELETE FROM (memory_items|memory_archive|memory_history|m3_lg_checkpoints|m3_lg_writes|tasks)\b"
)
_DYNAMIC = re.compile(r"DELETE FROM \{")
_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")

ALLOWED = {
    ("bin/memory_maintenance.py", "gdpr_forget_impl"): "THE allowance: GDPR erasure (two steps, compliance record)",
    ("bin/chatlog_core.py", "_run"): "chatlog_promote copy=false: a MOVE, the rows are copied to the main store first",
    ("bin/split_chatlog_from_core.py", "main"): "migration tool: a MOVE of chat rows from core to the chatlog store",
    ("bin/build_kg_variant.py", "_build"): "dev tool: regenerates the variant rows it generated itself",
    ("bin/files_memory/corpora.py", "corpus_delete"): "files corpus index (derived from files on disk), not memories",
    ("bin/m3_enrich_stage.py", "ack"): "queue bookkeeping: an acknowledged queue row",
    ("bin/reembed_space.py", "_delete_doomed"): "derived embeddings being replaced",
}


def _sites(root: Path = ROOT) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for base in ("bin", "m3_memory"):
        for path in (root / base).rglob("*.py"):
            if path.name.startswith(("test_", "bench_")) or "__pycache__" in path.parts:
                continue
            func = "<module>"
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                m = _DEF.match(line)
                if m:
                    func = m.group(1)
                if _PROTECTED.search(line) or _DYNAMIC.search(line):
                    found.add((path.relative_to(root).as_posix(), func))
    return found


def test_no_hard_delete_of_memory_content_outside_the_allow_list():
    unexpected = sorted(_sites() - set(ALLOWED))
    assert not unexpected, (
        f"hard delete outside GDPR erasure: {unexpected}. Hard delete is GDPR-only: soft-delete "
        "(memory.deletion), or route through gdpr_forget; if it is a move or derived data, "
        "add it to ALLOWED with the reason."
    )


def test_the_allow_list_has_no_stale_entries():
    stale = sorted(set(ALLOWED) - _sites())
    assert not stale, f"ALLOWED names sites that no longer delete: {stale}; remove them"


def test_the_guard_can_fail(tmp_path):
    """Planted violations, literal and dynamic, are caught (the scan really reads code)."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "planted.py").write_text(
        'def sneaky():\n    db.execute("DELETE FROM memory_items WHERE id = 1")\n'
        'def sneakier(t):\n    db.execute(f"DELETE FROM {t}")\n',
        encoding="utf-8",
    )
    assert _sites(tmp_path) == {("bin/planted.py", "sneaky"), ("bin/planted.py", "sneakier")}
