"""GLiNER entity extraction selects and links rows on EVERY backend.

It refused on PostgreSQL (require_sqlite_backend): row selection opened the
SQLite file with sqlite3 and hand-built the variant clause, and an import-time
default pointed M3_DATABASE at the legacy repo-relative path. Selection now
rides the backend seam and scope_predicates; the path comes from
resolve_db_path. The model is an optional extra, so it is stubbed: what this
pins is selection, the variant scope, the write, and idempotence.
"""
from __future__ import annotations

import asyncio
import uuid


def _write(content: str, **kw) -> str:
    from memory.write import memory_write_impl

    res = asyncio.run(memory_write_impl("note", content, title=content[:20], embed=False, **kw))
    return res.split(":", 1)[1].strip() if isinstance(res, str) else res["id"]


def _run(*argv: str) -> int:
    import m3_entities_gliner as g

    return asyncio.run(g._run_async(g.build_parser().parse_args(list(argv))))


def test_extracts_only_the_selected_variant_once(each_backend, monkeypatch):
    import m3_entities_gliner as g
    from m3_sdk import resolve_db_path

    if each_backend.name == "sqlite":
        # _run_async pins M3_DATABASE for the write path; let monkeypatch restore it.
        monkeypatch.setenv("M3_DATABASE", resolve_db_path(None))
    tag = uuid.uuid4().hex[:8]  # the PG test store persists across runs
    variant, other = f"gl-{tag}", f"gl-other-{tag}"
    person = f"zorblax{tag}"
    picked = _write(f"{person} met the team today", variant=variant)
    skipped = _write(f"{person} met the team again", variant=other)

    seen: list[str] = []
    monkeypatch.setattr(g, "_load_gliner", lambda cfg: object())
    monkeypatch.setattr(g, "_gliner_predict_batch", lambda model, texts, cfg: [
        seen.append(t) or [{"text": person, "label": "person", "score": 0.9, "start": 0}] for t in texts])

    assert _run("--source-variant", variant, "--device", "cpu") == 0
    where = f"on {each_backend.name}. inspect: bin/m3_entities_gliner.py _select_rows"
    assert len(seen) == 1 and seen[0].startswith(person), f"observed: model saw {seen!r} {where}"

    from memory.backends import dialect
    p = dialect().param()
    with each_backend.connection() as conn:
        linked = {r[0] for r in conn.execute(
            f"SELECT memory_id FROM memory_item_entities WHERE memory_id IN ({p}, {p})",
            (picked, skipped)).fetchall()}
    assert linked == {picked}, f"observed: linked {linked!r} {where}"

    seen.clear()
    assert _run("--source-variant", variant, "--device", "cpu") == 0
    assert seen == [], f"observed: an already-linked row was re-extracted: {seen!r} {where}"


def test_core_db_on_a_server_backend_fails_loud(each_backend, capsys):
    if each_backend.name == "sqlite":
        assert _run("--core-db", "no-such-dir/none.db", "--dry-run") == 2
        assert "observed: no SQLite DB at" in capsys.readouterr().out
    else:
        assert _run("--core-db", "agent_memory.db", "--dry-run") == 2
        out = capsys.readouterr().out
        assert "observed: --core-db names a SQLite file" in out, f"observed: {out!r}"
