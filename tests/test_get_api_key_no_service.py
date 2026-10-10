"""An SLM profile's api_key_service is optional, and callers (m3_enrich,
run_observer, run_reflector, m3_entities, m3_enrich_batch) pass it straight to
get_api_key. With no service it raised TypeError from unicodedata.normalize, so
a profile without a key crashed the run. No service now means no key."""

from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize("service", [None, ""])
def test_no_service_means_no_key(monkeypatch, service):
    auth = importlib.import_module("auth_utils")
    store = importlib.import_module("m3_core.secret_store")
    monkeypatch.setattr(
        store, "explain_named_secret", lambda name: pytest.fail("the secret chain must not be consulted")
    )
    assert auth.get_api_key(service) is None
