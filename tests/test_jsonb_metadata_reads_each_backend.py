"""Reads of a JSON column survive PostgreSQL's JSONB on EVERY backend.

SQLite returns metadata_json as TEXT; PostgreSQL JSONB arrives as a dict, so a
bare json.loads() raised TypeError. Found by running bin/test_memory_bridge.py on
PostgreSQL (2026-10-10), then by hunting the same call shape:
  - agent_get returned "Error: TypeError ..." outright;
  - memory_inbox swallowed it and showed every sender as from=?;
  - chatlog_strip_framing_backfill --apply crashed on a row with metadata;
  - m3_entities_gliner's conversation allowlist crashed on metadata-only ids.
Each now reads through Dialect.json_column_to_dict.
"""

from __future__ import annotations

import uuid

import pytest

from conftest import skip_reason


def test_agent_get_shows_role_and_metadata(each_backend):
    import memory_bridge as mb

    name = f"agent-jb-{uuid.uuid4().hex[:8]}"
    assert mb.agent_register(name, role="tester", capabilities=["a"], metadata={"k": "v"}).startswith("Registered:")
    out = mb.agent_get(name)
    assert "Role: tester" in out and "'k': 'v'" in out, f"observed: {out[:300]!r} on {each_backend.name}"


def test_inbox_names_the_sender(each_backend):
    import memory_bridge as mb

    tag = uuid.uuid4().hex[:8]
    sender, recipient = f"jb-from-{tag}", f"jb-to-{tag}"
    for a in (sender, recipient):
        mb.agent_register(a, role="r")
    assert "Error" not in mb.memory_handoff(sender, recipient, "probe task")
    inbox = mb.memory_inbox(recipient)
    assert f"from={sender}" in inbox, f"observed: {inbox!r} on {each_backend.name}"


def test_strip_framing_apply_keeps_existing_metadata(each_backend, monkeypatch):
    import json

    if each_backend.stores_databases_as_files:
        pytest.skip(
            skip_reason("db", "the TEXT path is tests/test_chatlog_strip_framing_backfill.py; the defect is JSONB-only")
        )
    import chatlog_strip_framing_backfill as bf
    from memory.backends import dialect
    from memory.backends.dialect import chatlog_table

    items, p = chatlog_table("items"), dialect().param()
    tag = uuid.uuid4().hex[:8]
    rid, conv = f"jb-sf-{tag}", f"conv-jb-{tag}"
    body = "real ask\n<system-reminder>the date has changed.</system-reminder>\nreal reply"
    from m3_sdk import M3Context

    chat = M3Context.for_db(None).get_chatlog_conn  # the store the tool scans (its own file on SQLite)
    with chat() as conn:
        conn.execute(
            f"INSERT INTO {items} (id, type, content, metadata_json, conversation_id, created_at) "
            f"VALUES ({p}, 'chat_log', {p}, {p}, {p}, {p})",
            (rid, body, json.dumps({"k": 1}), conv, "2026-07-01T00:00:00Z"),
        )
        conn.commit()
    try:
        result = bf.backfill(conversation_id=conv, apply=True)
        with chat() as conn:
            content, meta = conn.execute(
                f"SELECT content, metadata_json FROM {items} WHERE id = {p}", (rid,)
            ).fetchone()
    finally:
        with chat() as conn:
            conn.execute(f"DELETE FROM {items} WHERE id = {p}", (rid,))
            conn.commit()
    meta = dialect().json_column_to_dict(meta)
    assert result["changed"] == 1 and "system-reminder" not in content, f"observed: {result!r}"
    assert meta.get("k") == 1 and meta.get("harness_framing_stripped") is True, f"observed: {meta!r}"


def test_gliner_allowlist_reads_conversation_from_metadata(each_backend):
    import asyncio
    import json

    import m3_entities_gliner as g
    from m3_sdk import resolve_db_path
    from memory.write import memory_write_impl

    tag = uuid.uuid4().hex[:8]
    variant, conv = f"jb-gl-{tag}", f"conv-jb-gl-{tag}"
    asyncio.run(
        memory_write_impl(
            "note",
            f"meta-only conversation {tag}",
            title="t",
            embed=False,
            variant=variant,
            metadata=json.dumps({"conversation_id": conv}),
        )
    )
    db = resolve_db_path(None) if each_backend.stores_databases_as_files else None
    rows = g._select_rows(db, variant, None, False, {conv})
    assert len(rows) == 1, f"observed: {rows!r} on {each_backend.name}"
