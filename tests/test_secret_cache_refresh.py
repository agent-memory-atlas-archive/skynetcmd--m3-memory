"""Secrets are cached per process, and a change reaches running processes
(user decision 2026-10-11: "being able to send a signal to refresh").

A hot path (the cloud embed token) must not hit the keyring per call, so values
are cached; every m3 secret write and `m3 secret refresh` move a generation marker
that cached processes check, cross-platform (a file, not SIGHUP).
"""

from __future__ import annotations

import os
import time

import pytest


@pytest.fixture
def ss(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    from m3_core import secret_store

    monkeypatch.setattr(secret_store, "_GEN_TTL_S", 0.0)  # check the marker on every call
    secret_store.clear_secret_cache()
    yield secret_store
    secret_store.clear_secret_cache()


def test_a_secret_is_resolved_once_per_process(ss):
    calls = []
    resolve = lambda n: calls.append(n) or "v1"  # noqa: E731
    assert ss.cached_secret("K", resolve) == "v1"
    assert ss.cached_secret("K", resolve) == "v1"
    assert calls == ["K"], "the resolver (the keyring) ran more than once"


def test_a_refresh_from_another_process_is_seen(ss):
    values = iter(["old", "new"])
    resolve = lambda n: next(values)  # noqa: E731
    assert ss.cached_secret("K", resolve) == "old"
    # Another process bumps: write the marker directly, with a later mtime.
    p = ss.secret_generation_path()
    p.write_text("other process\n", encoding="utf-8")
    later = time.time() + 5
    os.utime(p, (later, later))
    assert ss.cached_secret("K", resolve) == "new"


def test_storing_a_secret_bumps_the_generation(ss):
    before = ss._generation()
    ss.set_secret("LM_API_TOKEN", "x")  # a real m3 secret name, in the test's own config root
    assert ss._generation() != before, "set_secret did not signal running processes"


def test_m3_secret_refresh_moves_the_marker(ss, capsys):
    import argparse

    from m3_memory import cli

    before = ss._generation()
    assert cli._cmd_secret(argparse.Namespace(secret_cmd="refresh")) == 0
    assert ss._generation() != before
    assert "refresh" in capsys.readouterr().out
