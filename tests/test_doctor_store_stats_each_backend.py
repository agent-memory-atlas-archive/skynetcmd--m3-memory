"""`m3 doctor` reports every store on EVERY backend, and never calls a healthy
PostgreSQL chatlog "not yet created".

On PostgreSQL the backend panel showed memory_items only (no chat or files
store), and the chatlog section resolved a SQLite FILE path, found none, and
printed "db not yet created (no captures written)" on a working install -- a
false alarm. Both now name their tables through the seam.
"""

from __future__ import annotations

import uuid


def test_backend_panel_lists_the_stores(each_backend, capsys):
    from m3_memory.install import sections

    with each_backend.connection():
        pass  # the sandbox materializes the migrated store on first touch
    sections._backend_section({})
    out = capsys.readouterr().out
    where = f"on {each_backend.name}. inspect: m3_memory/install/sections.py _backend_section"
    if each_backend.stores_databases_as_files:
        assert "stores (SQLite):" in out, f"observed: {out!r} {where}"
    else:
        assert "stores (PostgreSQL tables):" in out and "reachable:                yes" in out, (
            f"observed: {out!r} {where}"
        )
        for label, table in (("core", "memory_items"), ("chat", "chat_log_items"), ("files", "files.leaves")):
            assert f"{label:<6} {table}" in out, f"observed: no {label} store line {where}"


def test_chatlog_section_reads_the_server_tables(each_backend, capsys):
    from memory.backends import dialect
    from memory.backends.dialect import chatlog_table

    from m3_memory.install import sections

    if each_backend.stores_databases_as_files:
        assert sections._chatlog_server_stats() is None
        return
    items, p = chatlog_table("items"), dialect().param()
    rid = f"doc-{uuid.uuid4().hex[:8]}"
    with each_backend.connection() as conn:
        conn.execute(
            f"INSERT INTO {items} (id, type, content, created_at) VALUES ({p}, 'chat_log', 'x', {p})",
            (rid, "2026-10-10T00:00:00Z"),
        )
        conn.commit()
    try:
        sections._chatlog_section({})
        out = capsys.readouterr().out
    finally:
        with each_backend.connection() as conn:
            conn.execute(f"DELETE FROM {items} WHERE id = {p}", (rid,))
            conn.commit()
    assert "store:                   chat_log_items" in out, f"observed: {out!r}"
    assert "db not yet created" not in out, "observed: the false 'no captures' alarm on PostgreSQL"
    rows = int(out.split("chat rows:")[1].split()[0])
    assert rows >= 1, f"observed: {rows} live chat rows with one written"
