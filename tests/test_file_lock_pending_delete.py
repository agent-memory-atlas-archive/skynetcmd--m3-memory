"""file_lock waits out a lock file whose deletion is still pending.

Windows refuses to create a file while the previous holder's delete is in
progress, raising PermissionError, not FileExistsError. file_lock retried only
the latter, so a concurrent writer crashed (the audit-trail concurrency test on
windows-latest). Its callers include chat-capture ingest and the secrets store.
"""

from __future__ import annotations

import errno
import os

import pytest
from m3_core import locking


def _flaky_open(monkeypatch, lock_path, denials):
    """os.open that refuses the lock file `denials` times (None: always)."""
    real_open = os.open
    seen = {"n": 0}

    def fake(path, flags, *a, **k):
        if os.fspath(path) == lock_path and (denials is None or seen["n"] < denials):
            seen["n"] += 1
            raise PermissionError(errno.EACCES, "Access is denied", path)
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(locking.os, "open", fake)
    return seen


def test_a_pending_delete_is_waited_out(tmp_path, monkeypatch):
    lock = str(tmp_path / "x.lock")
    seen = _flaky_open(monkeypatch, lock, denials=2)
    with locking.file_lock(lock, purpose="probe"):
        assert os.path.exists(lock)
    assert seen["n"] == 2
    assert not os.path.exists(lock), "the lock was not released"


def test_a_lasting_denial_is_reported_not_spun_on(tmp_path, monkeypatch):
    lock = str(tmp_path / "x.lock")
    _flaky_open(monkeypatch, lock, denials=None)
    monkeypatch.setattr(locking, "_DENIED_RETRY_S", 0.2)
    with pytest.raises(PermissionError, match="cannot create the probe lock") as e:
        with locking.file_lock(lock, purpose="probe"):
            pass
    assert lock in str(e.value) and "inspect: write access" in str(e.value)
