"""A named entity-vocabulary file that is missing or malformed falls back to the
built-in vocabulary AND says so; the shipped default being absent stays quiet."""
from __future__ import annotations

import importlib
import logging


def test_malformed_vocab_warns(tmp_path, caplog):
    me = importlib.import_module("memory.entity")
    bad = tmp_path / "vocab.yaml"
    bad.write_text("entity_types: [unclosed\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="memory.entity"):
        types, _ = me.load_entity_vocab(bad)
    assert types == me._DEFAULT_VALID_ENTITY_TYPES
    assert "entity vocabulary unreadable" in caplog.text


def test_missing_named_vocab_warns(tmp_path, caplog):
    me = importlib.import_module("memory.entity")
    with caplog.at_level(logging.WARNING, logger="memory.entity"):
        types, _ = me.load_entity_vocab(tmp_path / "absent.yaml")
    assert types == me._DEFAULT_VALID_ENTITY_TYPES
    assert "entity vocabulary not found" in caplog.text
