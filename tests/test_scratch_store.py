"""Harnesses that write test rows never write them into the person's store.

Owner: m3_core.paths.scratch_store. The benchmark and end-to-end scripts in bin/
ship with m3, so a user can run them; by default their rows go to a new
temporary SQLite database, and on a server backend they refuse unless told to
write into the configured store.
"""

from __future__ import annotations

import ast
from pathlib import Path

import m3_core.paths as paths
import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"

# Shipped scripts that write test rows without scratch_store, each with its reason.
EXEMPT = {
    "bench_concurrent_writes.py": "builds its own store in a tempfile.mkdtemp folder",
    "test_mcp_proxy.py": "reads only: counts activity_logs rows the proxy server writes in its own process",
}

_WRITES = ("memory_write_impl", "memory_write_bulk_impl", "chatlog_write_impl", "chatlog_write_bulk_impl", "connect")


@pytest.fixture
def unpinned():
    yield
    paths.pin_database(None)


def test_the_default_is_a_new_temporary_database(monkeypatch, unpinned):
    monkeypatch.setattr(paths, "store_is_a_file", lambda: True)
    chosen = paths.scratch_store("Retrieval benchmark")
    assert paths.configured_main_db() == chosen
    assert "m3-retrieval-benchmark-" in chosen and chosen.endswith("agent_memory.db")


def test_a_named_database_is_pinned(monkeypatch, tmp_path, unpinned):
    monkeypatch.setattr(paths, "store_is_a_file", lambda: True)
    target = str(tmp_path / "mine.db")
    assert paths.scratch_store("x", target) == target and paths.configured_main_db() == target


def test_a_server_store_needs_the_explicit_flag(monkeypatch, unpinned):
    monkeypatch.setattr(paths, "store_is_a_file", lambda: False)
    with pytest.raises(SystemExit, match="--configured-store"):
        paths.scratch_store("bridge e2e")
    assert paths.scratch_store("bridge e2e", configured_store=True) == "the configured database"
    assert paths.configured_main_db() is None, "nothing may be pinned for a server store"


def _writers(base: Path = BIN) -> list[str]:
    """Shipped harness scripts that write rows but never call scratch_store."""
    found = []
    for p in sorted(base.glob("*.py")):
        if not p.name.startswith(("bench_", "benchmark_", "test_")) or p.name in EXEMPT:
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        names = {
            n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
        }
        if names & set(_WRITES) and "scratch_store" not in names:
            found.append(p.name)
    return found


def test_every_shipped_harness_that_writes_uses_scratch_store():
    found = _writers()
    assert not found, (
        f"observed: {found} write rows without m3_core.paths.scratch_store. inspect: call it before the "
        "first write (a test row must never land in the person's store), or add the script to EXEMPT with why"
    )


def test_the_guard_sees_a_harness_that_writes(tmp_path):
    (tmp_path / "bench_new.py").write_text("import memory_core\nmemory_core.memory_write_impl('note', 'x')\n")
    (tmp_path / "benchmark_ok.py").write_text(
        "from m3_core.paths import scratch_store\nscratch_store('x')\nmemory_write_impl('note', 'x')\n"
    )
    (tmp_path / "helper.py").write_text("memory_write_impl('note', 'x')\n")  # not a harness
    assert _writers(tmp_path) == ["bench_new.py"]


def test_every_exemption_still_builds_its_own_store():
    text = (BIN / "bench_concurrent_writes.py").read_text(encoding="utf-8")
    assert "mkdtemp" in text, "bench_concurrent_writes no longer builds its own store; remove it from EXEMPT"
    proxy = (BIN / "test_mcp_proxy.py").read_text(encoding="utf-8")
    assert "memory_write" not in proxy and "INSERT INTO" not in proxy.upper(), (
        "test_mcp_proxy now writes rows itself; it needs scratch_store, not an exemption"
    )
