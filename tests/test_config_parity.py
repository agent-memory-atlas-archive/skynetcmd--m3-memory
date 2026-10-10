"""bin/config_parity.py: the config parity snapshot (plan Step 2A.2).

The real OS keyring is never touched. Keyring-behaviour tests use in-memory
fakes and stop before any capture. Every test that CAPTURES runs the CLI in a
subprocess, as the tool is actually used: capture imports m3's config modules
to read their import-time constants, and doing that inside the test process
froze those constants for later tests (auto_route's M3_ROUTE_SHADOW_MODE broke
test_memory_search_routed on every Rust lane, Phase 2A gate, 2026-10-08). The
subprocess runs with no keyring backend, so its key is the user-only file under
the sandboxed config root.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "config_parity.py"


def _cli(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
    if env:
        e.update(env)
    return subprocess.run([sys.executable, str(_SCRIPT), *args], capture_output=True, text=True, env=e)


def _capture(snap: Path, **env) -> subprocess.CompletedProcess:
    return _cli("capture", "--context", "t", "--path", str(snap), env=env)


def _check(snap: Path, *extra: str, **env) -> subprocess.CompletedProcess:
    return _cli("check", "--context", "t", "--path", str(snap), *extra, env=env)


@pytest.fixture
def keyed():
    """A parity key for the subprocess (the key-file tier, sandboxed)."""
    r = _cli("init-key")
    assert r.returncode == 0 and ".hmac_key" in r.stdout, r.stdout + r.stderr


# ── keyring behaviour: in-process fakes, no capture ──────────────────────────
class _FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, value):
        self.store[(service, user)] = value


class _NoKeyring:
    def get_password(self, *a):
        import keyring.errors

        raise keyring.errors.NoKeyringError("no backend")

    set_password = get_password


class _LockedKeyring:
    def get_password(self, *a):
        import keyring.errors

        raise keyring.errors.KeyringError("keychain locked")

    set_password = get_password


class _ReadsButWontWrite:
    """A locked macOS keychain over SSH: reads return None, writes refuse (-25308)."""

    def get_password(self, *a):
        return None

    def set_password(self, *a):
        import keyring.errors

        raise keyring.errors.PasswordSetError("Can't store password on keychain: (-25308, 'Unknown Error')")


def test_init_key_never_overwrites(monkeypatch):
    import config_parity as cp

    kr = _FakeKeyring()
    monkeypatch.setattr(cp, "_keyring", lambda: kr)
    assert cp.init_key() == "keyring"
    first = dict(kr.store)
    assert cp.init_key() is None
    assert kr.store == first


def test_no_keyring_backend_uses_a_user_only_file(monkeypatch):
    import stat

    import config_parity as cp

    monkeypatch.setattr(cp, "_keyring", lambda: _NoKeyring())
    where = cp.init_key()
    assert where is not None and where.endswith(".hmac_key")
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(where).st_mode) == 0o600
    assert cp.init_key() is None  # never overwrites


def test_locked_keyring_is_an_error_not_a_fallback(monkeypatch):
    import config_parity as cp

    monkeypatch.setattr(cp, "_keyring", lambda: _LockedKeyring())
    assert cp.main(["init-key"]) == 2
    assert not cp._key_file().exists(), "a locked keyring must not fall back to a file"


def test_keyring_that_refuses_the_write_is_exit_2_not_a_crash(monkeypatch):
    import config_parity as cp

    monkeypatch.setattr(cp, "_keyring", lambda: _ReadsButWontWrite())
    assert cp.main(["init-key"]) == 2
    assert not cp._key_file().exists()


# ── capture and check: the CLI, in a subprocess ──────────────────────────────
def test_missing_key_cannot_capture(tmp_path):
    r = _capture(tmp_path / "s.json")
    assert r.returncode == 2 and "init-key" in r.stderr, r.stderr
    assert not (tmp_path / "s.json").exists()


def test_missing_baseline_is_not_a_pass(keyed, tmp_path):
    assert _check(tmp_path / "absent.json").returncode == 2
    assert not (tmp_path / "absent.json").exists(), "check must never seed a baseline"


@pytest.mark.requires_posix  # POSIX mode bits
def test_group_readable_key_file_is_refused(keyed, tmp_path):
    import config_parity as cp

    os.chmod(cp._key_file(), 0o644)
    r = _capture(tmp_path / "s.json")
    assert r.returncode == 2 and "chmod 600" in r.stderr, r.stderr


def test_capture_then_check_is_clean_and_drift_is_named(keyed, tmp_path):
    snap = tmp_path / "s.json"
    assert _capture(snap, M3_TOOL_TIMEOUT="30").returncode == 0
    assert _check(snap, M3_TOOL_TIMEOUT="30").returncode == 0
    r = _check(snap, M3_TOOL_TIMEOUT="45")
    assert r.returncode == 1 and "env:M3_TOOL_TIMEOUT" in r.stdout, r.stdout
    allowed = tmp_path / "intended.json"
    allowed.write_text(json.dumps(["env:M3_TOOL_TIMEOUT"]), encoding="utf-8")
    assert _check(snap, "--intended", str(allowed), M3_TOOL_TIMEOUT="45").returncode == 0


def test_secrets_are_hmac_only(keyed, tmp_path):
    snap = tmp_path / "s.json"
    secret = "postgresql://m3:hunter2-not-real@db.example/m3"
    assert _capture(snap, M3_PRIMARY_PG_URL=secret).returncode == 0
    text = snap.read_text(encoding="utf-8")
    assert "hunter2-not-real" not in text and "db.example" not in text
    entry = json.loads(text)["env"]["M3_PRIMARY_PG_URL"]["post"]
    assert entry["secret"] is True and len(entry["hmac"]) == 64
    # a changed secret is caught without being revealed
    assert _check(snap, M3_PRIMARY_PG_URL="postgresql://m3:other@db.example/m3").returncode == 1


def test_baseline_from_another_key_cannot_be_compared(keyed, tmp_path):
    import config_parity as cp

    snap = tmp_path / "s.json"
    assert _capture(snap).returncode == 0
    cp._key_file().unlink()  # a different machine, or a recreated key
    assert _cli("init-key").returncode == 0
    assert _check(snap).returncode == 2


def test_snapshot_covers_the_layers(keyed, tmp_path):
    snap = tmp_path / "s.json"
    assert _capture(snap).returncode == 0
    d = json.loads(snap.read_text(encoding="utf-8"))
    assert d["coverage"]["env_names"] > 100
    assert d["coverage"]["parsed_import_errors"] == {}, d["coverage"]["parsed_import_errors"]
    assert any(k.startswith("memory.config.") for k in d["parsed"])
    assert d["store"]["main"].startswith(("sqlite:", "postgres:"))
