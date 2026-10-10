"""The LangGraph checkpointer (M3Saver) round-trips on EVERY backend.

It refused on PostgreSQL (require_sqlite_backend): BLOB columns, executescript
and INSERT OR REPLACE. The schema, the upserts and the binds now come from the
dialect; this pins put / get_tuple / list / put_writes / delete_thread on each.
"""

from __future__ import annotations

import uuid

import pytest

from conftest import skip_reason

pytest.importorskip("langgraph.checkpoint", reason=skip_reason("dep", "requires `pip install langgraph`"))


def _cfg(thread: str, ckpt_id: str | None = None, user: str = "") -> dict:
    conf = {"thread_id": thread, "checkpoint_ns": ""}
    if ckpt_id:
        conf["checkpoint_id"] = ckpt_id
    if user:
        conf["user_id"] = user
    return {"configurable": conf}


def test_checkpoints_round_trip(each_backend):
    from langgraph.checkpoint.base import empty_checkpoint

    from m3_memory.integrations.langchain.checkpoint import M3Saver

    saver = M3Saver()
    thread = f"t-{uuid.uuid4().hex[:10]}"  # the PG test store persists across runs
    where = f"on {each_backend.name}. inspect: m3_memory/integrations/langchain/checkpoint.py"

    first = empty_checkpoint()
    first["channel_values"] = {"blob": b"\x00\xffraw", "n": 1}
    saved = saver.put(_cfg(thread, user="u1"), first, {"step": 1}, {})
    assert saved["configurable"]["checkpoint_id"] == first["id"]

    # Re-putting the same id is an upsert, not a duplicate-key error.
    first["channel_values"]["n"] = 2
    saver.put(_cfg(thread, user="u1"), first, {"step": 1}, {})

    second = empty_checkpoint()
    saver.put(_cfg(thread, first["id"], user="u1"), second, {"step": 2}, {})

    saver.put_writes(_cfg(thread, second["id"]), [("ch", {"v": 1}), ("ch2", b"\x01")], "task-a")
    saver.put_writes(_cfg(thread, second["id"]), [("ch", {"v": 9})], "task-a")  # upsert on idx 0

    got = saver.get_tuple(_cfg(thread, first["id"]))
    assert got is not None, f"observed: no checkpoint {where}"
    assert got.checkpoint["channel_values"] == {"blob": b"\x00\xffraw", "n": 2}, (
        f"observed: {got.checkpoint['channel_values']!r} {where}"
    )
    assert got.metadata == {"step": 1}, f"observed: metadata {got.metadata!r} {where}"

    latest = saver.get_tuple(_cfg(thread))
    assert latest.checkpoint["id"] == second["id"], f"observed: latest {latest.checkpoint['id']} {where}"
    assert latest.parent_config["configurable"]["checkpoint_id"] == first["id"]
    writes = sorted((t, c, v if not isinstance(v, dict) else v["v"]) for t, c, v in latest.pending_writes)
    assert writes == [("task-a", "ch", 9), ("task-a", "ch2", b"\x01")], f"observed: {writes!r} {where}"

    listed = [t.checkpoint["id"] for t in saver.list(_cfg(thread))]
    assert listed == [second["id"], first["id"]], f"observed: list {listed!r} {where}"
    assert [t.checkpoint["id"] for t in saver.list(_cfg(thread), limit=1)] == [second["id"]]
    assert list(saver.list(_cfg(thread, user="someone-else"))) == [], f"observed: user scope leaked {where}"
    before = [t.checkpoint["id"] for t in saver.list(_cfg(thread), before=_cfg(thread, second["id"]))]
    assert before == [first["id"]], f"observed: before= {before!r} {where}"

    saver.delete_thread(thread)
    assert saver.get_tuple(_cfg(thread)) is None, f"observed: thread survived delete {where}"
