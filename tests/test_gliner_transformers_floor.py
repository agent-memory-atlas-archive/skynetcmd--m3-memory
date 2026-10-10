"""entity-ner pulls a transformers below m3's floor (every gliner release caps
it); the GLiNER loader says so instead of letting it pass unnoticed. The floor
is read from the installed package metadata, so pyproject stays its one source."""
from __future__ import annotations

import importlib
from importlib import metadata

_REQS = ['transformers>=5.17.0,<6; extra == "embed-server"', "numpy>=2.5.3"]


def _patch(monkeypatch, installed: str):
    def requires(dist):
        assert dist == "m3-memory"
        return _REQS

    def version(dist):
        assert dist == "transformers"
        return installed

    monkeypatch.setattr(metadata, "requires", requires)
    monkeypatch.setattr(metadata, "version", version)


def test_below_the_floor_warns(monkeypatch, capsys):
    g = importlib.import_module("m3_entities_gliner")
    _patch(monkeypatch, "5.16.2")
    g._warn_if_below_transformers_floor()
    out = capsys.readouterr().out
    assert "transformers 5.16.2 is below m3's floor 5.17.0" in out


def test_at_the_floor_is_quiet(monkeypatch, capsys):
    g = importlib.import_module("m3_entities_gliner")
    _patch(monkeypatch, "5.19.0")
    g._warn_if_below_transformers_floor()
    assert capsys.readouterr().out == ""


def test_no_package_metadata_is_quiet(monkeypatch, capsys):
    g = importlib.import_module("m3_entities_gliner")

    def missing(dist):
        raise metadata.PackageNotFoundError(dist)

    monkeypatch.setattr(metadata, "requires", missing)
    g._warn_if_below_transformers_floor()
    assert capsys.readouterr().out == ""
