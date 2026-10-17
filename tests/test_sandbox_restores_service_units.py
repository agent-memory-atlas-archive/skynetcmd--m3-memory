"""The sandbox puts back a REAL m3 service unit a test moved, rewrote or created.

Reporting alone was not enough: `doctor --fix`'s job repair moved a developer's
live units into a pytest temp dir (2026-10-17), which pytest deletes a few runs
later. Exercised on a temp directory standing in for the service dirs, never on
the real ones.
"""

from __future__ import annotations

import contextlib
import os

import conftest


@contextlib.contextmanager
def _service_dirs(*dirs):
    """Point the guard's helpers at ``dirs`` for the block only. Not monkeypatch:
    its undo runs after the guard's own teardown (the sandbox requests it first),
    and the guard would then compare the real dirs with these."""
    saved = conftest._SERVICE_DIRS
    conftest._SERVICE_DIRS = dirs
    try:
        yield
    finally:
        conftest._SERVICE_DIRS = saved


def test_moved_rewritten_and_created_units_are_put_back(tmp_path):
    units = tmp_path / "systemd-user"
    units.mkdir()
    moved, rewritten = units / "m3-cognitive-loop.service", units / "m3-dashboard.service"
    moved.write_bytes(b"[Unit]\nDescription=loop\n")
    rewritten.write_bytes(b"[Unit]\nDescription=dashboard\n")
    os.utime(moved, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    created = units / "m3-from-a-test.service"

    with _service_dirs(units):
        before, snap = conftest._service_files_state(), conftest._service_files_snapshot()
        moved.rename(tmp_path / "elsewhere.service")
        rewritten.write_bytes(b"[Unit]\nDescription=a test's\n")
        created.write_bytes(b"x")
        after = conftest._service_files_state()
        changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))

        assert conftest._restore_service_files(snap, changed) == []
        assert conftest._service_files_state() == before

    assert moved.read_bytes() == b"[Unit]\nDescription=loop\n"
    assert moved.stat().st_mtime_ns == 1_700_000_000_000_000_000, "the original timestamp, not now"
    assert rewritten.read_bytes() == b"[Unit]\nDescription=dashboard\n"
    assert not created.exists()


def test_a_non_m3_file_is_never_touched(tmp_path):
    units = tmp_path / "systemd-user"
    units.mkdir()
    other = units / "someone-else.service"
    other.write_bytes(b"theirs")
    with _service_dirs(units):
        snap = conftest._service_files_snapshot()
    assert str(other) not in snap
    other.write_bytes(b"changed by its owner")
    assert conftest._restore_service_files(snap, []) == []
    assert other.read_bytes() == b"changed by its owner"
