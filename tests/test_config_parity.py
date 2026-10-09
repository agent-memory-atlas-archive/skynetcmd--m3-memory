"""bin/config_parity.py: the config parity snapshot (plan Step 2A.2).

Every test runs against an in-memory keyring: the real OS keyring is never
touched. Capture runs in-process inside conftest's sandbox (temp roots).
"""
from __future__ import annotations

import json

import pytest


class _FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, value):
        self.store[(service, user)] = value


@pytest.fixture
def cp(monkeypatch):
    import config_parity

    kr = _FakeKeyring()
    monkeypatch.setattr(config_parity, "_keyring", lambda: kr)
    config_parity.fake_keyring = kr
    return config_parity


def _check(cp, path, *extra):
    return cp.main(["check", "--context", "t", "--path", str(path), *extra])


def test_missing_key_cannot_capture_or_check(cp, tmp_path):
    assert cp.main(["capture", "--context", "t", "--path", str(tmp_path / "s.json")]) == 2
    assert not (tmp_path / "s.json").exists()


def test_init_key_never_overwrites(cp):
    assert cp.init_key() is True
    first = dict(cp.fake_keyring.store)
    assert cp.init_key() is False
    assert cp.fake_keyring.store == first


def test_missing_baseline_is_not_a_pass(cp, tmp_path):
    cp.init_key()
    assert _check(cp, tmp_path / "absent.json") == 2
    assert not (tmp_path / "absent.json").exists(), "check must never seed a baseline"


def test_capture_then_check_is_clean_and_drift_is_named(cp, tmp_path, monkeypatch, capsys):
    cp.init_key()
    snap = tmp_path / "s.json"
    monkeypatch.setenv("M3_TOOL_TIMEOUT", "30")
    assert cp.main(["capture", "--context", "t", "--path", str(snap)]) == 0
    assert _check(cp, snap) == 0
    monkeypatch.setenv("M3_TOOL_TIMEOUT", "45")
    capsys.readouterr()
    assert _check(cp, snap) == 1
    assert "env:M3_TOOL_TIMEOUT" in capsys.readouterr().out
    allowed = tmp_path / "intended.json"
    allowed.write_text(json.dumps(["env:M3_TOOL_TIMEOUT"]), encoding="utf-8")
    assert _check(cp, snap, "--intended", str(allowed)) == 0


def test_secrets_are_hmac_only(cp, tmp_path, monkeypatch):
    cp.init_key()
    monkeypatch.setenv("M3_PRIMARY_PG_URL", "postgresql://m3:hunter2-not-real@db.example/m3")
    snap = tmp_path / "s.json"
    assert cp.main(["capture", "--context", "t", "--path", str(snap)]) == 0
    text = snap.read_text(encoding="utf-8")
    assert "hunter2-not-real" not in text and "db.example" not in text
    entry = json.loads(text)["env"]["M3_PRIMARY_PG_URL"]["post"]
    assert entry["secret"] is True and len(entry["hmac"]) == 64
    # a changed secret is caught without being revealed
    monkeypatch.setenv("M3_PRIMARY_PG_URL", "postgresql://m3:other@db.example/m3")
    assert _check(cp, snap) == 1


def test_baseline_from_another_key_cannot_be_compared(cp, tmp_path):
    cp.init_key()
    snap = tmp_path / "s.json"
    assert cp.main(["capture", "--context", "t", "--path", str(snap)]) == 0
    cp.fake_keyring.store.clear()
    cp.init_key()  # a different machine, or a recreated key
    assert _check(cp, snap) == 2


def test_snapshot_covers_the_layers(cp, tmp_path):
    cp.init_key()
    snap = tmp_path / "s.json"
    assert cp.main(["capture", "--context", "t", "--path", str(snap)]) == 0
    d = json.loads(snap.read_text(encoding="utf-8"))
    assert d["coverage"]["env_names"] > 100
    assert d["coverage"]["parsed_import_errors"] == {}, d["coverage"]["parsed_import_errors"]
    assert any(k.startswith("memory.config.") for k in d["parsed"])
    assert d["store"]["main"].startswith(("sqlite:", "postgres:"))
