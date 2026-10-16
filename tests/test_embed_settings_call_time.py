"""The embed path reads its settings when it uses them, not at import.

memory.embed bound EMBED_BULK_CHUNK, ENTITY_NAME_EMBED_CACHE_MAX, TOKEN_CEILING
and the chunk sizes at import, and embed_sweep_lib bound TOKEN_BUDGET, so a
changed setting (a running daemon, a test fixture) was ignored. memory_core's
shim cached the first value it served on top of that.
"""

from __future__ import annotations

import asyncio
import importlib


def test_embed_serves_its_settings_unbound_and_the_shim_does_not_cache(m3_settings):
    emb = importlib.import_module("memory.embed")
    mc = importlib.import_module("memory_core")
    bound = sorted(n for n in emb.SETTING_NAMES if n in vars(emb))
    assert not bound, f"bound at import, so not read at call time: {bound}"

    m3_settings({"embed.bulk_chunk": 17, "embed.chunk_max_chars": 5000, "embed.chunk_overlap_chars": 1000})
    assert (emb.EMBED_BULK_CHUNK, mc.EMBED_BULK_CHUNK) == (17, 17)
    assert (emb.MAX_CHARS_PER_CHUNK, mc.MAX_CHARS_PER_CHUNK, mc.STRIDE_CHARS) == (5000, 5000, 4000)
    m3_settings({"embed.bulk_chunk": 33})
    assert mc.EMBED_BULK_CHUNK == 33, "memory_core cached the first value"


def test_the_length_contract_reads_the_ceiling_at_call_time(m3_settings):
    emb = importlib.import_module("memory.embed")
    m3_settings({"embed.ctx": 4096})
    assert emb._oversize_report(["a", "b"], [4000, 5000]) == [(1, 5000)]
    m3_settings({"embed.ctx": 8192})
    assert emb._oversize_report(["a", "b"], [4000, 5000]) == []


def test_the_entity_name_cache_bound_is_read_at_call_time(m3_settings, monkeypatch):
    emb = importlib.import_module("memory.embed")

    async def fake_embed(_text):
        return [1.0], "m"

    monkeypatch.setattr(emb, "_embed", fake_embed)
    monkeypatch.setattr(emb, "_ENTITY_NAME_EMBED_CACHE", {})
    m3_settings({"embed.entity_name_cache_max": 2})
    for name in ("a", "b", "c"):
        asyncio.run(emb._embed_canonical_cached(name))
    assert emb._ENTITY_NAME_EMBED_CACHE == {"c": [1.0]}, "the cache cleared at the configured bound"


def test_the_sweep_reads_the_token_budget_at_call_time(m3_settings):
    lib = importlib.import_module("embed_sweep_lib")
    m3_settings({"embed.token_budget": 1234})
    assert lib._token_budget_limit() == 1234
    m3_settings({"embed.token_budget": 4321})
    assert lib._token_budget_limit() == 4321
