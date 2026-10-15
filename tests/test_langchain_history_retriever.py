"""Live tests for M3ChatMessageHistory + with_m3_history + M3Retriever (PR-3).

Runs against a real (tmp) m3 DB (repo conftest autouse fixture isolates paths).
Requires langchain-core/langgraph (the [langchain] extra); skipped if absent.

Codifies the PR-3 guarantees, including the two runtime traps found while
building:
  * message ORDER is deterministic chronological via the persisted turn_index
    (chatlog_search order is NOT stable across topologies — unified ASC vs
    separate created_at-DESC — and created_at is only second-granular)
  * message ROLE round-trips (it rides the row's `metadata`, not a top-level col)
  * chatlog schema SELF-HEALS in separate topology (no "no such table" spill)
  * clear() is the two-step search→bulk-delete shim
  * M3Retriever surfaces bitemporal/confidence in Document.metadata
  * host_agent 'langchain' is accepted (added to the core enum, still extensible)
"""

from __future__ import annotations

import uuid

import pytest

from conftest import skip_reason

pytest.importorskip("langchain_core", reason=skip_reason("dep", "needs the [langchain] extra"))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage  # noqa: E402

from m3_memory.langchain import (  # noqa: E402
    M3ChatMessageHistory,
    M3Retriever,
    with_m3_history,
)

# Warnings are surfaced, NOT suppressed (repo policy: never hide a warning; annotate
# its disposition at the source so a reviewer sees the reasoning). Two UserWarnings
# can appear when this file runs; both are understood and intentionally left visible:
#
#   1. ".*non-main thread.*" — pytest notes that M3Client's shared asyncio event-loop
#      thread ("m3client-langchain-loop") is still alive at test end. That thread is a
#      process-wide DAEMON by design (M3Client._ensure_loop): all m3 dispatch lands on
#      one loop-thread, reused across tests. Still correct — do NOT "fix" by killing
#      the shared loop per test (that defeats its purpose and slows every adapter call).
#
#      ⚠ CORRECTED 2026-10-02: this note used to add "torn down at interpreter exit.
#      Benign — not a leak". The SHARING is benign; "not a leak" was wrong. Nothing
#      stopped the loop, so the daemon thread sat in run_forever until CPython
#      finalized it. `M3Client._shutdown_loop` (atexit) now closes it, and its default
#      executor, while the interpreter is intact — see
#      tests/test_m3client_loop_shutdown.py.
#
#      ⚠ That cleanup is NOT a fix for the intermittent 0xC0000005 the Windows suite
#      throws at finalization. This loop looked like the cause and is not: the suite
#      still crashed with the handler in place. That issue is open and tracked in m3.
#
#   2. ".*Pydantic V1.*" — langchain_core still imports pydantic.v1 shims, which warn on
#      Python 3.14 ("Core Pydantic V1 functionality isn't compatible with 3.14+"). This
#      is an UPSTREAM langchain dependency we don't control; it's a real tracking signal
#      (pydantic v1 is removed in 3.16, so langchain must migrate before then). Left
#      visible on purpose so the ticking-dependency stays in view; resolved only by a
#      langchain release that drops pydantic-v1. Not fixable in our code.


def _settled(read, done, timeout=15.0):
    """Poll ``read()`` until ``done(value)``: chat-log writes go through an async
    queue, and a fixed sleep (0.4 s) lost to a loaded machine."""
    import time

    deadline = time.monotonic() + timeout
    while True:
        value = read()
        if done(value) or time.monotonic() > deadline:
            return value
        time.sleep(0.1)


def test_history_roundtrip_order_and_roles():
    h = M3ChatMessageHistory(f"conv-1-{uuid.uuid4()}", user_id="alex")
    h.add_messages(
        [HumanMessage(content="I love hiking"), AIMessage(content="Where?"), HumanMessage(content="The Alps")]
    )
    msgs = _settled(lambda: h.messages, lambda m: len(m) == 3)
    assert [m.content for m in msgs] == ["I love hiking", "Where?", "The Alps"]
    assert [m.type for m in msgs] == ["human", "ai", "human"]


def test_history_multi_add_preserves_order():
    """turn_index continues across separate add_messages calls (monotonic)."""
    h = M3ChatMessageHistory(f"conv-2-{uuid.uuid4()}", user_id="alex")
    h.add_messages([HumanMessage(content="one"), AIMessage(content="two")])
    h.add_messages([HumanMessage(content="three"), AIMessage(content="four")])  # before the first lands
    msgs = _settled(lambda: h.messages, lambda m: len(m) == 4)
    assert [m.content for m in msgs] == ["one", "two", "three", "four"]


