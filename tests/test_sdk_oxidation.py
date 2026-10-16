"""M3 SDK telemetry, migration lock and circuit breaker: the Python implementations.

These once probed m3_core_rs for get_native_telemetry, NativeMigrationLock and
NativeCircuitBreaker. The Rust core exports none of them, so those branches could
never run on a real install and were tested only against mocks — a guard that
cannot fail. They were removed; a native version belongs in m3-core-rs first.
"""

from __future__ import annotations

import os
import pathlib
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import m3_sdk

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"


def test_no_probe_for_apis_the_rust_core_does_not_export():
    for rel in ("m3_core/context.py", "m3_core/locking.py"):
        src = (_BIN / rel).read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        for name in ("get_native_telemetry", "NativeMigrationLock", "NativeCircuitBreaker"):
            assert f"m3_core_rs.{name}" not in code and f'"{name}"' not in code, f"{rel} probes {name} again"


def test_get_system_telemetry(tmp_path):
    ctx = m3_sdk.M3Context(db_path=str(tmp_path / "test.db"))
    telemetry = ctx.get_system_telemetry()
    assert {"cpu_total", "ram_total", "gpu_total", "thermal"} <= set(telemetry)


def test_migration_lock_takes_and_releases_the_file_lock(tmp_path, monkeypatch):
    lock_root = tmp_path / "config"
    lock_root.mkdir(exist_ok=True)  # the sandbox may have made it
    monkeypatch.setattr(m3_sdk, "get_m3_config_root", lambda: str(lock_root))
    # An m3_core_rs claiming a lock class is ignored: the file lock decides.
    monkeypatch.setitem(sys.modules, "m3_core_rs", types.SimpleNamespace(NativeMigrationLock=object))

    with m3_sdk.migration_lock():
        assert os.path.exists(lock_root / ".migration.lock")

    assert not os.path.exists(lock_root / ".migration.lock")


def test_circuit_breaker_opens_and_closes(tmp_path, monkeypatch):
    monkeypatch.setitem(m3_sdk._CIRCUITS, "test_svc", None)

    ctx = m3_sdk.M3Context(db_path=str(tmp_path / "test.db"))
    assert ctx._check_circuit("test_svc") is True

    for _ in range(3):
        ctx._record_failure("test_svc", custom_cooldown=10.0)
    assert ctx._check_circuit("test_svc") is False

    ctx._record_success("test_svc")
    assert ctx._check_circuit("test_svc") is True
