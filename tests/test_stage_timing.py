"""m3_core.monitoring.stage_timing: per-stage timing on sys.monitoring (PEP 669)."""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import time

import pytest
from m3_core.monitoring import MonitoringBusy, StageMap, c_target, stage_timing

from m3_memory._pyver import event_loop_timer_slack


def inner():
    time.sleep(0.02)


def outer():
    time.sleep(0.01)
    inner()


async def slow(n):
    await asyncio.sleep(0.03 * n)
    return n


def boom():
    raise ValueError("x")


def test_nested_stages_split_self_time():
    with stage_timing(StageMap(functions={"outer": [outer], "inner": [inner]})) as r:
        outer()
    o, i = r.stages["outer"], r.stages["inner"]
    assert (o.calls, i.calls) == (1, 1)
    assert o.wall_s >= i.wall_s >= 0.02
    assert abs(o.self_s - (o.wall_s - i.wall_s)) < 1e-6, "nested time counted twice"


def test_concurrent_invocations_of_one_coroutine_are_timed_apart():
    """Frame identity, not the code object, pairs a start with its return."""
    async def main():
        return await asyncio.gather(slow(1), slow(2))

    with stage_timing(StageMap(functions={"slow": [slow]})) as r:
        assert asyncio.run(main()) == [1, 2]
    s = r.stages["slow"]
    assert s.calls == 2
    # A timer can fire up to one clock tick early (15.6 ms on Windows before 3.13):
    # the version seam says how much. A suspension that ENDED the stage would
    # still read near zero, far below this floor.
    slack = event_loop_timer_slack()
    assert s.max_s >= 0.06 - slack and s.wall_s >= 0.09 - 2 * slack, "a suspension ended a stage early"


def test_an_exception_counts_as_an_error_and_closes_the_stage():
    with stage_timing(StageMap(functions={"boom": [boom], "outer": [outer]})) as r:
        with pytest.raises(ValueError):
            boom()
        outer()  # the stack was unwound: outer is a top-level stage again
    assert r.stages["boom"].errors == 1 and r.stages["boom"].calls == 1
    assert r.stages["outer"].errors == 0


def _db_calls():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (x)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(2000)])
    rows = conn.execute("SELECT x FROM t").fetchall()
    conn.close()
    return len(rows)


def _classify(callable_: object, arg0: object) -> str | None:
    module, name = c_target(callable_, arg0)
    return "db" if module == "sqlite3" and name in {"execute", "executemany", "fetchall"} else None


def test_c_calls_are_attributed_by_the_classifier():
    sm = StageMap(functions={"work": [_db_calls]}, c_call_sites=[_db_calls], classify_c=_classify)
    with stage_timing(sm) as r:
        assert _db_calls() == 2000
    assert r.stages["db"].calls == 4  # execute (CREATE), executemany, execute (SELECT), fetchall
    assert r.stages["work"].self_s < r.stages["work"].wall_s


def test_one_session_at_a_time_and_the_slot_is_released():
    sm = StageMap(functions={"outer": [outer]})
    with stage_timing(sm):
        with pytest.raises(MonitoringBusy, match="already active"):
            with stage_timing(sm):
                pass
    assert all(sys.monitoring.get_tool(i) is None for i in (sys.monitoring.PROFILER_ID, 3, 4))
    with stage_timing(sm) as r:  # reusable afterwards
        outer()
    assert r.stages["outer"].calls == 1


def test_stage_timing_runs_alongside_cprofile():
    """cProfile holds the PROFILER_ID slot while enabled (3.12+); timing takes
    the next free id instead of refusing, and cProfile still records."""
    import cProfile
    import pstats

    prof = cProfile.Profile()
    prof.enable()
    try:
        assert sys.monitoring.get_tool(sys.monitoring.PROFILER_ID) == "cProfile"
        with stage_timing(StageMap(functions={"outer": [outer]})) as r:
            outer()
    finally:
        prof.disable()
    assert r.stages["outer"].calls == 1
    assert any(fn[2] == "outer" for fn in pstats.Stats(prof).stats), "cProfile lost its data"


def test_every_tool_id_held_is_refused_naming_the_holders():
    ids = (sys.monitoring.PROFILER_ID, 3, 4)
    for i in ids:
        sys.monitoring.use_tool_id(i, f"holder-{i}")
    try:
        with pytest.raises(MonitoringBusy, match=r"holder-2.*holder-3.*holder-4"):
            with stage_timing(StageMap(functions={"outer": [outer]})):
                pass
    finally:
        for i in ids:
            sys.monitoring.free_tool_id(i)


def test_search_profile_times_a_real_search_on_each_backend(each_backend):
    """End to end through the real search: db time is attributed on SQLite (the
    driver's C calls) and on PostgreSQL (the compat wrapper), and total's self
    time is reported as unattributed."""
    import importlib
    import re
    import uuid

    sp = importlib.import_module("memory.search_profile")
    mc = importlib.import_module("memory_core")
    # A row to find (on an empty store the search returns before any SQL), with
    # a word no other row has, deleted afterwards: on PostgreSQL the store is a
    # shared database that outlives the test.
    word = f"zq{uuid.uuid4().hex[:12]}"
    out = asyncio.run(mc.memory_write_impl(type="note", content=f"the server listens on {word}",
                                           title=f"server {word}", embed=False))
    mid = re.search(r"[0-9a-f-]{36}", str(out)).group(0)
    try:
        out = asyncio.run(sp.search_profile_impl(word, k=5, search_mode="keyword"))
    finally:
        mc.memory_delete_impl(mid, hard=True)
    assert out["hits"] == 1, out
    stages = out["stages"]
    assert stages["total"]["calls"] == 1
    assert stages.get("db", {}).get("calls", 0) >= 1, f"no db time on {each_backend.name}: {stages}"
    assert out["unattributed_ms"] == stages["total"]["self_ms"]
    assert out["elapsed_ms"] >= stages["total"]["wall_ms"]
    assert sys.monitoring.get_tool(sys.monitoring.PROFILER_ID) is None


def test_the_stage_map_names_only_live_functions():
    """A stage pointing at a renamed function would silently time nothing."""
    import importlib

    from m3_core.monitoring import _code_of

    sm = importlib.import_module("memory.search_profile").retrieval_stage_map()
    for fns in sm.functions.values():
        for fn in fns:
            _code_of(fn)  # raises for anything that is not a live Python function
    assert {"total", "embed", "db", "rerank", "expand", "rank"} <= set(sm.functions)


def test_every_live_copy_of_a_function_is_timed():
    """Code objects compare EQUAL by value, so a set merged a reloaded module's
    copy into the original; sys.monitoring instruments OBJECTS, so the copy
    that actually ran went untimed (seen after a test reloaded a backend)."""
    src = "def twin():\n    return 1\n"
    first: dict = {}
    second: dict = {}
    exec(compile(src, "twin_module.py", "exec"), first)
    exec(compile(src, "twin_module.py", "exec"), second)
    original, copy = first["twin"], second["twin"]
    assert original.__code__ == copy.__code__ and original.__code__ is not copy.__code__
    with stage_timing(StageMap(functions={"twin": [original]})) as r:
        copy()
    assert r.stages["twin"].calls == 1, "the running copy was not instrumented"


def test_nothing_is_instrumented_after_the_block():
    with stage_timing(StageMap(functions={"outer": [outer]})) as r:
        outer()
    outer()
    assert r.stages["outer"].calls == 1
