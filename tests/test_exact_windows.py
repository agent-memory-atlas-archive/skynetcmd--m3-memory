"""Token-exact windows from the embedder replace the estimator's when available.

The estimator over-counts prose ~3x, so m3 cut ~1.7x more windows than needed
(192 vs 112 on 110 long memories, measured 2026-10-15), each one an embed call.
The Rust core (>= the token-window release) counts exactly, in process or over
the embed server's /tokenize/windows. Without it the estimator keeps windows
correct.
"""

from __future__ import annotations

import asyncio
import importlib

import httpx
import pytest


def _embed():
    return importlib.import_module("memory.embed")


def test_the_chunker_uses_windows_that_cover_the_text():
    from memory.chunking import _chunk_for_sliding_window

    text = "abcdefghij"
    assert _chunk_for_sliding_window(text, [(0, 6), (4, 10)]) == [("abcdef", 0), ("efghij", 1)]


@pytest.mark.parametrize(
    "bad",
    [[(0, 6)], [(1, 10)], [(0, 6), (7, 10)], [(0, 6), (0, 10)], [(0, 0), (0, 10)], [("x", 3)], []],
    ids=["short", "late-start", "gap", "no-advance", "empty-window", "junk", "none"],
)
def test_windows_that_do_not_cover_are_not_used(bad):
    from memory.chunking import _chunk_for_sliding_window

    text = "abcdefghij"
    assert _chunk_for_sliding_window(text, bad) == _chunk_for_sliding_window(text)


class _InProc:
    def __init__(self):
        self.calls = []

    def token_windows(self, text, budget, overlap):
        self.calls.append((budget, overlap))
        return [(0, len(text))]


def test_a_loaded_in_process_embedder_is_asked(monkeypatch):
    e = _embed()
    emb = _InProc()
    monkeypatch.setattr(e, "_embedded_embedder", emb)
    assert asyncio.run(e._exact_windows("some text")) == [(0, 9)]
    assert emb.calls, "the in-process embedder was not asked"


def _serve(monkeypatch, handler):
    e = _embed()
    seen = []

    def wrapped(request):
        seen.append(str(request.url))
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    monkeypatch.setattr(e, "_embedded_embedder", None)
    monkeypatch.setattr(e, "_TOKENIZE_ROUTE_ABSENT", False)
    monkeypatch.setattr(e, "_get_embed_client", lambda: client)
    return e, seen


def test_the_shared_server_is_asked(monkeypatch):
    e, seen = _serve(monkeypatch, lambda r: httpx.Response(200, json={"windows": [[0, 4], [2, 7]]}))
    assert asyncio.run(e._exact_windows("1234567")) == [(0, 4), (2, 7)]
    assert seen and seen[0].endswith("/tokenize/windows")


def test_an_older_server_is_asked_once(monkeypatch):
    e, seen = _serve(monkeypatch, lambda r: httpx.Response(404, text="Not Found"))
    assert asyncio.run(e._exact_windows("1234567")) is None
    assert asyncio.run(e._exact_windows("1234567")) is None
    assert len(seen) == 1, "a server without the route must not be asked on every write"


def test_windows_that_do_not_cover_are_reported_and_dropped(monkeypatch, caplog):
    e, _seen = _serve(monkeypatch, lambda r: httpx.Response(200, json={"windows": [[0, 3]]}))
    assert asyncio.run(e._exact_windows("1234567")) is None
    assert "do not cover" in caplog.text


def test_a_server_error_falls_back_to_the_estimate(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused")

    e, _seen = _serve(monkeypatch, boom)
    assert asyncio.run(e._exact_windows("1234567")) is None
    assert e._TOKENIZE_ROUTE_ABSENT is False, "a down server is not an older server"


def test_a_short_memory_makes_no_request(monkeypatch):
    w = importlib.import_module("memory.write")
    asked = []

    async def fake(text):
        asked.append(text)
        return

    async def fake_embed(text):
        return ([0.0] * 4, "m")

    monkeypatch.setattr(w, "_exact_windows", fake)
    monkeypatch.setattr(w, "_embed", fake_embed)
    monkeypatch.setattr(w, "_get_embedded_embedder", lambda: None)
    asyncio.run(w._embed_windows("id1", "a short memory"))
    assert not asked
