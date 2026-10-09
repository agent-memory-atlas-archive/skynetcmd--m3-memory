"""Per-stage timing on Python 3.12's ``sys.monitoring`` (PEP 669): the one owner.

``stage_timing(stage_map)`` instruments ONLY the functions a stage map names
(LOCAL events on their code objects), so everything else runs uninstrumented
while it is active and nothing at all when it is not. Measured on 3.12 and
3.14 (scratch probe, 2026-10-09):

* a callback's ``sys._getframe(1)`` is the monitored frame, and its identity
  is stable across ``await`` suspensions, so concurrent invocations of one
  coroutine (two searches at once) are told apart;
* a coroutine reports PY_START, then PY_YIELD/PY_RESUME per suspension, then
  PY_RETURN or PY_UNWIND;
* PY_UNWIND cannot be a local event ("invalid local event set"), so it is
  enabled globally and filtered by code object; the cost to unmonitored code
  measured +3.4% on 3.12 and noise on 3.14, paid only while timing is active;
* event values differ between versions (0x1005 vs 0x2005), so only the
  ``sys.monitoring.events`` names are used here, never numbers.

C calls (database drivers, the Rust core) are timed with a local CALL event on
the calling functions plus the C_RETURN / C_RAISE ancillary events, attributed
by a classifier the stage map supplies.

Times are wall-clock: a stage's ``wall_s`` includes the awaits inside it (the
latency a caller sees); ``self_s`` excludes nested stages that ran in the same
task, so stages sum without double counting. Work fanned out to OTHER tasks is
not subtracted from its parent.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import CodeType, ModuleType
from typing import Any

_MON = sys.monitoring
_EV = _MON.events
# Tool ids tried in order: the profiler slot, then 3 and 4, which sys.monitoring
# leaves unassigned. cProfile holds PROFILER_ID while it is enabled (measured on
# 3.14), so a fixed slot would refuse to time a search under cProfile or an IDE
# profiler; with the fallback both run.
_TOOL_CANDIDATES: tuple[int, ...] = (_MON.PROFILER_ID, 3, 4)
_TOOL_NAME = "m3-stage-timing"
_SESSION_LOCK = threading.Lock()  # sys.monitoring is process-wide: one session at a time


class MonitoringBusy(RuntimeError):
    """The profiler tool slot is taken, or a timing session is already active."""


@dataclass(slots=True)
class StageStats:
    calls: int = 0
    wall_s: float = 0.0
    self_s: float = 0.0
    max_s: float = 0.0
    errors: int = 0

    def as_dict(self) -> dict[str, float | int]:
        return {"calls": self.calls, "wall_ms": round(self.wall_s * 1000, 3),
                "self_ms": round(self.self_s * 1000, 3), "max_ms": round(self.max_s * 1000, 3),
                "errors": self.errors}


@dataclass(slots=True)
class StageReport:
    stages: dict[str, StageStats] = field(default_factory=dict)

    def stat(self, name: str) -> StageStats:
        return self.stages.setdefault(name, StageStats())

    def as_dict(self) -> dict[str, dict[str, float | int]]:
        return {name: s.as_dict() for name, s in sorted(self.stages.items())}


@dataclass(frozen=True, slots=True, kw_only=True)
class StageMap:
    """What to time. ``functions``: stage name -> the Python callables in it.
    ``c_call_sites``: functions whose C calls are inspected; ``classify_c`` maps a
    C callable to a stage name, or None to ignore it."""

    functions: Mapping[str, Iterable[Callable[..., Any]]]
    c_call_sites: Iterable[Callable[..., Any]] = ()
    # Every function, method and nested function DEFINED in these modules is a
    # C-call site too: the C calls of interest (a driver's execute) are usually
    # made by helpers, not by the stage functions themselves.
    c_call_modules: Iterable[ModuleType | type] = ()
    classify_c: Callable[[object, object], str | None] | None = None


def _codes_in_module(module: ModuleType | type) -> set[CodeType]:
    """The code objects of everything defined in ``module`` (nested included), or,
    given a class, of its methods. Pass the LIVE class when it matters which copy
    runs: a module loaded twice holds different code objects than the one the
    running instance came from."""
    found: set[CodeType] = set()

    def add(code: CodeType) -> None:
        if code not in found:
            found.add(code)
            for const in code.co_consts:
                if isinstance(const, CodeType):
                    add(const)

    def visit(obj: object) -> None:
        target = getattr(obj, "__func__", obj)
        code = getattr(target, "__code__", None)
        if isinstance(code, CodeType) and getattr(target, "__module__", None) == module.__name__:
            add(code)

    if isinstance(module, type):
        for attr in vars(module).values():
            target = attr.fget if isinstance(attr, property) and attr.fget else attr
            code = getattr(getattr(target, "__func__", target), "__code__", None)
            if isinstance(code, CodeType):
                add(code)
        return found
    for value in vars(module).values():
        if isinstance(value, type) and value.__module__ == module.__name__:
            for attr in vars(value).values():
                visit(attr.fget if isinstance(attr, property) and attr.fget else attr)
        else:
            visit(value)
    return found


def c_target(callable_: object, arg0: object) -> tuple[str, str]:
    """``(owner module, name)`` of a C call as the CALL event reports it. A
    method call arrives as the UNBOUND descriptor (``Connection.execute``) with
    the instance as ``arg0`` (measured on 3.12 and 3.14); a bound builtin
    carries its owner in ``__self__``. Classifiers use this, not their own guess."""
    name = getattr(callable_, "__name__", "")
    if type(callable_).__name__ == "method_descriptor":
        owner: object = arg0
    else:
        owner = getattr(callable_, "__self__", None)
    module = owner.__name__ if isinstance(owner, type(sys)) else type(owner).__module__
    return module or "", name


@dataclass(slots=True)
class _Open:
    stage: str
    started: float
    parent: _Open | None
    child_s: float = 0.0


def _live_copies(keys: set[tuple[str, str]]) -> list[CodeType]:
    """Every live code object whose (source file, qualified name) is in ``keys``,
    nested functions included, unique by IDENTITY. A stale module copy that is
    still referenced is still run; measured: after a test that reloads a backend
    module, three copies of a backend method were alive and the search ran a
    stale one. One gc scan per session (~14 ms over 128k objects)."""
    import gc

    qualnames = {q for _, q in keys}
    found: dict[int, CodeType] = {}
    for obj in gc.get_objects():
        code = getattr(obj, "__code__", None) if callable(obj) else None
        # qualname first (cheap), then the normalised path: a copy loaded through
        # spec_from_file_location spells the same file differently.
        if (isinstance(code, CodeType) and code.co_qualname in qualnames
                and _code_key(code) in keys):
            pending = [code]
            while pending:
                c = pending.pop()
                found[id(c)] = c
                pending += [k for k in c.co_consts if isinstance(k, CodeType)]
    return list(found.values())


def _resolve_codes(stage_map: StageMap) -> tuple[dict[CodeType, str], set[CodeType], list[CodeType]]:
    """For ``stage_map``: code -> stage and the C-call sites, both for LOOKUP in
    the callbacks, plus every code OBJECT to instrument.

    ⚠ Code objects compare EQUAL by value, so a set or dict merges the copies of
    one function into a single entry, while sys.monitoring.set_local_events acts
    on each OBJECT. Lookup by equality is right (any copy maps to its stage);
    instrumenting must be by identity, or only one copy is timed (it was: the
    search ran an uninstrumented copy and reported no DB time)."""
    code_stage: dict[CodeType, str] = {}
    for stage, fns in stage_map.functions.items():
        for fn in fns:
            code_stage[_code_of(fn)] = stage
    call_sites = {_code_of(fn) for fn in stage_map.c_call_sites}
    for module in stage_map.c_call_modules:
        call_sites |= _codes_in_module(module)
    objects: dict[int, CodeType] = {id(c): c for c in (*code_stage, *call_sites)}
    keys = {_code_key(c) for c in objects.values()}
    for code in _live_copies(keys):
        objects.setdefault(id(code), code)
    return code_stage, call_sites, list(objects.values())


def _code_key(code: CodeType) -> tuple[str, str]:
    """(normalised source path, qualified name): equal for every copy of a function."""
    import os

    return os.path.normcase(os.path.realpath(code.co_filename)), code.co_qualname


def _code_of(fn: Callable[..., Any]) -> CodeType:
    target = getattr(fn, "__func__", fn)
    target = getattr(target, "__wrapped__", target)
    code = getattr(target, "__code__", None)
    if not isinstance(code, CodeType):
        raise TypeError(f"observed: {fn!r} has no Python code object. inspect: the stage map; "
                        "C callables are timed through classify_c, not functions")
    return code


def _context_key() -> tuple[int, int]:
    try:
        task = asyncio.current_task()  # C implementation since 3.12: cheap
    except RuntimeError:
        task = None
    return threading.get_ident(), id(task)


@contextmanager
def stage_timing(stage_map: StageMap) -> Iterator[StageReport]:
    """Time the stages of ``stage_map`` for the duration of the block."""
    code_stage, call_sites, objects = _resolve_codes(stage_map)
    classify = stage_map.classify_c

    report = StageReport()
    frames: dict[int, _Open] = {}        # id(frame) -> its open stage
    top: dict[tuple[int, int], _Open] = {}  # context -> innermost open stage
    c_open: dict[tuple[int, int], list[tuple[object, _Open]]] = {}

    def _push(stage: str) -> _Open:
        key = _context_key()
        entry = _Open(stage=stage, started=time.perf_counter(), parent=top.get(key))
        top[key] = entry
        return entry

    def _close(entry: _Open, *, error: bool) -> None:
        wall = time.perf_counter() - entry.started
        stat = report.stat(entry.stage)
        stat.calls += 1
        stat.wall_s += wall
        stat.self_s += max(0.0, wall - entry.child_s)
        stat.max_s = max(stat.max_s, wall)
        stat.errors += error
        if entry.parent is not None:
            entry.parent.child_s += wall
        key = _context_key()
        if top.get(key) is entry:
            if entry.parent is None:
                del top[key]
            else:
                top[key] = entry.parent

    def on_start(code: CodeType, _offset: int) -> None:
        stage = code_stage.get(code)
        if stage is not None:
            frames[id(sys._getframe(1))] = _push(stage)

    def on_return(code: CodeType, _offset: int, _value: object) -> None:
        entry = frames.pop(id(sys._getframe(1)), None)
        if entry is not None:
            _close(entry, error=False)

    def on_unwind(code: CodeType, _offset: int, _exc: BaseException) -> None:
        if code in code_stage:
            entry = frames.pop(id(sys._getframe(1)), None)
            if entry is not None:
                _close(entry, error=True)

    def on_call(code: CodeType, _offset: int, callable_: object, arg0: object) -> None:
        if classify is not None and code in call_sites:
            stage = classify(callable_, arg0)
            if stage is not None:
                key = _context_key()
                innermost = top.get(key)
                if innermost is not None and innermost.stage == stage:
                    return  # already inside this stage (a driver call in a db wrapper)
                c_open.setdefault(key, []).append((callable_, _push(stage)))

    def _c_end(error: bool) -> Callable[[CodeType, int, object, object], None]:
        def end(code: CodeType, _offset: int, callable_: object, _arg0: object) -> None:
            stack = c_open.get(_context_key())
            if stack and stack[-1][0] is callable_:
                _close(stack.pop()[1], error=error)
        return end

    if not _SESSION_LOCK.acquire(blocking=False):
        raise MonitoringBusy("observed: a stage-timing session is already active in this process. "
                             "inspect: sys.monitoring is process-wide; run one session at a time")
    try:
        tool = next((t for t in _TOOL_CANDIDATES if _MON.get_tool(t) is None), None)
        if tool is None:
            held = ", ".join(f"{t}={_MON.get_tool(t)!r}" for t in _TOOL_CANDIDATES)
            raise MonitoringBusy(f"observed: every sys.monitoring tool id m3 can use is held ({held}). "
                                 "inspect: stop one of those tools (a profiler, debugger) and retry")
        _MON.use_tool_id(tool, _TOOL_NAME)
        try:
            callbacks: dict[int, Callable[..., object]] = {_EV.PY_START: on_start, _EV.PY_RETURN: on_return,
                         _EV.PY_UNWIND: on_unwind, _EV.CALL: on_call,
                         _EV.C_RETURN: _c_end(False), _EV.C_RAISE: _c_end(True)}
            for event, cb in callbacks.items():
                _MON.register_callback(tool, event, cb)
            for code in objects:  # by identity: every live copy
                events = 0
                if code in code_stage:
                    events |= _EV.PY_START | _EV.PY_RETURN
                if code in call_sites:
                    events |= _EV.CALL | _EV.C_RETURN | _EV.C_RAISE
                _MON.set_local_events(tool, code, events)
            _MON.set_events(tool, _EV.PY_UNWIND)  # global-only event (see module doc)
            yield report
        finally:
            _MON.set_events(tool, 0)
            for code in objects:
                _MON.set_local_events(tool, code, 0)
            for event in (_EV.PY_START, _EV.PY_RETURN, _EV.PY_UNWIND, _EV.CALL,
                          _EV.C_RETURN, _EV.C_RAISE):
                _MON.register_callback(tool, event, None)
            _MON.free_tool_id(tool)
    finally:
        _SESSION_LOCK.release()
