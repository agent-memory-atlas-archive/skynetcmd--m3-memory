"""m3_core.atomic_io.atomic_write: the one owner of write-temp-then-replace.

Thirteen modules hand-rolled it, most with a FIXED temp name (`<path>.tmp`), so
two concurrent writers clobbered each other's temp; none retried the Windows
sharing violation that os.replace raises while a reader holds the target; none
fsynced. The owner (CONFIG_DESIGN §12) uses a unique temp per write
(NamedTemporaryFile(delete_on_close=False), 3.12), writes BYTES (so the file is
identical on every OS -- text mode would emit CRLF on Windows), fsyncs, keeps an
existing file's mode, creates a new file user-only, and removes the temp when
the replace never happens.
"""
from __future__ import annotations

import os
import stat
import sys

import pytest

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")


def _leftovers(d):
    return [n for n in os.listdir(d) if n != "target.json"]


def test_writes_exact_bytes_and_leaves_no_temp(tmp_path):
    from m3_core.atomic_io import atomic_write

    target = tmp_path / "target.json"
    atomic_write(target, '{"a": 1}\n')
    assert target.read_bytes() == b'{"a": 1}\n'  # LF on every OS
    atomic_write(target, b"\x00raw")
    assert target.read_bytes() == b"\x00raw"
    assert _leftovers(tmp_path) == []


def test_failed_replace_keeps_old_content_and_cleans_temp(tmp_path, monkeypatch):
    from m3_core import atomic_io

    target = tmp_path / "target.json"
    target.write_bytes(b"old")

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(atomic_io.os, "replace", _boom)
    with pytest.raises(OSError, match="disk full"):
        atomic_io.atomic_write(target, "new")
    assert target.read_bytes() == b"old"
    assert _leftovers(tmp_path) == []


def test_sharing_violation_is_retried(tmp_path, monkeypatch):
    from m3_core import atomic_io

    target = tmp_path / "target.json"
    real = os.replace
    calls = {"n": 0}

    def _flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(13, "sharing violation (simulated)")
        return real(src, dst)

    monkeypatch.setattr(atomic_io.os, "replace", _flaky)
    monkeypatch.setattr(atomic_io.time, "sleep", lambda s: None)
    atomic_io.atomic_write(target, "after retries")
    assert calls["n"] == 3
    assert target.read_text() == "after retries"


def test_persistent_permission_error_surfaces(tmp_path, monkeypatch):
    from m3_core import atomic_io

    def _never(src, dst):
        raise PermissionError(13, "still locked")

    monkeypatch.setattr(atomic_io.os, "replace", _never)
    monkeypatch.setattr(atomic_io.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError, match="still locked"):
        atomic_io.atomic_write(tmp_path / "target.json", "x")
    assert _leftovers(tmp_path) == []


# os.replace / Path.replace calls that MOVE an existing file rather than publish a new write.
_MOVES = {
    ("bin/chatlog_embed_sweeper.py", "spill_path"): "moves a spill file into quarantine",
    ("bin/m3_halt.py", "p"): "rotates a log file to its .1 backup",
    ("bin/memory/backends/postgres_backend.py", "partial"): "publishes a finished backup copy",
    ("bin/sqlite_snapshot.py", "partial"): "publishes a finished snapshot copy",
    # Found when the scan learned pathlib's Path.replace (pathlib phase 1):
    ("bin/_task_runtime.py", "log_path"): "rotates a log file to its .1 backup",
    ("bin/memory/model_fetch.py", "part"): "publishes a finished, validated download",
    ("m3_memory/embedder_admin.py", "tmp"): (
        "NOT a move: a hand-rolled temp-then-replace config WRITE that should use "
        "atomic_write; listed so the guard runs, to be converted (m3_memory phase)"),
}


def test_no_hand_rolled_temp_then_replace():
    """Writers go through atomic_write; os.replace elsewhere is a move, listed above."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    found = set()
    for top in ("bin", "m3_memory"):
        for p in (root / top).rglob("*.py"):
            rel = p.relative_to(root).as_posix()
            if p.name.startswith(("bench_", "benchmark_")) or "__pycache__" in p.parts or rel == "bin/m3_core/atomic_io.py":
                continue
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "replace"):
                    continue
                if isinstance(n.func.value, ast.Name) and n.func.value.id == "os" and n.args:
                    src = n.args[0]                     # os.replace(src, dst)
                elif len(n.args) == 1 and not n.keywords:
                    # Path(src).replace(dst) / p.replace(dst): pathlib's move.
                    # str/bytes .replace always take 2+ arguments, so one
                    # argument means a filesystem replace.
                    src = n.func.value
                else:
                    continue
                while isinstance(src, ast.Call) and src.args:  # str(p) / Path(p) -> p
                    src = src.args[0]
                found.add((rel, ast.unparse(src)))
    assert found == set(_MOVES), (
        f"new os.replace (use atomic_write for writes): {sorted(found - set(_MOVES))}; "
        f"stale allowlist entries: {sorted(set(_MOVES) - found)}")


@posix_only
def test_new_file_is_user_only_even_under_a_loose_umask(tmp_path):
    from m3_core.atomic_io import atomic_write

    old = os.umask(0o002)
    try:
        atomic_write(tmp_path / "target.json", "x")
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "target.json").stat().st_mode) == 0o600


@posix_only
@pytest.mark.parametrize("mode", [0o644, 0o600, 0o640])
def test_existing_mode_is_kept(tmp_path, mode):
    from m3_core.atomic_io import atomic_write

    target = tmp_path / "target.json"
    target.write_text("old")
    target.chmod(mode)
    atomic_write(target, "new")
    assert stat.S_IMODE(target.stat().st_mode) == mode