def test_history_clear():
    h = M3ChatMessageHistory(f"conv-3-{uuid.uuid4()}", user_id="alex")
    h.add_messages([HumanMessage(content="ephemeral")])
    assert len(_settled(lambda: h.messages, lambda m: len(m) == 1)) == 1
    h.clear()
    assert _settled(lambda: h.messages, lambda m: m == []) == []


def test_history_requires_conversation_id():
    with pytest.raises(ValueError):
        M3ChatMessageHistory("", user_id="alex")


def test_history_conversations_are_isolated():
    a = M3ChatMessageHistory(f"conv-A-{uuid.uuid4()}", user_id="alex")
    b = M3ChatMessageHistory(f"conv-B-{uuid.uuid4()}", user_id="alex")
    try:
        a.add_messages([HumanMessage(content="alpha only")])
        assert [m.content for m in _settled(lambda: a.messages, lambda m: len(m) == 1)] == ["alpha only"]
        assert b.messages == []
    finally:
        a.clear()
        b.clear()


def test_system_message_roundtrips():
    h = M3ChatMessageHistory(f"conv-sys-{uuid.uuid4()}", user_id="alex")
    h.add_messages([SystemMessage(content="you are helpful"), HumanMessage(content="hi")])
    msgs = _settled(lambda: h.messages, lambda m: len(m) == 2)
    assert msgs[0].type == "system"
    assert msgs[1].type == "human"


def _langchain_core_floor() -> str:
    """The [langchain] extra's langchain-core floor, read from pyproject (one owner)."""
    import tomllib
    from pathlib import Path

    from packaging.requirements import Requirement

    data = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    for line in data["project"]["optional-dependencies"]["langchain"]:
        req = Requirement(line)
        if req.name == "langchain-core":
            return next(s.version for s in req.specifier if s.operator == ">=")
    raise AssertionError("pyproject's [langchain] extra names no langchain-core floor")


def test_with_m3_history_builds_runnable():
    import importlib.metadata

    from packaging.version import Version

    floor = _langchain_core_floor()
    installed = importlib.metadata.version("langchain-core")
    if Version(installed) < Version(floor):
        pytest.skip(
            skip_reason(
                "dep",
                f"langchain-core {installed} is below the [langchain] extra's floor {floor}; "
                "the deprecation this test asserts exists from 1.3.3",
            )
        )
    # LangChain promoted RunnableWithMessageHistory from pending to deprecated in
    # langchain-core 1.3.3, with removal in 2.0.0: the trigger this test's earlier
    # note set for deprecating with_m3_history (users move to M3Saver / M3Store).
    # The wrapper still works below 2.0, so assert the warning instead of hiding it.
    from langchain_core._api.deprecation import LangChainDeprecationWarning
    from langchain_core.runnables import RunnableLambda

    with pytest.warns(LangChainDeprecationWarning, match="RunnableWithMessageHistory"):
        chain = with_m3_history(RunnableLambda(lambda x: x), user_id="alex")
    # It's a RunnableWithMessageHistory; the factory produces M3ChatMessageHistory.
    hist = chain.get_session_history("sess-1")  # type: ignore[attr-defined]
    assert isinstance(hist, M3ChatMessageHistory)
    assert hist.conversation_id == "sess-1"


def test_retriever_returns_documents_with_temporal_metadata():
    # seed a memory the retriever can find (via the mem0 surface). Use a
    # single distinctive token as the query: multi-word queries with stopwords
    # can miss on a fresh DB before the async embedding backfills (FTS
    # tokenization), which is a search-recall property, not a retriever bug.
    from m3_memory.langchain import Memory

    Memory(user_id="alex").add("Zanzibar is an island archipelago", user_id="alex")
    r = M3Retriever(user_id="alex", k=5)
    docs = _settled(lambda: r.invoke("Zanzibar"), lambda d: len(d) >= 1)
    assert len(docs) >= 1
    md = docs[0].metadata
    assert "id" in md and "score" in md
    assert "confidence" in md  # bitemporal/confidence surfaced (§2.3)


def test_retriever_requires_user_id():
    r = M3Retriever(k=5)
    with pytest.raises(ValueError):
        r.invoke("anything")


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))


def test_a_second_batch_continues_the_turn_index_before_the_first_is_stored():
    """chatlog_write is queued: the second add_messages counted stored rows that
    did not yet include the first batch, reused its indices, and the turns read
    back out of order (seen under load on Windows: one, three, two, four)."""
    h = M3ChatMessageHistory(f"conv-q-{uuid.uuid4()}", user_id="alex")
    sent = []

    class _QueuedClient:
        def _tool(self, name, **kw):
            sent.append(kw["turn_index"])

    h._client = _QueuedClient()
    h._fetch_rows = list  # the store has not caught up: no rows yet
    h.add_messages([HumanMessage(content="one"), AIMessage(content="two")])
    h.add_messages([HumanMessage(content="three"), AIMessage(content="four")])
    assert sent == [0, 1, 2, 3]
