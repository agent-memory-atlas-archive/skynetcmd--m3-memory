"""pytest configuration and fixtures for chatlog + main-DB tests."""

import importlib
import json

# Add bin/ directory to Python path so tests can import bin modules
import os as _os

# Short-circuit cli._ensure_utf8's Windows UTF-8 re-exec for the whole test
# session. That helper runs at IMPORT time of m3_memory.cli (module-level call);
# on Windows, when the interpreter wasn't launched with -X utf8, it spawns a
# subprocess and sys.exit()s — which aborts any test that imports cli with a
# bare SystemExit (e.g. test_installer::test_auto_install_opt_out_via_env).
# Setting this sentinel makes _ensure_utf8 return immediately. Must be set
# BEFORE any test imports cli, so it lives at conftest import time.
_os.environ.setdefault("_M3_UTF8_REEXEC", "1")

# The suite runs the native m3_core_rs extension when it is installed: that is
# the shipping configuration, and a suite that disables it tests only the
# fallback. What must NOT happen in unit tests is the in-process tier-1
# EmbeddedEmbedder loading — a multi-second CUDA context + model load that
# (a) blows the doctor <5s SLO assertions and (b) leaves warm global embed state
# that poisons run-order-dependent tier-ordering / dedup / cold-cascade tests.
# memory/embed.py allows that load only when a .embed_config.json is present
# (the collection-time config root below is empty) or M3_EMBED_INPROC=1, so pin
# the opt-in off. setdefault so an outer environment that deliberately set it
# wins. Must run at conftest IMPORT time: memory/embed.py reads it once.
_os.environ.setdefault("M3_EMBED_INPROC", "0")

# Point the config root at an empty directory for COLLECTION. The autouse
# sandbox pins it per test, but modules imported during collection read config
# before any fixture runs: llm_failover builds LLM_ENDPOINTS from
# <config_root>/.llm_config.json, so a developer's real file would reach tests.
# Unconditional (not setdefault): an outer value would be that same leak.
import atexit as _atexit  # noqa: E402
import shutil as _shutil  # noqa: E402
import tempfile as _tempfile  # noqa: E402

# The engine and logs roots too, for the same reason at the OTHER end: code that
# runs after the last test (atexit flushes, session teardown) is outside every
# per-test sandbox. migrate_memory runs printed AFTER pytest's summary auto-migrated
# test stores and wrote pre-up backups into the real ~/.m3/engine/backups (~6 per
# PostgreSQL run). Registered first, so these removals run after every later
# atexit hook.
for _var, _prefix in (
    ("M3_CONFIG_ROOT", "m3-test-config-"),
    ("M3_ENGINE_ROOT", "m3-test-engine-"),
    ("M3_LOGS_ROOT", "m3-test-logs-"),
):
    _os.environ[_var] = _tempfile.mkdtemp(prefix=_prefix)
    _atexit.register(_shutil.rmtree, _os.environ[_var], True)
# The trusted wolfSSL directory derives from the config root's parent; keep it
# on the real installed library so FIPS-mode collection does not fail closed
# (same remedy as the per-test sandbox below).
_real_lib_dir = _os.path.join(_os.path.expanduser("~"), ".m3", "lib")
if _os.path.isdir(_real_lib_dir) and not _os.environ.get("M3_LIB_DIR"):
    _os.environ["M3_LIB_DIR"] = _real_lib_dir
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_BIN_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "bin")
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

# A bin/ script a test spawns (`[sys.executable, "bin/x.py"]`) imports m3_memory
# for the version/OS seams. Point every child at THIS checkout's package: without
# it a child fails on an interpreter that lacks m3_memory, or silently imports a
# different installed version than the one under test.
_REPO_ROOT = _os.path.dirname(_BIN_DIR)
_os.environ["PYTHONPATH"] = _os.pathsep.join(p for p in (_REPO_ROOT, _os.environ.get("PYTHONPATH", "")) if p)


def simulate_os(monkeypatch, name: str) -> None:
    """Present OS ``name`` ('Windows' | 'Darwin' | 'Linux') to code that asks the
    OS seam, by turning the one knob the seam reads (m3_memory._platform.PLATFORM_OF).
    Call it in the test BODY: conftest restores the real platform at each report
    phase, so a fixture's patch is gone before the body runs."""
    from m3_memory._platform import PLATFORM_OF

    monkeypatch.setattr(sys, "platform", PLATFORM_OF[name])


def embed_backend_reachable() -> bool:
    """True iff a tier-2 embedding backend answers a TCP connect quickly.

    Used to gate SLO / integration tests whose premise REQUIRES a reachable
    embedder (warm-cascade latency, tier-4 redaction roundtrip). On CI there is
    no embedder, so every cascade probe waits out its full retry/backoff and the
    sub-3s SLO can't hold — that's an environment limitation, not a regression.
    A fast 0.5s connect probe distinguishes the two without coupling the test to
    the network's timeout behavior.
    """
    import re
    import socket

    url = _os.environ.get("M3_EMBED_FALLBACK_URL", "http://127.0.0.1:8082")
    m = re.search(r"://([^:/]+):(\d+)", url)
    if not m:
        return False
    host, port = m.group(1), int(m.group(2))
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _llm_chat_reachable() -> bool:
    """True iff a local OpenAI-compatible CHAT endpoint answers a real completion.

    Gates `requires_llm` tests (the citation-drift judge). A bare TCP probe is not
    enough: LM Studio accepts the connection but returns 401 without the resolved
    token, so we do the same authenticated round-trip the judge does (token via
    auth_utils) and require a non-empty reply. Fast-fails on any error so the suite
    stays hermetic when no model is loaded."""
    url = (
        _os.environ.get("M3_WIKI_DRIFT_URL")
        or _os.environ.get("M3_WIKI_SYNTH_URL")
        or "http://127.0.0.1:1234/v1/chat/completions"
    )
    try:
        import httpx

        sys.path.insert(0, _BIN_DIR)
        try:
            from auth_utils import get_api_key  # type: ignore

            token = get_api_key("LM_API_TOKEN")
        except Exception:
            token = None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = httpx.post(
            url,
            json={
                "messages": [{"role": "user", "content": "reply with: ok"}],
                "max_tokens": 5,
                "temperature": 0,
                "stream": False,
            },
            headers=headers,
            timeout=5.0,
        )
        r.raise_for_status()
        return bool(r.json()["choices"][0]["message"]["content"])
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Capability probes + gating (docs/design/TEST_SUITE_DESIGN.md)
# ──────────────────────────────────────────────────────────────────────────────
# ONE probe per external capability, and ONE marker per capability. A test that
# needs a resource carries the `requires_*` marker; pytest_collection_modifyitems
# auto-skips it when the probe reports the resource absent — so no test file
# re-derives DSNs, reachability checks, or skipif conditions. This replaces the
# five hand-rolled gating idioms catalogued in the design doc and fixes their
# latent inconsistencies (a typo'd PG skip reason, presence-vs-reachability drift,
# two GGUF env vars, three native-wheel idioms).


_PG_URL_OPTION: "str | None" = None


def pytest_addoption(parser):
    parser.addoption(
        "--m3-pg-url",
        default=None,
        help="DSN of a THROWAWAY PostgreSQL cluster for the live tests (never a production one). "
        "A pytest option, not an env var: m3's settings come from files.",
    )


def pytest_configure(config):
    global _PG_URL_OPTION
    _PG_URL_OPTION = config.getoption("--m3-pg-url")


def pg_dsn() -> "str | None":
    """The Postgres DSN for live tests: ``pytest --m3-pg-url``, the ONE source.

    Never an env var: the lanes, CI and the
    pre-commit pass the option, and a DSN in the environment (a developer's own
    M3_PRIMARY_PG_URL, or PG_URL, which points at PROD) can never select the
    cluster tests write to. Centralized so the 12 former copies cannot drift.
    """
    return (_PG_URL_OPTION or "").strip() or None


def select_backend(name: str) -> None:
    """Select the backend for the rest of this test the way `m3 --set` does: a
    flag-level setting, live at once, importing nothing from ``memory`` (so it
    works under a test that blocks that package). No env;
    ``_process_flags_cleared`` undoes it after the test. Use
    ``use_backend`` instead when the test needs the file and a stored DSN."""
    from m3_core.config import settings as st

    st.set_from_cli({"db.backend": name})


def use_backend(name: str, dsn: "str | None" = None) -> None:
    """Select the backend for this test the way a user does: ``[db] backend`` in
    the sandbox's m3_config.toml, and the PostgreSQL DSN in the sandbox's
    user-only secrets file. No env var.
    Clears the settings snapshot, the secret cache and the selector so the next
    read sees them."""
    from m3_core import secret_store
    from m3_core.config import locate
    from m3_core.config import settings as st
    from m3_core.config.keys import DB_BACKEND
    from m3_core.config.writer import set_values
    from memory.backends import selector

    set_values(locate(), {DB_BACKEND: name})
    if dsn:
        secret_store.set_secret("M3_PRIMARY_PG_URL", dsn)
    st._reset_for_tests()
    secret_store.clear_secret_cache()
    selector._reset_for_tests()


def _pg_reachable() -> bool:
    """True iff a Postgres cluster answers within a short connect timeout.
    Reachability, not mere presence — the presence-only gate in the old
    test_backend_conformance was an inconsistency this unifies away.

    The result is cached per pytest session (see the collection hook), so a SINGLE
    transient failure here would silently skip EVERY requires_pg test for the whole
    run — a false green. That is a real hazard when the cluster is still accepting
    connections (CI service just started, a WSL `service postgresql start` mid-run,
    a brief pause). So retry a few times with a short backoff before concluding
    "not reachable": a genuinely-absent cluster still fails fast (no DSN → instant
    False; refused connection → the retries add at most ~2s), while a momentary
    blip no longer poisons the session's PG coverage."""
    dsn = pg_dsn()
    if not dsn:
        return False
    import time as _time

    try:
        import psycopg2
    except Exception:  # noqa: BLE001 — driver absent = not reachable
        return False
    for attempt in range(3):
        try:
            psycopg2.connect(dsn, connect_timeout=3).close()
            return True
        except Exception:  # noqa: BLE001 — any failure = retry, then give up
            if attempt < 2:
                _time.sleep(1.0)
    return False


def _native_wheel_present() -> bool:
    """True iff the m3_core_rs native extension is importable (not the
    pure-Python fallback). Unifies the three former idioms (importorskip /
    in-body skip / _has_native_governor)."""
    import importlib.util

    return importlib.util.find_spec("m3_core_rs") is not None


def gguf_path() -> "str | None":
    """The GGUF model path for tests that need a real model — via the SINGLE
    canonical env var M3_TEST_GGUF (the old suite split between M3_TEST_GGUF and
    M3_EMBED_GGUF for the same purpose)."""
    return (_os.environ.get("M3_TEST_GGUF") or "").strip() or None


_FILES_DB = Path(__file__).resolve().parent.parent / "memory" / "files_database.db"


def _files_db_present() -> bool:
    return _FILES_DB.is_file()


def _memory_core_importable() -> bool:
    """True iff `import memory_core` succeeds in THIS environment.

    Not a proxy for "is package X installed" — the probe is the import itself,
    because the failure it guards is a BROKEN TRANSITIVE CHAIN, not a missing
    direct dependency.

    Measured 2026-09-19: every CI lane (3 OSes x 4 Pythons) errored at fixture
    setup with `ModuleNotFoundError: No module named 'torchvision'`. Nothing in
    m3 imports torchvision. The chain is: memory_core -> transformers, and
    transformers' lazy `__getattr__` (transformers/__init__.py:867) resolves an
    attribute that pulls models/aria/image_processing_aria.py, which does
    `from torchvision.transforms.v2 import functional as tvF`. The same import
    SUCCEEDS locally with torchvision absent, so keying on torchvision's
    presence would skip tests that can actually run.

    Importing here is safe and cheap: memory_core is already imported by most of
    the suite, so this is a cache hit in the common case.
    """
    import importlib

    # Probe EVERY module the guarded fixtures import, not just the one this
    # marker is named after. Measured 2026-09-29 on a fresh Linux environment
    # built from the current requirements: `memory_core` imported fine, the
    # marker did not skip, and `test_elbow_trim`'s fixture then ERRORED at setup
    # with `ModuleNotFoundError: No module named 'torch'` — because that fixture
    # also does `from memory import search`, and THAT is the import on the heavy
    # chain. A probe narrower than the code it guards does not guard it.
    #
    # torch left requirements.txt recently, so this only shows up in an
    # environment built after that change; it stayed invisible on a developer box
    # whose venv still had torch installed from before (§3 hermeticity: green
    # because a leftover dependency rescued the unprobed path).
    for name in ("memory_core", "memory.search"):
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 — ANY import failure means "cannot run here"
            return False
    return True


# Every skip names the dimension that caused it, so a run reports WHY tests did
# not run and a skip that names none stands out (pytest_terminal_summary below):
#   os     - the test needs a real facility of another OS (OS seam)
#   python - the test needs another interpreter version (version seam)
#   db     - the test needs another database backend or engine version (DB seam)
#   dep    - an optional package / native wheel / model file is not installed
#   env    - an external service (embedder, LLM, network) is not reachable
#   na     - the case does not apply here (nothing to compare, a path not taken).
#            The suspect kind: a skip that should have been an assertion hides
#            here, so the report lists every one for review.
SKIP_DIMENSIONS = ("os", "python", "db", "dep", "env", "na")


def skip_reason(dimension: str, text: str) -> str:
    """The reason string for a skip: ``pytest.skip(skip_reason("dep", "no numpy"))``."""
    if dimension not in SKIP_DIMENSIONS:
        raise ValueError(
            f"unknown skip dimension; observed: {dimension!r}. "
            f"inspect: use one of {SKIP_DIMENSIONS} (tests/conftest.py)"
        )
    return f"[{dimension}] {text}"


# Marker -> (probe, dimension, reason). A marked test is skipped when its probe
# returns False. One place; adding a capability is one row.
_CAPABILITY_PROBES = {
    "requires_pg": (_pg_reachable, "db", "no reachable PostgreSQL (pass pytest --m3-pg-url=<a throwaway cluster DSN>)"),
    "requires_embedder": (
        embed_backend_reachable,
        "env",
        "no reachable embedder (M3_EMBED_FALLBACK_URL, default :8082)",
    ),
    "requires_native": (_native_wheel_present, "dep", "m3_core_rs native wheel not installed"),
    "requires_gguf": (lambda: gguf_path() is not None, "dep", "no GGUF model (set M3_TEST_GGUF)"),
    "requires_files_db": (_files_db_present, "dep", "shipped files_database.db absent"),
    "requires_llm": (
        _llm_chat_reachable,
        "env",
        "no reachable local chat model (set M3_WIKI_DRIFT_URL / load a model in LM Studio)",
    ),
    "requires_memory_core": (
        _memory_core_importable,
        "dep",
        "memory_core / memory.search are not importable here (a "
        "transitive ML dep such as torch is absent or broken in this "
        "environment — see _memory_core_importable)",
    ),
}


# OS markers ask the OS seam (m3_memory/_platform.py), as code does, instead of
# each test testing sys.platform / os.name itself. Not `integration`: an OS is
# not an external service, so these tests stay in the hermetic lane. They are for
# a test that needs the REAL facility (file modes, the registry, launchd); a test
# of OS-branch LOGIC should run everywhere with simulate_os instead of skipping.
def _os_probes() -> dict:
    from m3_memory._platform import is_apple_silicon, is_linux, is_macos, is_windows

    return {
        "requires_windows": (is_windows, "Windows only"),
        "requires_posix": (lambda: is_macos() or is_linux(), "POSIX only (macOS / Linux)"),
        "requires_macos": (is_macos, "macOS only"),
        "requires_linux": (is_linux, "Linux only"),
        "requires_apple_silicon": (is_apple_silicon, "Apple Silicon only"),
    }


def _python_skip(item) -> str | None:
    """``@pytest.mark.requires_python(3, 13)``: needs that interpreter or newer,
    asked of the version seam (m3_memory/_pyver.py)."""
    m = item.get_closest_marker("requires_python")
    if m is None:
        return None
    from m3_memory._pyver import PY

    need = tuple(m.args)
    if PY >= need:
        return None
    return skip_reason("python", f"needs Python {'.'.join(map(str, need))}+, running {PY[0]}.{PY[1]}")


def pytest_collection_modifyitems(config, items):
    """Auto-skip capability-marked tests whose resource is absent, and give every
    requires_* test the `integration` umbrella marker so `-m "not integration"`
    selects the hermetic lane in one expression. Probes are memoized per session
    so a marker used by 50 tests probes the cluster once, not 50 times."""
    os_probes = _os_probes()
    for item in items:
        for m, (probe, reason) in os_probes.items():
            if m in item.keywords and not probe():
                item.add_marker(pytest.mark.skip(reason=skip_reason("os", reason)))
        py_reason = _python_skip(item)
        if py_reason:
            item.add_marker(pytest.mark.skip(reason=py_reason))
    cache: dict[str, bool] = {}

    def _ok(marker: str) -> bool:
        if marker not in cache:
            cache[marker] = bool(_CAPABILITY_PROBES[marker][0]())
        return cache[marker]

    for item in items:
        marks = set(item.keywords)
        capability_marks = [m for m in _CAPABILITY_PROBES if m in marks]
        if capability_marks:
            item.add_marker(pytest.mark.integration)
        for m in capability_marks:
            if not _ok(m):
                _probe, dim, reason = _CAPABILITY_PROBES[m]
                item.add_marker(pytest.mark.skip(reason=skip_reason(dim, reason)))


# ── skip report: what did not run, by dimension ──────────────────────────────
_SKIPS: list[tuple[str, str]] = []  # (nodeid, reason)


def _skip_dimension(reason: str) -> str | None:
    if reason.startswith("[") and "]" in reason:
        dim = reason[1 : reason.index("]")]
        if dim in SKIP_DIMENSIONS:
            return dim
    return None


def _strict_skips() -> bool:
    return _os.environ.get("M3_STRICT_SKIPS") == "1"


def pytest_runtest_logreport(report):
    if report.skipped and isinstance(report.longrepr, tuple):
        reason = str(report.longrepr[2])
        _SKIPS.append((report.nodeid, reason.removeprefix("Skipped: ")))


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if not _SKIPS:
        return
    by_dim: dict[str, int] = {}
    unclassified: list[tuple[str, str]] = []
    for nodeid, reason in _SKIPS:
        dim = _skip_dimension(reason)
        if dim is None:
            unclassified.append((nodeid, reason))
        else:
            by_dim[dim] = by_dim.get(dim, 0) + 1
    tr = terminalreporter
    tr.section("skipped tests by dimension")
    for dim in SKIP_DIMENSIONS:
        if by_dim.get(dim):
            tr.write_line(f"  {dim:<7} {by_dim[dim]:>5}")
    na = [(n, r) for n, r in _SKIPS if _skip_dimension(r) == "na"]
    for nodeid, reason in na[:25]:
        tr.write_line(f"    na: {nodeid}: {reason[:120]}")
    if unclassified:
        tr.write_line(
            f"  UNCLASSIFIED; observed: {len(unclassified)} skips name no dimension, so "
            "nothing says they were skipped for a real reason. inspect: use "
            "conftest.skip_reason(dimension, text) or a requires_* marker",
            yellow=True,
        )
        for nodeid, reason in unclassified[:25]:
            tr.write_line(f"    {nodeid}: {reason[:120]}")
        if len(unclassified) > 25:
            tr.write_line(f"    ... and {len(unclassified) - 25} more")
        if _strict_skips():
            tr.write_line(
                "  M3_STRICT_SKIPS=1 fails this run; observed: the unclassified skips "
                "above. inspect: classify each, or unset M3_STRICT_SKIPS",
                red=True,
            )


def pytest_sessionfinish(session, exitstatus):
    """M3_STRICT_SKIPS=1 fails a run that has an unclassified skip."""
    if _strict_skips() and exitstatus == 0 and any(_skip_dimension(r) is None for _n, r in _SKIPS):
        session.exitstatus = 1


@pytest.fixture
def pg_url() -> str:
    """DSN for a reachable Postgres. Requesting this fixture in a `requires_pg`
    test guarantees (via the collection hook) the cluster is up, so this just
    returns it. Kept as a fixture so test bodies get the value without importing
    the probe."""
    dsn = pg_dsn()
    if not dsn or not _pg_reachable():
        _probe, dim, reason = _CAPABILITY_PROBES["requires_pg"]
        pytest.skip(skip_reason(dim, reason))
    return dsn


@pytest.fixture
def m3_settings():
    """Set m3 configuration for one test, instead of monkeypatching env vars.
    ``m3_settings({"search.row_cap": 10})`` installs a
    snapshot built from registry defaults only (no process env, no file, no
    .env), with those keys replaced, as what ``settings.current()`` returns; it
    is validated like any value. Reset after the test (and per test by the
    sandbox). Readers see it once 2D has moved them to the registry.
    """
    from m3_core.config import REGISTRY
    from m3_core.config import settings as st
    from m3_core.config.resolve import NO_DOTENV

    def use(values=None):
        snap = st.build(environ={}, dotenv=NO_DOTENV)
        if values:
            unknown = sorted(n for n in values if REGISTRY.get(n) is None)
            assert not unknown, f"m3_settings: not registry keys: {unknown}"
            snap = st.override(snap, {REGISTRY.get(n): v for n, v in values.items()})
        st._install_for_tests(snap)
        return snap

    yield use
    st._reset_for_tests()


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.requires_pg)])
def each_backend(request, monkeypatch):
    """Run the test once per shipped backend; yields the active StorageBackend.

    The sandbox clears M3_DB_BACKEND for every test, so a test runs on SQLite
    unless it selects PostgreSQL itself, and a PG lane alone does not put a
    plain test on PG. Request this fixture to cover both: the `postgres` case
    carries `requires_pg`, so it skips (never silently runs SQLite) when no
    cluster is reachable. Prefer it to a per-file `pg` fixture; 16 files carry
    their own copy today.
    """
    from memory.backends import selector

    name = request.param
    if name == "postgres":
        monkeypatch.delenv("M3_DATABASE", raising=False)
    use_backend(name, pg_dsn() if name == "postgres" else None)
    backend = selector.active_backend()
    assert backend.name == name, f"asked for {name}, the seam resolved {backend.name}"
    backend.ensure_schema()
    yield backend
    selector._reset_for_tests()


_REAL_OS_NAME = _os.name
_REAL_SYS_PLATFORM = sys.platform


def _real_is_windows() -> bool:
    from m3_memory._platform import is_windows

    return is_windows()


# Read once at import, before any test simulates another OS.
_ON_WINDOWS = _real_is_windows()


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    """Restore `os.name` and `sys.platform` BEFORE pytest renders a test report.

    A fixture cannot do this: pytest builds the report inside
    ``pytest_runtest_makereport``, which runs while the test's fixtures are
    still active, so monkeypatch teardown (and any autouse fixture) happens too
    late. ``repr_failure`` constructs ``Path(os.getcwd())``; with ``os.name``
    still forced to ``"nt"`` that is a ``WindowsPath``, which raises
    ``NotImplementedError`` on a POSIX host and aborts the whole session with an
    INTERNALERROR that never names the failing test.

    Restoring here — first, and around the report call — means a leaked
    ``os.name`` can no longer poison the reporter. See
    ``_restore_platform_identity`` below for the fixture-level half (which keeps
    the leak from reaching the *next* test).

    ``sys.platform`` is restored here for a second reason, found 2026-09-28: this
    hook fires for the ``call`` phase BEFORE any teardown fixture runs, so it is
    the only place that can stop a simulated platform from reaching *another
    fixture's teardown*. It was reaching one. `test_fips_integrity.py`'s
    crypto-repair finalizer reloads `crypto_provider`, and the two tests that set
    ``sys.platform = "linux"`` left it live through that reload: on a macOS host
    the resolver then looked for ``libwolfssl.so`` instead of the real
    ``libwolfssl.dylib``, found nothing, and under ``M3_FIPS_MODE=1`` failed
    closed — two teardown ERRORs reporting a machine state that did not exist.
    The ``os.name`` half of those same tests was already harmless because this
    hook had always restored it; the asymmetry was the whole bug.
    """
    if _os.name != _REAL_OS_NAME:
        _os.name = _REAL_OS_NAME
    if sys.platform != _REAL_SYS_PLATFORM:
        sys.platform = _REAL_SYS_PLATFORM
    outcome = yield
    _annotate_backend_mismatch(item, call, outcome)


def _backend_diagnosis(item, call) -> "str | None":
    """Explain a `requires_pg` failure that happened on the WRONG BACKEND.

    THE CLASS, not one instance. `requires_pg` asks "is a PostgreSQL cluster
    REACHABLE?" (see `_pg_reachable`). The test bodies assume something stronger:
    "the storage seam IS PostgreSQL". Those are different questions, and when
    they disagree the test runs PG SQL against a SQLite connection. What the
    operator sees is:

        E   sqlite3.OperationalError: near "%": syntax error
        E   sqlite3.OperationalError: no such table: memory_items

    Neither mentions a backend, a DSN, or `M3_DB_BACKEND`. Measured 2026-09-30:
    23 failures and 1 error across 10 `*_pg_live` files on Windows, every message
    of that shape, and the file passed 3/3 when run alone — so the output pointed
    at the schema, the SQL, and the test, and never at the one thing that was
    actually wrong. Diagnosing it took far longer than fixing it would have.

    This adds the missing context to the report instead of changing behaviour,
    which matters for two reasons:
      * It CANNOT create a false failure: it only annotates a failure that
        already happened.
      * It is read-only: it never imports the selector, and
        `resolve_backend_name()` is a pure env read (no memo), so asking the
        already-loaded selector caches nothing.
    """
    import sqlite3 as _sqlite3

    if call.when != "call" or call.excinfo is None:
        return None
    if not any(m.name == "requires_pg" for m in item.iter_markers()):
        return None

    exc = call.excinfo.value
    on_sqlite = isinstance(exc, _sqlite3.Error) or type(exc).__module__ == "sqlite3"

    # Never import — ask only a selector that is already loaded.
    env_backend = (
        _os.environ.get("M3_DB_BACKEND") or _os.environ.get("DB_BACKEND") or ""
    ).strip() or "(unset -> default sqlite)"
    _sel = sys.modules.get("memory.backends.selector")
    try:
        resolved = _sel.resolve_backend_name() if _sel is not None else None
    except Exception as e:  # an invalid M3_DB_BACKEND is itself the answer
        resolved = f"<error: {e}>"
    dsn = pg_dsn()
    dsn_shown = "present" if dsn else "ABSENT"

    lines = [
        "observed: a test marked `requires_pg` failed" + (" while executing against SQLite" if on_sqlite else ""),
        f"           exception          : {type(exc).__module__}.{type(exc).__name__}",
        f"           M3_DB_BACKEND      : {env_backend}",
        f"           selector resolved  : {resolved!r}"
        + ("  <- NOT postgres" if resolved not in (None, "postgres") else ""),
        f"           PG DSN (pg_dsn())  : {dsn_shown}",
    ]
    if on_sqlite:
        lines += [
            "cause:    `requires_pg` gates on PostgreSQL being REACHABLE, not on the",
            "          seam being PostgreSQL. With a reachable DSN but a non-postgres",
            "          seam, PG SQL is handed to a SQLite connection — hence a sqlite3",
            "          error naming missing tables or `%` placeholders.",
            "inspect:  run this file ALONE (`pytest <file> -q`). If it passes alone, the",
            "          backend was decided elsewhere in the session: a test that resolved",
            "          the seam before this one, or a fixture that set M3_DB_BACKEND",
            "          without calling `selector._reset_for_tests()`. If it also fails",
            "          alone, set M3_DB_BACKEND=postgres for the run.",
        ]
    else:
        lines.append("inspect:  if this is not a backend problem, ignore this section.")
    return "\n".join(lines)


def _annotate_backend_mismatch(item, call, outcome) -> None:
    """Attach `_backend_diagnosis` to the report. Never raises: a diagnostic that
    can fail a test is worse than no diagnostic."""
    try:
        msg = _backend_diagnosis(item, call)
        if not msg:
            return
        report = outcome.get_result()
        report.sections.append(("m3 backend diagnosis", msg))
    except Exception:  # noqa: BLE001 — hygiene must never fail a test
        pass


# Entries a test run is ALLOWED to create in the repo root.
_ROOT_WRITE_ALLOWED = {".pytest_cache", "__pycache__", ".coverage", ".ruff_cache", ".mypy_cache", ".hypothesis"}


@pytest.fixture(scope="session", autouse=True)
def _repo_root_stays_clean():
    """Fail the SESSION if the suite leaves new entries in the repo working tree.

    Tests write to tmp_path; the repo is read-only to them. Nothing enforced that,
    and it broke twice in the same shape — a `subprocess.run` mock that assumed it
    only saw the one call it cared about, then treated `cmd[-1]` as a path:

      * 2026-09-29, `test_installer.py`: on Windows the mock also intercepted
        `["schtasks", "/Query", "/TN", "AgentOS_Dashboard"]` and created
        `AgentOS_Dashboard/README.md` in the CHECKOUT. Invisible on macOS, which
        never runs that branch, and invisible to every assertion in the suite —
        found only by running `git status` on the test box afterwards.

    Reported at session end rather than per test: the cost is one directory
    listing, and a leak is a property of the run, not of whichever test happened
    to be executing. Root-level only — deep artifacts (generated docs pages) are
    already covered by their own freshness tests.
    """
    root = Path(__file__).resolve().parent.parent
    before = {e.name for e in root.iterdir()}
    yield
    added = {e.name for e in root.iterdir()} - before - _ROOT_WRITE_ALLOWED
    assert not added, (
        f"the test run created {sorted(added)} in the repo working tree at {root}. "
        "Tests must write under tmp_path; a path built from mocked subprocess argv "
        "is the usual cause (see this fixture's docstring)."
    )


# The developer's REAL service directories, resolved at import -- before any test
# can patch HOME or os.path.expanduser -- so the guard below always watches the
# live ones, whatever a test redirects.
_REAL_HOME = Path(_os.path.expanduser("~"))
_SERVICE_DIRS = (_REAL_HOME / ".config" / "systemd" / "user", _REAL_HOME / "Library" / "LaunchAgents")
_SERVICE_NAME = re.compile(r"^(m3[-_].*\.(service|timer)|com\.(m3memory|skynetcmd)\..*\.plist)$")


def _service_files_state() -> dict:
    """{path: (mtime_ns, size)} of m3's unit/plist files in the real service dirs."""
    state = {}
    for d in _SERVICE_DIRS:
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if _SERVICE_NAME.match(p.name):
                try:
                    st = p.stat()
                except OSError:
                    continue
                state[str(p)] = (st.st_mtime_ns, st.st_size)
    return state


# The developer's REAL Claude Code config: only the parts m3 writes. A test that
# reached the keep-mode repair rewrote the live hooks and status line to the
# test's checkout and registered m3_memory with a pytest temp root (2026-10-15);
# the MCP server then failed to start for every later session. The whole files
# are not compared -- the live Claude Code process writes ~/.claude.json itself
# during a run -- and they are parsed only when their mtime moves.
_CLAUDE_FILES = {
    _REAL_HOME / ".claude" / "settings.json": lambda d: {k: d.get(k) for k in ("hooks", "statusLine")},
    _REAL_HOME / ".claude.json": lambda d: (d.get("mcpServers") or {}).get("m3_memory"),
}
_claude_seen: dict[Path, tuple[int, object]] = {}


def _claude_m3_state() -> dict:
    """{path: the m3-owned value} of the real Claude config, re-read on mtime change."""
    out = {}
    for path, pick in _CLAUDE_FILES.items():
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            out[path] = None
            continue
        seen = _claude_seen.get(path)
        if seen is None or seen[0] != mtime:
            try:
                value = pick(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                value = seen[1] if seen else None  # mid-write by the live client: keep the last read
            _claude_seen[path] = (mtime, value)
        out[path] = _claude_seen[path][1]
    return out


@pytest.fixture(autouse=True)
def _real_claude_config_untouched():
    """Fail the test that changes m3's entries in the REAL Claude Code config."""
    before = _claude_m3_state()
    yield
    after = _claude_m3_state()
    changed = [str(p) for p in before if before[p] != after[p]]
    if changed:
        pytest.fail(
            "this test changed m3's entries in the REAL Claude Code config: " + ", ".join(changed) + ". "
            "Stub the writer (setup_wizard._register_claude_server, environment_probe.repair, "
            "generate_configs) or redirect the home folder to tmp_path. Restore the file from its "
            ".backup_history or a .bak.",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _sys_path_restored():
    """Undo whatever a test added to ``sys.path``. Code under test legitimately
    puts a configured ``bin/`` on the path (``_bin.ensure_on_path``, honouring
    M3_PATH_BIN), and a test that points M3_PATH_BIN at a tmp dir with a fake
    ``memory_bridge.py`` left it there: every later ``import memory_bridge`` got
    the fake (test_lazy_tool_loading after test_agent_config_selfheal,
    2026-10-11). Collection-time inserts are in the baseline and stay."""
    saved = list(sys.path)
    yield
    sys.path[:] = saved


def clear_process_flags() -> None:
    """Undo the process-wide flag state a test left: flag-level settings
    (``settings.set_from_cli``) and the ``m3 --config`` pin. Both are process-wide
    by design (a CLI's flags for its whole run), so one test running an installer
    step that records ``db.backend = postgres``, or pinning a temp config file,
    would otherwise steer every later test in the process."""
    roots = sys.modules.get("m3_memory._roots")
    if roots is not None and roots.pinned_config_file() is not None:
        roots.pin_config_file(None)
    # The --database pin (M3_DATABASE design phase 2) is process-wide the same way.
    paths = sys.modules.get("m3_core.paths")
    if paths is not None and getattr(paths, "_pinned_db", None) is not None:
        paths.pin_database(None)
    if paths is not None and getattr(paths, "_pinned_chatlog", None) is not None:
        paths.pin_chatlog_db(None)
    st = sys.modules.get("m3_core.config.settings")
    if st is not None and st._CACHE._cli:
        st._reset_for_tests()


@pytest.fixture(autouse=True)
def _process_flags_cleared():
    yield
    clear_process_flags()


@pytest.fixture(autouse=True)
def _no_existing_install_by_default(monkeypatch):
    """setup's "keep current settings?" prompt keys off the developer's real
    store, which CI does not have; default it off so interactive-setup tests
    behave the same everywhere. Tests of that prompt patch it back on."""
    wiz = sys.modules.get("m3_memory.setup_wizard")
    if wiz is not None and hasattr(wiz, "_existing_install"):
        monkeypatch.setattr(wiz, "_existing_install", lambda: None)
    yield


_WAREHOUSE_SECRETS = frozenset({"PG_URL", "M3_CDW_PG_URL", "M3_CDW_URL"})


@pytest.fixture(autouse=True)
def _no_warehouse_secret(monkeypatch):
    """The env scrub cannot hide a warehouse DSN stored in the OS keyring or
    macOS Keychain, and resolve_warehouse_dsn() falls back to exactly that. A
    test that resolved it would reach the production warehouse."""
    import m3_sdk

    real = m3_sdk.get_secret
    monkeypatch.setattr(m3_sdk, "get_secret", lambda service: None if service in _WAREHOUSE_SECRETS else real(service))
    # The same block at the TIER level: m3_core.secret_store.resolve_pg_dsn reads
    # the keyring and vault tiers directly, past m3_sdk.get_secret, and on a dev
    # box those hold the real warehouse (and possibly primary) DSN. Measured
    # 2026-10-08: without this, a test resolved SkyPC's real PG_URL in-process.
    # A test of a tier patches the tier itself and so is unaffected.
    import functools

    import auth_utils
    import keyring

    # NO test reads the developer's real keyring or keychain, under ANY name:
    # secrets no longer come from env, so a test that misses the
    # secrets file falls through to the keyring, which on a dev box holds real
    # secrets. Three layers, because the real
    # store is reachable three ways: python keyring (an empty backend for the
    # test; a test that installs its own fake backend replaces it), the macOS
    # security CLI (stubbed), and the tier itself for every registry secret
    # (below, kept for a test that fakes _keyring_read to return a real-looking
    # value).
    real_backend = keyring.get_keyring()
    keyring.set_keyring(_EmptyKeyring())
    monkeypatch.setattr(auth_utils, "_macos_keychain_read", lambda name: None)
    real_keyring, real_vault = auth_utils._keyring_tier, auth_utils._vault_tier
    blocked_names = _DSN_SECRETS | _registry_secret_names()

    @functools.wraps(real_keyring)  # __wrapped__: a test with its OWN fake backend may lift the block
    def keyring_blocked(key, *, service="system"):
        return (None, None) if key in blocked_names else real_keyring(key, service=service)

    @functools.wraps(real_vault)
    def vault_blocked(service):
        return (None, None) if service in _DSN_SECRETS else real_vault(service)

    monkeypatch.setattr(auth_utils, "_keyring_tier", keyring_blocked)
    monkeypatch.setattr(auth_utils, "_vault_tier", vault_blocked)
    # And no test WRITES the developer's keyring, under any name: the store
    # falls through to the (sandboxed) secrets file instead. A test of the
    # writer patches _keyring_store or the keyring module itself.
    monkeypatch.setattr(
        auth_utils, "_keyring_store", lambda service, value: "keyring: writes blocked by the test sandbox"
    )
    try:
        yield
    finally:
        keyring.set_keyring(real_backend)


def _registry_secret_names() -> frozenset[str]:
    from m3_core.config import REGISTRY, KeyClass

    return frozenset(n for k in REGISTRY if k.klass is KeyClass.SECRET for n in (k.name, *k.env_names))


import keyring.backend as _keyring_backend  # noqa: E402


class _EmptyKeyring(_keyring_backend.KeyringBackend):
    """A keyring backend that holds nothing and stores nothing: what every test
    sees unless it installs its own."""

    priority = 1  # type: ignore[assignment]

    def get_password(self, service, username):
        return None

    def set_password(self, service, username, password):
        raise RuntimeError("keyring writes are blocked by the test sandbox")

    def delete_password(self, service, username):
        return None

    def get_credential(self, service, username):
        return None


# Every DSN name a test must never resolve from the developer's real keyring or vault.
_DSN_SECRETS = _WAREHOUSE_SECRETS | {"M3_PRIMARY_PG_URL", "M3_PG_URL"}


def dsn_env_snapshot() -> dict[str, str | None]:
    return {n: _os.environ.get(n) for n in _DSN_SECRETS}


def restore_dsn_env(before: dict[str, str | None]) -> list[str]:
    """Put each DSN name back as ``before`` had it; return the names that had changed."""
    leaked = sorted(n for n in _DSN_SECRETS if _os.environ.get(n) != before[n])
    for n in leaked:
        if before[n] is None:
            _os.environ.pop(n, None)
        else:
            _os.environ[n] = before[n]
    return leaked


_DSN_BEFORE = pytest.StashKey[dict]()


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    # Before ANY fixture of this test runs: the sandbox clears and restores these
    # names through fixtures, so a fixture-based check sees mid-sequence states.
    item.stash[_DSN_BEFORE] = dsn_env_snapshot()
    yield


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_teardown(item, nextitem):
    """Fail the test that leaves a DSN name in os.environ changed once EVERY
    fixture has torn down, and put it back: what remains is a raw write nothing
    undoes. One went unseen until 2026-10-12: an installer step wrote
    M3_PRIMARY_PG_URL into os.environ, and a LATER test (ai_mechanic's PostgreSQL
    refusal) passed in the full suite only because of that leaked DSN; run alone
    with no DSN it failed."""
    yield
    before = item.stash.get(_DSN_BEFORE, None)
    if before is None:
        return
    leaked = restore_dsn_env(before)
    if leaked:
        pytest.fail(
            f"observed: this test left {leaked} changed in os.environ (restored). "
            "inspect: set it with monkeypatch, or stop the code under test writing env",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _embed_port_serving_by_default(monkeypatch):
    """`m3 embedder install` waits for :8082 to answer before reporting success.
    Tests that stub the service as running would otherwise poll the developer's
    real port (or wait out the timeout where none runs). Tests of the wait call
    `embedder_admin._wait_serving_impl`."""
    ea = sys.modules.get("m3_memory.embedder_admin")
    if ea is not None and hasattr(ea, "_wait_serving"):
        monkeypatch.setattr(ea, "_wait_serving", lambda *a, **k: True)
    yield


@pytest.fixture(autouse=True)
def _isolated_shell_rc(monkeypatch, tmp_path):
    """The setup wizard reads and appends to the user's shell rc; point it at a
    temp file so tests neither depend on nor edit the developer's real one."""
    persist = sys.modules.get("m3_memory.wizard.persist")
    if persist is not None and hasattr(persist, "_pick_unix_shell_rc"):
        real_pick = persist._pick_unix_shell_rc

        def _pick():
            rc = real_pick()
            # Only the real home is redirected; a test that set HOME keeps its own.
            return tmp_path / rc.name if Path(rc).parent == _REAL_HOME else rc

        monkeypatch.setattr(persist, "_pick_unix_shell_rc", _pick)
    yield


@pytest.fixture(autouse=True)
def _isolated_env_sweep_home(monkeypatch, tmp_path):
    """The env-location sweep (m3_memory.install.env_sources) reads rc files and
    MCP client configs under home, ~/.claude.json among them; point it at a temp
    directory. A test that set HOME keeps its own (as _isolated_shell_rc)."""
    import importlib

    sources = importlib.import_module("m3_memory.install.env_sources")
    monkeypatch.setattr(sources, "_home", lambda: tmp_path if Path.home() == _REAL_HOME else Path.home())
    # The client config list is built from the real home too: rebase what it
    # builds there. Only those paths: on Windows pytest's own temp directory is
    # under the home, and a test's files there must stay where it put them.
    installer = importlib.import_module("m3_memory.installer")
    real_sources = installer._client_config_sources

    import tempfile

    temp_root = Path(tempfile.gettempdir()).resolve()

    def _rebase(p: Path) -> Path:
        # A path under the temp root is a test's own (its cwd .mcp.json).
        if not p.is_relative_to(_REAL_HOME) or p.resolve().is_relative_to(temp_root):
            return p
        return tmp_path / p.relative_to(_REAL_HOME)

    def _rebased():
        out = real_sources()
        if Path.home() != _REAL_HOME:
            return out
        return {client: [_rebase(p) for p in paths] for client, paths in out.items()}

    monkeypatch.setattr(installer, "_client_config_sources", _rebased)

    # The persistent Windows environment is the developer's real one: a sweep
    # (m3 setup imports it) would copy their real secrets into a test's files.
    # A test that installs a fake registry module still reads that fake.
    from m3_memory import _platform

    real_read = _platform.persistent_env_read

    def _sandboxed_read(names, unchecked=None):
        if _platform._winreg is _real_winreg() and _platform._winreg is not None:
            return []
        return real_read(names, unchecked)

    monkeypatch.setattr(_platform, "persistent_env_read", _sandboxed_read)
    yield


# The OS temp directory as it was before _session_tempdir redirected it. A test
# whose subject is that directory's own properties (its inherited ACL) needs the
# real one: pytest creates basetemp 0o700, so a child of it inherits a different ACL.
REAL_TEMPDIR: str | None = None


@pytest.fixture(scope="session", autouse=True)
def _session_tempdir(tmp_path_factory):
    """Route every temp file the suite makes into pytest's own basetemp.

    43 bare `tempfile.mkdtemp()` calls in tests/ (and helpers, and child
    processes) were never cleaned up: 3,142 m3 test dirs had piled up in the
    MacBook's $TMPDIR and 234,545 `tmp*` entries in SkyPC's %TEMP%, about 35 per
    run of five files. Pointing `tempfile` and the children's TMPDIR/TEMP/TMP here
    bounds it: pytest keeps only its last 3 basetemps, so a leak (or a killed
    run) is reclaimed automatically instead of accumulating forever.
    """
    import tempfile as _tf

    global REAL_TEMPDIR
    REAL_TEMPDIR = _tf.gettempdir()
    root = str(tmp_path_factory.mktemp("tempfile", numbered=False))
    saved_dir = _tf.tempdir
    saved_env = {k: _os.environ.get(k) for k in ("TMPDIR", "TEMP", "TMP")}
    _tf.tempdir = root
    for k in saved_env:
        _os.environ[k] = root
    try:
        yield root
    finally:
        _tf.tempdir = saved_dir
        for k, v in saved_env.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v


def _real_winreg():
    """The REAL registry module, captured the first time the guard runs, before
    any test body: tests replace _platform._winreg with a fake, and the guard
    must still see the developer's real HKCU."""
    global _REAL_WINREG
    if _REAL_WINREG is _UNSET:
        from m3_memory import _platform

        _REAL_WINREG = _platform._winreg
    return _REAL_WINREG


_UNSET = object()
_REAL_WINREG: object = _UNSET


def _user_env_snapshot() -> dict | None:
    """The persistent per-user environment, read through the OS seam from the
    real registry module."""
    from m3_memory._platform import user_env_snapshot

    return user_env_snapshot(_real_winreg())


@pytest.fixture(autouse=True)
def _real_user_env_untouched():
    """Fail the test that changes the developer's REAL Windows user environment
    (HKCU\\Environment, what `setx` writes), and put it back. On 2026-10-10 a
    test that simulated macOS reached the Windows branch, ran a real `setx`, and
    left M3_EMBED_GGUF=/path/to/bge-m3.gguf in every process started after it.
    POSIX shell rc files are redirected by _isolated_shell_rc."""
    before = _user_env_snapshot()
    yield
    if before is None:
        return
    after = _user_env_snapshot()
    if after == before:
        return
    winreg = _real_winreg()  # never a test's fake

    changed = sorted(n for n in before.keys() | after.keys() if before.get(n) != after.get(n))
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        for name in changed:
            if name in before:
                winreg.SetValueEx(key, name, 0, before[name][1], before[name][0])
            else:
                winreg.DeleteValue(key, name)
    pytest.fail(
        f"observed: this test changed the real Windows user environment: {changed} "
        "(restored). inspect: stub whatever ran setx or wrote HKCU\\Environment in the "
        "test, or simulate the OS with conftest.simulate_os",
        pytrace=False,
    )


@pytest.fixture(autouse=True)
def _real_service_units_untouched():
    """Fail the test that creates, rewrites or deletes a REAL m3 service unit.

    Hazard: installers write ~/.config/systemd/user/*.service and
    ~/Library/LaunchAgents/*.plist through os.path.expanduser, so a test that
    forces Linux or Darwin without redirecting it rewrites -- and may load -- the
    developer's live units.

    Per test, so the failure names the test that did it. A real m3 install on the
    same machine rewriting its own units mid-run would also trip this; rerun.
    """
    before = _service_files_state()
    yield
    after = _service_files_state()
    if after != before:
        changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
        pytest.fail(
            "this test changed REAL service units: " + ", ".join(changed) + ". "
            "Redirect os.path.expanduser (or HOME) to tmp_path before calling an installer.",
            pytrace=False,
        )


# launchctl / systemctl stand-ins, first on PATH for the whole session. A service
# manager acts on the developer's REAL session whatever HOME a test sets: a
# `launchctl unload` of ~/Library/LaunchAgents/com.m3memory.cognitiveloop.plist
# stopped the live loop and its watchdog, and a `load` of a tmp plist registers
# that job in the real gui domain. _real_service_units_untouched cannot see this
# (no file changes). Read-only subcommands pass through; anything else is
# recorded and NOT run, so no test can stop, start or (re)load a real service.
_SVC_READ_ONLY = {
    "launchctl": "list print print-disabled getenv version help blame",
    "systemctl": "is-active is-enabled is-failed status show cat list-units list-unit-files list-timers --version",
}
_SVC_LOG_ENV = "M3_TEST_SERVICE_CALLS"


def _write_service_shims(shim_dir: Path, log: Path) -> None:
    for tool, readonly in _SVC_READ_ONLY.items():
        real = shutil.which(tool)
        script = shim_dir / tool
        script.write_text(
            "#!/bin/sh\n"
            "# test stand-in: see tests/conftest.py _service_manager_shim\n"
            'sub=""\n'
            'for a in "$@"; do case "$a" in --*) [ "$a" = "--version" ] && sub="$a" ;; '
            '*) sub="$a"; break ;; esac; done\n'
            f'case " {readonly} " in *" $sub "*) ' + (f'exec "{real}" "$@" ;; ' if real else "exit 0 ;; ") + "esac\n"
            f'echo "${{PYTEST_CURRENT_TEST:-?}} | {tool} $*" >> "{log}"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        script.chmod(0o755)


_SCHTASKS_MUTATING = {"/create", "/change", "/run", "/end", "/delete"}
# `claude mcp add/remove` writes the developer's REAL ~/.claude.json from its own
# process, whatever home a test redirects: a test registered m3_memory there with
# a pytest temp root (2026-10-15). Recorded and not run, like a service change.
_CLAUDE_MCP_MUTATING = {"add", "add-json", "remove", "add-from-claude-desktop"}


def _mutates_claude_mcp(first: str, parts: list) -> bool:
    words = [str(p).lower() for p in parts]
    return (
        first in ("claude", "claude.exe", "claude.cmd")
        and "mcp" in words
        and any(w in _CLAUDE_MCP_MUTATING for w in words)
    )


def _install_schtasks_guard(log: Path) -> "object":
    """Windows counterpart of the launchctl/systemctl shim. schtasks resolves
    from System32 before PATH, so it cannot be shadowed there; instead every
    process launch is screened, and a task-changing schtasks call is recorded
    and replaced by a no-op. Queries pass through. Installed on every OS for
    `claude mcp add/remove` (_mutates_claude_mcp). Returns the original Popen."""
    real_popen = subprocess.Popen

    class _GuardedPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args, *a, **k):
            parts = [args] if isinstance(args, (str, bytes)) else list(args or [])
            text = " ".join(str(p) for p in parts)
            first = Path(str(parts[0]).split()[0]).name.lower() if parts else ""
            if (
                first in ("schtasks", "schtasks.exe") and any(f in text.lower().split() for f in _SCHTASKS_MUTATING)
            ) or _mutates_claude_mcp(first, parts):
                with open(log, "a", encoding="utf-8") as fh:
                    fh.write(f"{_os.environ.get('PYTEST_CURRENT_TEST', '?')} | {text}\n")
                args = ["cmd", "/c", "exit", "0"]
            super().__init__(args, *a, **k)

    subprocess.Popen = _GuardedPopen  # type: ignore[misc]
    return real_popen


@pytest.fixture(scope="session", autouse=True)
def _service_manager_shim(tmp_path_factory):
    if sys.platform == "win32":
        log = tmp_path_factory.mktemp("service-manager-guard") / "calls.log"
        log.write_text("", encoding="utf-8")
        real_popen = _install_schtasks_guard(log)
        try:
            yield log
        finally:
            subprocess.Popen = real_popen  # type: ignore[misc]
        return
    shim_dir = tmp_path_factory.mktemp("service-manager-shim")
    log = shim_dir / "calls.log"
    log.write_text("", encoding="utf-8")
    _write_service_shims(shim_dir, log)
    saved = (_os.environ.get("PATH", ""), _os.environ.get(_SVC_LOG_ENV))
    _os.environ["PATH"] = f"{shim_dir}{_os.pathsep}{saved[0]}"
    _os.environ[_SVC_LOG_ENV] = str(log)
    real_popen = _install_schtasks_guard(log)  # here for `claude mcp add/remove`
    try:
        yield log
    finally:
        subprocess.Popen = real_popen  # type: ignore[misc]
        _os.environ["PATH"] = saved[0]
        if saved[1] is None:
            _os.environ.pop(_SVC_LOG_ENV, None)
        else:
            _os.environ[_SVC_LOG_ENV] = saved[1]


_REAL_SERVICE_TARGET = re.compile(
    re.escape(str(_REAL_HOME)) + r"|(?<![/\w.-])(com\.(m3memory|skynetcmd)\.[\w.-]+|m3-[\w.-]+\.(service|timer))\b"
)


@pytest.fixture(autouse=True)
def _no_real_service_manager_calls(_service_manager_shim):
    """Fail the test that tried to change a REAL m3 service (a bare m3 label, or
    a path under the real home). The shim already refused to run it; this names
    the test, so it gets a stub instead of quietly relying on the shim. Calls on
    a tmp-dir plist are not flagged: with the shim in place they reach nothing."""
    log = _service_manager_shim
    start = log.stat().st_size if log is not None else 0
    yield
    if log is None:
        return
    with open(log, encoding="utf-8") as fh:
        fh.seek(start)
        new = [ln.split(" | ", 1)[-1].strip() for ln in fh if ln.strip()]
    tmp_root = str(Path(tempfile.gettempdir()).resolve())
    if sys.platform == "win32":
        real = new  # the Task Scheduler is machine-global: any change is real
    else:
        real = [
            c
            for c in new
            if _REAL_SERVICE_TARGET.search(c.replace(tmp_root, "<tmp>")) and "<tmp>" not in c.replace(tmp_root, "<tmp>")
        ]
    if real:
        pytest.fail(
            "this test tried to change a REAL m3 service or Claude's m3 registration "
            "(refused by the test shim): " + "; ".join(real) + ". Stub the call "
            "(e.g. setup_wizard._stop_supervised_services, setup_wizard._register_claude_server).",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _restore_platform_identity():
    """Guarantee `os.name` AND `sys.platform` are restored after every test.

    Several tests simulate a different OS with
    ``monkeypatch.setattr(<mod>.os, "name", "nt")`` or
    ``monkeypatch.setattr(<mod>.sys, "platform", "linux")``. Because ``<mod>.os``
    and ``<mod>.sys`` ARE the global modules, both overrides are process-wide.

    **os.name** — ``pathlib.Path()`` chooses PosixPath vs WindowsPath from
    ``os.name`` *at construction time*, so while an ``nt`` override is live every
    ``Path(...)`` on a POSIX host becomes a ``WindowsPath``, which raises
    ``NotImplementedError``. pytest constructs one in ``repr_failure``
    (``Path(os.getcwd())``), so a leak does not fail the leaking test: it turns
    the next *unrelated* failure into a session-aborting INTERNALERROR that never
    names the culprit. CI was red on ubuntu+macos py3.11 for ~30 straight runs
    this way, green on Windows (where WindowsPath is native) and on py3.12 (where
    the guard sits elsewhere).

    **sys.platform** — the same window, found 2026-09-28 because only the
    ``os.name`` half had ever been closed. Any fixture whose teardown runs before
    monkeypatch's sees the SIMULATED platform, and code that picks a per-OS
    filename then picks the wrong one. Measured: in
    ``test_fips_integrity.py::TestSecureWolfsslDiscovery``, the two tests that set
    ``sys.platform = "linux"`` entered that file's crypto-repair finalizer with the
    override still live, so the reload looked for ``libwolfssl.so`` on a macOS host
    whose real library is ``libwolfssl.dylib``, found nothing, and — correctly,
    under ``M3_FIPS_MODE=1`` — failed closed. Two teardown ERRORs whose message
    ("no wolfSSL library was found in any TRUSTED location") described a real
    machine state that did not exist. The ``os.name`` half of those same tests was
    already harmless precisely because this fixture handled it.

    monkeypatch's own teardown restores both, but only *after* the test's report
    is built — too late. This fixture closes that window for every test at once,
    so no future ``os.name`` / ``sys.platform`` patch can reopen it. Restoring
    both here, rather than in the fixture that tripped over it, is what keeps the
    other 43 ``sys.platform`` call sites across 8 files from each needing to know.
    """
    # Restore from the SESSION constants captured at conftest import, never from
    # a per-test snapshot of the current value. A per-test `saved = os.name`
    # records whatever this test INHERITED, so if a leak ever reaches here the
    # fixture faithfully restores the polluted value and makes it permanent —
    # the guard would endorse the leak instead of healing it. Reading the
    # session value is the same rule `test_fips_integrity.py` states for env
    # vars ("read the session values captured at import, never os.environ").
    try:
        yield
    finally:
        if _os.name != _REAL_OS_NAME:
            _os.name = _REAL_OS_NAME
        if sys.platform != _REAL_SYS_PLATFORM:
            sys.platform = _REAL_SYS_PLATFORM


def _memory_namespace_problem(package: str = "memory") -> "str | None":
    """Describe why `memory.*` is unusable by later tests, or None if it is fine.

    An incoherent namespace is silent at the cause and loud elsewhere: a
    `memory` without a `backends` attribute surfaces as `AttributeError: module
    'memory' has no attribute 'backends'` in an unrelated test, and a `dialect`
    shadowed by its submodule as `TypeError: 'module' object is not callable` at
    every call site (141 across 135 tests on one lane, 96 across 89 on another).

    `memory` absent is fine -- that is the purge doing its job.

    ``package`` is a seam for the tests: churning the real ``memory.*`` modules
    to exercise this is itself the hazard (it leaks across
    tests and has crashed the Windows interpreter at exit), so they point this at
    a synthetic package instead.
    """
    mod = sys.modules.get(package)
    if mod is None:
        return None

    if getattr(mod, "backends", None) is None:
        return (
            "left `memory` in sys.modules WITHOUT a `backends` attribute. Later "
            "tests resolving `memory.backends` by name -- a dotted-path "
            "monkeypatch target, or `import memory.backends` once the submodule "
            "is cached -- raise AttributeError far from here. See "
            ".claude/rules/test-sandbox.md."
        )

    dialect = getattr(mod.backends, "dialect", None)
    if not callable(dialect):
        return (
            f"left `memory.backends.dialect` bound to {type(dialect).__name__}, "
            "not the accessor function, so every `dialect()` call site raises "
            "\"'module' object is not callable\". Do not replace or stub "
            "`sys.modules['memory.backends']`. See "
            ".claude/rules/backend-seam.md."
        )
    return None


def _purge_memory_namespace(package: str = "memory") -> None:
    roots = (package, "memory_core") if package == "memory" else (package,)
    for name in list(sys.modules):
        if name in roots or name.startswith(package + "."):
            del sys.modules[name]


def _report_namespace_breakage(entered_coherent: bool, package: str = "memory") -> None:
    """Purge a broken namespace, and fail the test only if it broke it.

    Repair first, unconditionally: leaving it broken makes every later test fail
    for a cause that is not theirs. Then attribute -- a test that INHERITED the
    breakage is not at fault and must not be failed for it.
    """
    problem = _memory_namespace_problem(package)
    if problem is None:
        return
    _purge_memory_namespace(package)
    if entered_coherent:
        raise RuntimeError("this test " + problem + " The namespace has been purged so later tests are unaffected.")


@pytest.fixture(autouse=True)
def _restore_memory_modules():
    """Heal `sys.modules` pollution of the memory.* namespace after each test.

    Several tests deliberately purge / reimport `memory_core` and `memory.*`
    (e.g. test_doctor's _isolate_env, the shim-identity test, the parity tests)
    to exercise a fresh import under a specific env. When they don't restore
    afterward, the rest of the session runs against a half-initialized or
    re-initialized module set — config globals revert to defaults
    (ELBOW_MIN_INPUT 5->20), monkeypatches target a stale `config`, and
    `import memory_core` returns a broken cached module. That surfaced as
    order-dependent CI reds that pass in isolation: test_oxidation_probe,
    test_elbow_trim, test_entity_coalesce, test_memory_core_parity, etc.

    Snapshot the memory-namespace modules before the test; after it, drop any
    that were added/replaced and restore the originals. Tests that intentionally
    reimport still see their fresh module DURING the test; the next test starts
    from the original, fully-initialized objects.
    """

    def _snapshot():
        return {
            name: mod
            for name, mod in sys.modules.items()
            if name == "memory_core" or name == "memory" or name.startswith("memory.")
        }

    before = _snapshot()
    # Whether the namespace was already unusable when this test STARTED. Without
    # this, a single breakage is blamed on every test that follows it -- measured
    # as 2819 teardown errors from one cause.
    _entered_coherent = _memory_namespace_problem() is None
    yield
    after = {
        name: sys.modules[name]
        for name in list(sys.modules)
        if name == "memory_core" or name == "memory" or name.startswith("memory.")
    }
    # Detect whether the test REPLACED any memory.* module object (not just
    # added one). A replacement means some submodule was reimported and may now
    # be bound, via `from .x import y`, to a DIFFERENT sibling than the one we'd
    # restore — leaving a divergent mix where e.g. memory.entity's cached
    # `_embed_canonical_cached` reads a different memory.embed cache than a test
    # patches. That cross-module identity divergence is what made
    # test_entity_graph::test_resolution_cosine fail only under batch ordering
    # (the stub _embed was patched on one memory.embed instance but the
    # resolution path called another). When a replacement happened, restoring
    # the old objects can't guarantee internal consistency — so PURGE the whole
    # memory.* namespace instead and let the next test reimport a clean,
    # consistently cross-bound set. When nothing was replaced (the common case),
    # keep the cheap restore.
    #
    # This heals CROSS-test leakage on teardown, but it is a safety net, not a
    # license: DO NOT monkeypatch.setitem(sys.modules, "memory.backends", <fake>)
    # in a test. memory.backends re-exports a `dialect` CALLABLE and also has a
    # `dialect` SUBMODULE (same name) -- replacing the package object breaks that
    # resolution so `from memory.backends import dialect; dialect()` binds the
    # MODULE and raises "module object is not callable" in tests that run before
    # this teardown fires (bit test_wiki_determinism, 2026-07-24). Use the REAL
    # dialect() factory (defaults to SqliteDialect with no active backend) against
    # an in-memory DB instead. See m3 memory
    # "test-hazard-monkeypatch-memory.backends-dialect-poisons-suite".
    replaced = any(name in before and before[name] is not mod for name, mod in after.items())
    if replaced:
        for name in list(sys.modules):
            if name == "memory_core" or name == "memory" or name.startswith("memory."):
                del sys.modules[name]
        _report_namespace_breakage(_entered_coherent)
        return
    # No replacement: drop only what the test ADDED, restore the originals.
    for name in set(after) - set(before):
        del sys.modules[name]
    for name, mod in before.items():
        sys.modules[name] = mod
    _report_namespace_breakage(_entered_coherent)


@pytest.fixture(autouse=True)
def _guard_thread_leaks():
    """Fail fast when a test leaks a live worker thread into the session.

    A test that spawns a background thread which never exits (e.g. a simulated
    wedged native init, `threading.Event().wait()` with no release) leaves that
    thread alive for the rest of the run. Beyond hiding a real bug, a lingering
    NATIVE-touching thread can race a later test that reloads a C-extension
    module and crash the whole run (that is exactly what happened with the
    m3-embed-init thread and the crypto_provider reload, #85).

    Guard: snapshot the live threads before the test; after it, any NEW thread
    still alive after a short grace is a leak. Threads whose name matches a
    known worker prefix (they should be joined/stopped by their test) FAIL the
    test; anything else is surfaced as a warning so genuinely long-lived helpers
    don't turn into false reds. Give stragglers a brief join first — a thread
    mid-shutdown at yield time is not a leak.
    """
    import threading
    import warnings

    # Worker-thread name prefixes that a well-behaved test must not leave running.
    _LEAK_FAIL_PREFIXES = ("m3-embed-init",)

    before = {t.ident for t in threading.enumerate()}
    yield
    current = threading.current_thread()

    def _new_live():
        return [t for t in threading.enumerate() if t.ident not in before and t is not current and t.is_alive()]

    # A leaked thread may be a few ms from exiting; give it a short window.
    for t in _new_live():
        t.join(timeout=2.0)
    leaked = _new_live()
    if not leaked:
        return
    fail = [t for t in leaked if t.name.startswith(_LEAK_FAIL_PREFIXES)]
    if fail:
        names = ", ".join(sorted(t.name for t in fail))
        raise AssertionError(
            f"test leaked live worker thread(s): {names}. Join/stop them in "
            "teardown (an unreleased native-init thread can crash a later "
            "module reload — see #85)."
        )
    # Non-worker stragglers: surface but don't fail (may be a legit helper).
    warnings.warn(
        "test left non-main thread(s) alive: " + ", ".join(sorted(t.name for t in leaked)),
        stacklevel=2,
    )


@pytest.fixture(autouse=True)
def _close_db_pools():
    """Close every cached M3Context SQLite pool after each test.

    M3Context.for_db() caches contexts (each owning a 5-connection pool) in a
    16-entry LRU and NEVER closes them between tests — so pooled connections to
    every test's tmp DB accumulate, holding open file handles + WAL locks. Under
    CI's slower I/O that cross-test contention surfaces as OperationalError:
    database is locked (m3_sdk.py:494) in test_entity_* / resolution-tuning,
    which passed locally only because fast local I/O dodged the race. WAL mode
    alone wasn't enough — the leaked pooled connections are the real cause.

    m3_sdk._cleanup() closes all pools and clears the cache (it's the same
    routine registered atexit). Run it as teardown so each test starts with no
    inherited open connections. Best-effort: a missing/funny m3_sdk must not
    break unrelated tests.
    """
    yield
    try:
        import m3_sdk

        m3_sdk._cleanup()
    except Exception:  # noqa: BLE001 — teardown hygiene must never fail a test
        pass


@pytest.fixture(autouse=True)
def _reset_storage_backend_cache():
    """Reset the storage-backend selector cache around every test.

    `memory.backends.selector` caches the resolved StorageBackend per name in a
    process-global dict (so pools/capability probes aren't rebuilt every call).
    A test that resolves `active_backend()` under `M3_DB_BACKEND=postgres` (the
    seam unit tests, and every live-PG test) leaves the postgres backend cached;
    a LATER test on a different file that expects the sqlite default then gets the
    stale cached postgres backend and fails — an order-dependent cross-file leak.

    This resets the CACHE only. The backend-selecting ENV (M3_DB_BACKEND, PG
    URLs, CDW target) is cleared by the `m3_sandbox` fixture, which owns all
    environment isolation in one place. Best-effort: the selector must import,
    but a funny state must never break an unrelated test.
    """

    def _clear():
        # IMPORTANT: only clear if the selector is ALREADY imported — do NOT
        # trigger the import here. Importing memory.backends eagerly pulls the
        # whole memory.* package (memory/__init__ imports all submodules incl.
        # memory.embed in the host's shared-embedder mode). Doing that during
        # this autouse fixture's setup polluted the module snapshot that
        # _restore_memory_modules takes, so test_doctor's tier-1 probe read a
        # shared-mode config and reported 'shared-mode' instead of
        # 'not-configured' (regression from the test-isolation commit). Guarding
        # on sys.modules keeps this fixture a no-op for tests that never touch the
        # backend seam.
        _sel = sys.modules.get("memory.backends.selector")
        if _sel is not None:
            try:
                _sel._reset_for_tests()
            except Exception:  # noqa: BLE001 — hygiene must never fail a test
                pass
        # The process's named store (a CLI's --store, set while parsing) is a
        # module global; a test that ran a main() in-process must not leave it
        # selected for the next test.
        _paths = sys.modules.get("m3_core.paths")
        if _paths is not None:
            _paths._process_store = None
        # memory.config serves registry-backed names through __getattr__.
        # monkeypatch's undo re-binds the value it read as a REAL
        # attribute, which would shadow the accessor with a stale value for
        # every later test; drop any such binding.
        _mcfg = sys.modules.get("memory.config")
        if _mcfg is not None:
            for _name in getattr(_mcfg, "SETTING_NAMES", ()):
                _mcfg.__dict__.pop(_name, None)

    _clear()
    yield
    _clear()


_EMBED_PRISTINE: dict = {}


def _snapshot_embed_globals(mod) -> None:
    """Capture the import-time shared-mode switches, once, BEFORE any test can
    mutate them. Called from a session-scoped autouse fixture as well as from the
    per-test restore, so the values are pristine even if the very first test to
    touch memory.embed is one that flips shared mode on."""
    if mod is None:
        return
    if "_INPROC_ALLOWED" not in _EMBED_PRISTINE and hasattr(mod, "_INPROC_ALLOWED"):
        _EMBED_PRISTINE["_INPROC_ALLOWED"] = mod._INPROC_ALLOWED
    if "_EMBED_CFG" not in _EMBED_PRISTINE and hasattr(mod, "_EMBED_CFG"):
        try:
            _EMBED_PRISTINE["_EMBED_CFG"] = dict(mod._EMBED_CFG)
        except (TypeError, ValueError):
            _EMBED_PRISTINE["_EMBED_CFG"] = {}


@pytest.fixture(scope="session", autouse=True)
def _no_console_windows_during_tests():
    """Stop test subprocesses flashing console windows on Windows.

    ⚠ REPORTED BY THE USER, 2026-09-19: "when you're doing testing I have random
    python windows appearing". 42 test files spawn subprocesses and most pass no
    creationflags, so each one allocates a console -- a window that steals focus
    on someone's desktop while a suite runs for ten minutes.

    Patched at ``Popen.__init__`` rather than on ``subprocess.run`` because run,
    check_output, check_call and call all funnel through Popen: one seam instead
    of four, and it also covers the many call sites that construct Popen
    directly (§10a -- the convergence point, not the call sites).

    ⚠ ADDS ONLY, NEVER OVERRIDES. A test that passes its own creationflags is
    making a deliberate statement -- test_installer_spawns_are_hidden and
    test_python_exe_windowless both assert on exactly these flags -- so this
    ORs into whatever the caller asked for and leaves an explicit choice intact.
    DETACHED_PROCESS is deliberately NOT added: it would reparent the child and
    break the output capture every one of these tests depends on.

    ⚠ WHY A PROBE FROM AN AUTOMATED RUN DOES NOT SHOW THE BUG. A child inherits
    its parent's console rather than allocating a new one, so when pytest itself
    is launched WITHOUT a visible console (a CI runner, an agent's captured
    shell) the child has no window either and GetConsoleWindow() returns 0 with
    or without this fixture. Measured 2026-09-19: identical result with the
    fixture disabled. The flash only appears when a human runs the suite from an
    interactive session -- which is exactly the case being fixed, and the one a
    captured-output check cannot reproduce. Verify by running a suite by hand,
    not by asserting on a spawned child's console handle.

    No-op off Windows, where the flag does not exist and no console is allocated.
    """
    if _os.name != "nt":
        yield
        return

    import subprocess as _sp

    flag = getattr(_sp, "CREATE_NO_WINDOW", 0)
    if not flag:
        yield
        return

    _orig_init = _sp.Popen.__init__

    def _quiet_init(self, *args, **kwargs):
        # Positional creationflags would be argument 13; nothing in this repo
        # passes it that way, and honouring the keyword form covers every call
        # site. A caller's own flags are preserved, not replaced.
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | flag
        return _orig_init(self, *args, **kwargs)

    _sp.Popen.__init__ = _quiet_init
    try:
        yield
    finally:
        _sp.Popen.__init__ = _orig_init


@pytest.fixture(scope="session", autouse=True)
def _capture_pristine_embed_globals():
    """Import memory.embed once at session start and snapshot its shared-mode
    switches, so the restore below always has genuinely-pristine values."""
    try:
        _m = importlib.import_module("memory.embed")

        _snapshot_embed_globals(_m)
    except Exception:  # noqa: BLE001 — best effort; module may not be importable
        pass
    yield


def _restore_embed_globals(mod) -> None:
    """Restore memory.embed's shared-mode switches to their FIRST-IMPORT values.

    `_INPROC_ALLOWED` is derived at import time from whether a config file was
    found and whether it disables the in-process embedder, so there is no
    hardcodable "default" — the correct value is whatever this environment
    computed before any test mutated it. Snapshot on first sight, restore after
    every test.
    """
    _snapshot_embed_globals(mod)
    if "_INPROC_ALLOWED" in _EMBED_PRISTINE:
        mod._INPROC_ALLOWED = _EMBED_PRISTINE["_INPROC_ALLOWED"]
    if "_EMBED_CFG" in _EMBED_PRISTINE and hasattr(mod, "_EMBED_CFG"):
        try:
            mod._EMBED_CFG.clear()
            mod._EMBED_CFG.update(_EMBED_PRISTINE["_EMBED_CFG"])
        except (AttributeError, TypeError):
            mod._EMBED_CFG = dict(_EMBED_PRISTINE["_EMBED_CFG"])


@pytest.fixture(autouse=True)
def _reset_embed_global_cache():
    """Reset memory.embed's in-process embedder cache between tests.

    `memory.embed` memoizes the in-process EmbeddedEmbedder in module-level
    globals (`_embedded_embedder`, `_embedded_embed_checked`) and only probes
    once per process (the `_embedded_embed_checked` guard). On a host with the
    native m3_core_rs wheel installed AND a discoverable GGUF, the FIRST test
    that exercises an embed warms that cache — and `_restore_memory_modules`
    above restores the module IDENTITY but NOT its mutated internal globals. So
    every later test inherits a live tier-1 embedder, which silently satisfies
    embeds before the HTTP cascade is reached. That made tier-ordering / dedup /
    cold-cascade tests (test_embed_cascade_order, test_vector_kind_strategy,
    test_doctor SLOs) pass in isolation but fail under batch ordering on dev
    boxes with the wheel installed.

    Clearing the cache to its pristine "not yet checked" state after each test
    means the next test re-probes under ITS OWN env (e.g. M3_CORE_RS_DISABLE /
    M3_EMBED_GGUF), so tier-1 presence is decided per-test, not leaked.
    Best-effort: if memory.embed isn't imported / lacks the globals, do nothing.
    """
    yield
    mod = sys.modules.get("memory.embed")
    if mod is not None:
        if hasattr(mod, "_embedded_embedder"):
            mod._embedded_embedder = None
        if hasattr(mod, "_embedded_embed_checked"):
            mod._embedded_embed_checked = False
        # The entity-name embed cache memoizes name->vector across calls. A
        # sibling test that resolves entities leaves it populated; a later test
        # that stubs _embed and asserts on embed_calls then sees ZERO calls
        # because the resolution path served the name from this cache instead
        # (test_entity_graph::test_resolution_cosine_stores...). Clear it too.
        cache = getattr(mod, "_ENTITY_NAME_EMBED_CACHE", None)
        if cache is not None:
            try:
                cache.clear()
            except (AttributeError, TypeError):
                pass
        # The shared-embedder switches are ALSO module globals, read straight by
        # memory.doctor to decide tier-1's verdict ("shared-mode" vs
        # "not-configured"). A sibling test that exercises shared mode leaves
        # _INPROC_ALLOWED=False / _EMBED_CFG={"disable_inproc_embedder": True}
        # behind, and the next test to call memory_doctor_impl inherits that
        # verdict — test_doctor::test_tier1_not_configured_when_no_gguf passed
        # alone and failed under batch ordering for exactly this reason.
        #
        # Restore the values captured at FIRST import, not a hardcoded default:
        # _INPROC_ALLOWED is computed from the config file's presence and
        # contents (see memory/embed.py), so pinning it to True here would
        # invent a state this environment never had.
        _restore_embed_globals(mod)


# Environment variables that must NOT leak from the developer's shell into a
# test. Grouped by concern so the single source of truth is legible. A test that
# genuinely needs one sets it via monkeypatch, which runs AFTER this autouse
# fixture and therefore wins.
_SANDBOX_CLEAR_ENV = (
    # Backend selector — a leaked M3_DB_BACKEND=postgres makes sqlite-default
    # tests resolve the wrong backend.
    "M3_DB_BACKEND",
    "DB_BACKEND",
    # A developer's exported M3_DATABASE would point a test at a real store; tests
    # use pin_database (M3_DATABASE design phase 2). The chatlog path names are NOT
    # cleared here: the repo-root conftest pins them to the test's tmp dir first,
    # and clearing them silently unified every test's chatlog with its main store.
    "M3_DATABASE",
    # Primary-store DSNs — an ambient dev DSN to a throwaway cluster leaks into
    # tests that read PG_URL (higher-precedence M3_PG_URL overrides an injected
    # PG_URL otherwise).
    "M3_PG_URL",
    "PG_URL",
    # CDW / warehouse-sync target — a real M3_SYNC_TARGET_IP=10.x / M3_CDW_PG_URL
    # overrides a test's injected target via getenv_compat precedence (that made
    # test_backup_reminder assert on 192.0.2.50 but read the real host).
    "M3_SYNC_TARGET_IP",
    "SYNC_TARGET_IP",
    "M3_CDW_PG_URL",
    "M3_CDW_URL",
    "M3_POSTGRES_SERVER",
    "POSTGRES_SERVER",
    # LLM endpoint switches — a developer's exported M3_LLM_URL or
    # M3_ENABLE_OLLAMA_FAILOVER outranks the test's own .llm_config.json.
    "M3_LLM_URL",
    "M3_LLM_ENDPOINTS_CSV",
    "LLM_ENDPOINTS_CSV",
    "M3_ENABLE_LMSTUDIO_FAILOVER",
    "M3_ENABLE_OLLAMA_FAILOVER",
)


@pytest.fixture(autouse=True)
def m3_sandbox(monkeypatch, tmp_path):
    """The single source of truth for a hermetic test environment.

    Every test runs inside a filesystem + environment sandbox so it neither
    reads the developer's real config/warehouse nor writes into their real
    engine/backup dirs. This ONE fixture replaces the previously scattered
    env-isolation (a dedicated engine-root fixture + the env half of the
    backend-cache fixture + ~130 lines of per-file `monkeypatch.setenv` for
    M3_*_ROOT / PG_URL / M3_DATABASE). Consolidating it here is what closes the
    "gap between fixtures" leak class — the backup-dir leak and the CDW-env leak
    both existed because isolation was piecemeal and each covered only part of
    the surface.

    Two halves, both backend-agnostic (SQLite / PostgreSQL / future backends):

    1. **Roots → tmp.** Pin all three M3 roots to per-test tmp dirs at their
       HIGHEST-precedence env vars:
         * ``M3_ENGINE_ROOT``  — DBs, engine state, and (crucially) the migrator's
           ``get_m3_engine_root()/backups`` pre-upgrade backups. A test that ran a
           migration previously leaked ``*.pre-up.*.db`` into the real backup dir
           (GBs of ``test_agent_chatlog`` backups over time).
         * ``M3_CONFIG_ROOT``  — load-bearing for the SUBPROCESS leak path:
           ``memory.db._ensure_sync_tables`` shells out to ``migrate_memory.py``
           with ``os.environ.copy()``; that subprocess's ``prompt_backup_dir``
           prefers the SAVED ``backup_dir`` in ``.migrate_config.json``. On a dev
           box that saved value points at the real dir, and a bare
           ``M3_MEMORY_ROOT`` does not override an already-exported real
           ``M3_CONFIG_ROOT`` — so only pinning the config root at top precedence
           makes both in-process and subprocess read a clean, empty config.
         * ``M3_MEMORY_ROOT``  — the master root others derive from when unset.
       Also re-points ``migrate_memory``'s import-time module constants
       (``_M3_ENGINE_ROOT`` / ``CONFIG_PATH``) if it's already imported, so a
       pre-imported module doesn't retain real-root values.
       tmp_path is unique per test and reaped by pytest, so anything written under
       it is cleaned up automatically — tests clean up after themselves.

    2. **Leaky env → cleared.** Remove the backend / PG / CDW-sync vars listed in
       ``_SANDBOX_CLEAR_ENV`` so a developer running the suite with a real
       warehouse exported can't have that host bleed into assertions.
    """
    # 1. Roots → tmp (highest-precedence env vars). Logs too: the session-level
    # pin at the top of this file would otherwise outrank M3_MEMORY_ROOT/logs.
    for var, path in (
        ("M3_ENGINE_ROOT", tmp_path / "engine"),
        ("M3_CONFIG_ROOT", tmp_path / "config"),
        ("M3_LOGS_ROOT", tmp_path / "logs"),
        ("M3_MEMORY_ROOT", tmp_path),
    ):
        monkeypatch.setenv(var, str(path))
    # The config snapshot is cached per PROCESS (m3_core.config.settings), so
    # the roots above would not move a snapshot an earlier test built: it would
    # keep that test's config root, and the secrets file derived from it.
    from m3_core.config import settings as _cfg_settings

    _cfg_settings._reset_for_tests()
    # The FILES store, too. memory.config.FILES_DB_PATH (re-exported as
    # files_memory.config.FILES_DB_PATH) is resolved AT IMPORT, so once either
    # module is imported the roots above do not move it, and files tools that
    # fall through to it read the developer's real files_database.db: measured
    # 2026-10-08, a files_stats test returned the live store's 20 files and
    # 218 leaves. files_memory.db._resolve_path reads M3_FILES_DB_PATH per call;
    # readers of the constant get the re-pointed attribute.
    _files_db = str(tmp_path / "engine" / "files_database.db")
    monkeypatch.setenv("M3_FILES_DB_PATH", _files_db)
    for _mod_name in ("memory.config", "files_memory.config"):
        _mod = sys.modules.get(_mod_name)
        if _mod is not None and hasattr(_mod, "FILES_DB_PATH"):
            monkeypatch.setattr(_mod, "FILES_DB_PATH", _files_db)
    # Pinning M3_MEMORY_ROOT to tmp also blinds discovery of SHIPPED read-only
    # payload that derives from <M3_MEMORY_ROOT>/config/ — notably the SLM profiles
    # (bin/slm_intent._profile_search_dirs → <root>/config/slm). Those are part of
    # the repo, not per-test state, so point M3_SLM_PROFILES_DIR at the real repo
    # profiles dir; tests that manage their own profiles (test_slm_intent) set this
    # var themselves and override us (monkeypatch runs after this autouse fixture).
    _repo_slm = Path(__file__).resolve().parent.parent / "config" / "slm"
    if _repo_slm.is_dir():
        monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(_repo_slm))
    # Same blinding problem, second instance: crypto_provider._m3_lib_dir()
    # derives the TRUSTED wolfSSL search path from M3_CONFIG_ROOT's parent, so
    # pinning the root above relocates it to <tmp>/lib — which never contains
    # the library. Harmless when FIPS is off (it falls back to the DEFAULT
    # backend), but a developer with M3_FIPS_MODE=1 exported gets a FAIL-CLOSED
    # RuntimeError in every CLI subprocess and the suite fails for an
    # environmental reason with no relation to the code under test.
    # The library is installed payload, not per-test state — pin it explicitly.
    #
    # Pin the DIRECTORY (M3_LIB_DIR), never the file (M3_WOLFSSL_LIB): the
    # latter is used verbatim, so it would inject a host-specific filename
    # (a Windows .dll) into the candidate list and break the platform-
    # simulation tests in test_fips_integrity.py that assert .so-only under a
    # monkeypatched posix/linux. M3_LIB_DIR keeps per-OS filename selection in
    # crypto_provider where it belongs.
    _real_lib = Path(_os.path.expanduser("~")) / ".m3" / "lib"
    if _real_lib.is_dir() and not _os.environ.get("M3_LIB_DIR"):
        monkeypatch.setenv("M3_LIB_DIR", str(_real_lib))
    _mm = sys.modules.get("migrate_memory")
    if _mm is not None:
        if hasattr(_mm, "_M3_ENGINE_ROOT"):
            monkeypatch.setattr(_mm, "_M3_ENGINE_ROOT", str(tmp_path / "engine"))
        if hasattr(_mm, "CONFIG_PATH"):
            monkeypatch.setattr(_mm, "CONFIG_PATH", str(tmp_path / "config" / ".migrate_config.json"))
    # chatlog_config.CONFIG_PATH is the same hazard as migrate_memory's above, and
    # it bites harder: the constant is resolved AT IMPORT (chatlog_config.py:65),
    # so once the module is imported before this fixture runs, `save_config()`
    # rewrites the DEVELOPER'S LIVE ~/.m3/config/.chatlog_config.json. Measured
    # 2026-09-28: a test that enabled redaction through save_config persisted its
    # tmp `db_path` into the real config, so `m3 chat status` afterwards reported
    # the live chatlog as 0 rows and CAPTURE WAS AIMED AT A PYTEST TMPDIR that
    # pytest later reaps — silent turn loss on the developer's own machine, from a
    # green test run. Pin it for every test, not just the ones that opt into
    # `chatlog_env`; this is precisely the "gap between fixtures" class this
    # sandbox exists to close.
    _cc = sys.modules.get("chatlog_config")
    if _cc is not None and hasattr(_cc, "CONFIG_PATH"):
        monkeypatch.setattr(_cc, "CONFIG_PATH", str(tmp_path / "config" / ".chatlog_config.json"))
        # Its resolved config is cached per process: without this, a test reads the
        # db_path the PREVIOUS test's sandbox file named (a reaped tmp dir).
        _cc.invalidate_cache()

    # 2. Leaky env → cleared. Every m3 secret's env names too (the registry is
    # the list): m3 no longer reads them, but a developer's real key in the test
    # process would reach every child a test spawns and every env sweep it runs.
    for _var in (*_SANDBOX_CLEAR_ENV, *_registry_secret_names()):
        monkeypatch.delenv(_var, raising=False)
    yield


@pytest.fixture(autouse=True)
def _read_log_drained():
    """Flush the read-access log at THIS test's teardown, while its sandbox is
    still in place (declared after m3_sandbox, so it tears down first). A queue
    left for the background flusher was written during a LATER test: opening
    the store reads settings, which rebuilt that test's snapshot between the
    sandbox's reset and its setenv (a stale read, seen once on the Mac lane,
    2026-10-12). What cannot be written (the test's database is gone) is
    discarded here, never carried into the next test."""
    yield
    drain_read_log()


def drain_read_log() -> None:
    """Write the queued reads now, and discard what cannot be written."""
    db = sys.modules.get("memory.db")
    if db is None or not getattr(db, "_read_log_pending", None):
        return
    try:
        db.flush_read_log()
    except Exception:  # noqa: BLE001 -- a test-only drain; the store may be gone
        pass
    with db._read_log_lock:
        db._read_log_pending.clear()


def gdpr_erase(**target) -> dict:
    """A GDPR erasure the way an operator runs it: the plan, then the confirmation
    with its token and a compliance record. ``target`` is ``user_id=`` or
    ``memory_ids=``. The one way a test erases: gdpr_forget is the only hard delete."""
    import importlib

    mm = importlib.import_module("memory_maintenance")
    plan = mm.gdpr_forget_impl(**target)
    assert isinstance(plan, dict) and plan["step"] == "plan" and not plan["erased"], plan
    done = mm.gdpr_forget_impl(**target, confirm=plan["token"], compliance=dict.fromkeys(plan["requires"], "test"))
    assert isinstance(done, dict) and done["erased"], done
    return done


# Minimal memory_items schema sufficient for chatlog writes. Embeddings and
# FTS5 tables are created lazily by specific fixtures that need them.
#
# Kept as a small chatlog-side helper. Main-DB tests should use
# `create_full_main_schema` (post-v031 canonical schema, built from the
# session-scoped template DB).
_MEMORY_ITEMS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS memory_items (
        id TEXT PRIMARY KEY,
        type TEXT,
        title TEXT,
        content TEXT,
        metadata_json TEXT,
        agent_id TEXT,
        model_id TEXT,
        change_agent TEXT,
        importance REAL,
        source TEXT,
        origin_device TEXT,
        user_id TEXT,
        scope TEXT,
        expires_at TEXT,
        created_at TEXT,
        valid_from TEXT,
        valid_to TEXT,
        conversation_id TEXT,
        refresh_on TEXT,
        refresh_reason TEXT,
        content_hash TEXT,
        variant TEXT
    );
"""


def isolate_chatlog_env(monkeypatch, tmp_path):
    """Route every chatlog-subsystem side effect into tmp_path.

    This centralises the three-layer isolation that chatlog fixtures need:

    1. **Config paths** — monkeypatch the module-level path constants so tools
       that read them directly (status line, init, migrate) see tmp paths.
    2. **CHATLOG_DB_PATH env var** — the dataclass default `db_path` captured
       `DEFAULT_DB_PATH` at class-definition time, so patching the constant
       alone does *not* steer new configs. The env-var override is the only
       reliable knob for `resolve_config()`.
    3. **Module globals** — `chatlog_config._POOL` / `_POOL_DB_PATH` and
       `chatlog_core._QUEUE` / `_FLUSH_TASK` are kept alive across tests by
       the import cache. Without explicit teardown, a stale pool pointing at
       the real DB (or a queue from a prior test's flush) will leak writes.

    Returns a dict of the tmp paths for tests that need to open the DB or
    inspect spill files directly.
    """
    import chatlog_config
    import chatlog_core

    db_path = tmp_path / "agent_chatlog.db"
    main_db_path = tmp_path / "agent_memory.db"
    state_file = tmp_path / ".chatlog_state.json"
    spill_dir = tmp_path / "chatlog_spill"

    monkeypatch.setattr(chatlog_config, "DEFAULT_DB_PATH", str(db_path))
    monkeypatch.setattr(chatlog_config, "MAIN_DB_PATH", str(main_db_path))
    monkeypatch.setattr(chatlog_config, "STATE_FILE", str(state_file))
    monkeypatch.setattr(chatlog_config, "SPILL_DIR", str(spill_dir))
    # CHATLOG_MODE is deprecated / ignored. Scope chatlog + main to tmp so a
    # test run never touches the live store.
    monkeypatch.delenv("CHATLOG_MODE", raising=False)
    # The --chatlog-db / --database pins, not env (both cleared after the test).
    from m3_core.paths import pin_chatlog_db, pin_database

    pin_chatlog_db(str(db_path))
    pin_database(str(main_db_path))
    chatlog_config.invalidate_cache()

    monkeypatch.setattr(chatlog_config, "_POOL", None)
    monkeypatch.setattr(chatlog_config, "_POOL_DB_PATH", None)
    monkeypatch.setattr(chatlog_core, "_QUEUE", None)
    monkeypatch.setattr(chatlog_core, "_FLUSH_TASK", None)

    return {
        "db_path": db_path,
        "main_db_path": main_db_path,
        "state_file": state_file,
        "spill_dir": spill_dir,
    }


def create_memory_items_schema(db_path) -> None:
    """Create the minimal memory_items table. Safe to call on an existing DB.

    For full main-DB schema use `create_full_main_schema` instead.
    """
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_MEMORY_ITEMS_SCHEMA)
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# Full main-DB schema fixture — pinned to current migrations
# ──────────────────────────────────────────────────────────────────────────────
# Tests that need the post-v031 canonical schema (memory_items + 30+ feature
# tables, entity graph, fact_enrichment_queue, observation_queue, etc.) should
# call `create_full_main_schema(db_path)` instead of inlining tiny synthetic
# schemas. This way, when migrations evolve (v032, v033, ...) the test fixtures
# track automatically — no manual schema duplication.
#
# Implementation: a session-scoped template DB is built ONCE per pytest run by
# invoking `bin/migrate_memory.py --database DB up --yes --target main` on a fresh DB.
# Per-test copies are bytewise file-copies — fast (~ms) instead of paying the
# subprocess + 31-migration cost per test.

_TEMPLATE_DB_PATH: Path | None = None


def _build_template_db() -> Path:
    """Run all main migrations against a fresh DB and return its path.

    Cached at module level — built once per pytest session. The template
    is created in tempdir (so the session's tearDown cleans it up).
    """
    tmp_root = Path(tempfile.mkdtemp(prefix="m3_test_template_"))
    template_db = tmp_root / "template.db"

    repo_root = Path(__file__).resolve().parent.parent
    migrate_script = repo_root / "bin" / "migrate_memory.py"

    env = _os.environ.copy()
    # The migrator's backup step writes to get_m3_engine_root()/backups. It does
    # NOT honor M3_BACKUP_DIR (prompt_backup_dir reads only the engine root + the
    # saved config value), so the previous M3_BACKUP_DIR override here was a
    # no-op and the template build leaked its backup to the real dir. Pin the
    # ENGINE ROOT (the knob that IS honored) into tmp so the backup lands here.
    env["M3_ENGINE_ROOT"] = str(tmp_root / "engine")
    env["M3_MEMORY_ROOT"] = str(tmp_root)

    result = subprocess.run(
        [sys.executable, str(migrate_script), "--database", str(template_db), "up", "--yes", "--target", "main"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"failed to build test template DB:\nstdout={result.stdout}\nstderr={result.stderr}")
    if not template_db.is_file():
        raise RuntimeError(f"template DB not created at {template_db}")
    return template_db


def _get_template_db() -> Path:
    global _TEMPLATE_DB_PATH
    if _TEMPLATE_DB_PATH is None or not _TEMPLATE_DB_PATH.is_file():
        _TEMPLATE_DB_PATH = _build_template_db()
    return _TEMPLATE_DB_PATH


def orthogonal_vectors(names, dim: int = 64) -> dict[str, list[float]]:
    """One-hot vectors for `names`, a DIFFERENT axis each, so no two resolve to
    each other by cosine. A hash-mod-dim index collides (64 axes, three names:
    ~5% of random names), and the colliding entity is then correctly merged —
    a test failure that is not a product failure."""
    names = list(dict.fromkeys(names))
    if len(names) > dim:
        raise ValueError(f"{len(names)} names need more than {dim} axes")
    return {n: [1.0 if k == i else 0.0 for k in range(dim)] for i, n in enumerate(names)}


def create_full_main_schema(db_path) -> None:
    """Create a fresh main-DB-schema at `db_path` (post-v031 canonical).

    Implementation: copies a session-cached template DB built by running
    all migrations once. Fast (~ms per test) and always in sync with the
    current migration files — no schema duplication to maintain.

    Use this in place of inlined `CREATE TABLE memory_items (...)` blocks
    in tests that exercise main-DB code paths.
    """
    template = _get_template_db()
    shutil.copyfile(str(template), str(db_path))
    # Put the copy in WAL mode (production default) so the m3_sdk pool's 5
    # connections and any raw test connection coexist without an exclusive-lock
    # fight. Without this, under CI's slower I/O the pool's WAL-mode switch
    # contends with a test reader and fails with "database is locked"
    # (m3_sdk.py:494) — the cause of flaky test_entity_* / resolution-tuning reds.
    _wal = sqlite3.connect(str(db_path), timeout=30)
    try:
        _wal.execute("PRAGMA journal_mode=WAL")
        _wal.commit()
    finally:
        _wal.close()


@pytest.fixture(scope="session")
def main_db_template() -> Path:
    """Session-scoped: returns the path to a fresh post-v031 main DB.

    Tests that copy this directly should call `shutil.copyfile(template, dst)`.
    Most callers should use `create_full_main_schema(db_path)` instead, which
    handles the copy and is the public API.
    """
    return _get_template_db()


# ── schema-correct row seeding (raw-SQL primitive tests) ─────────────────────
#
# WHY: many tests hand-write `INSERT INTO memory_items(id, title, content) ...`
# with an ad-hoc column list. Against a lenient minimal fixture schema that
# passes; against the REAL schema (SQLite ensure_schema / live PostgreSQL) it
# fails a NOT NULL constraint — `type` on memory_items and the `id` PK on
# memory_embeddings are required. That drift bit test_backend_conformance and
# test_postgres_backend_live on PG (2026-07-24). These helpers are the ONE place
# that knows the required columns, so a raw-SQL primitive test (one that must
# stay below the seam to exercise keyword/vector/CAS native paths) can seed a
# schema-VALID row on either backend without re-listing columns and drifting.
#
# NOTE: this is for tests that legitimately bypass the seam. A test that just
# needs a row to EXIST (not exercise a primitive) should write through the seam
# (`memory_write_impl`) instead — that path supplies every column itself and is
# what production uses.


def seed_memory_row(
    conn,
    dialect,
    *,
    mid,
    type="note",
    title="",
    content="",
    importance=0.5,
    confidence=None,
    pinned=0,
    user_id="",
    scope="agent",
):
    """Insert ONE schema-valid memory_items row via raw SQL, supplying every
    NOT NULL column the real schema requires. `dialect` is the active seam
    dialect (for the placeholder + now()). Returns `mid`."""
    p = dialect.param()
    conn.execute(
        f"INSERT INTO memory_items "
        f"(id, type, title, content, importance, confidence, pinned, is_deleted, "
        f" user_id, scope, valid_from, created_at, updated_at) "
        f"VALUES ({p},{p},{p},{p},{p},{p},{p},0,{p},{p},{dialect.now()},"
        f"{dialect.now()},{dialect.now()})",
        (mid, type, title, content, importance, confidence, pinned, user_id, scope),
    )
    return mid


def seed_embedding_row(conn, dialect, *, mid, embedding, dim, embed_model, emb_id=None, vector_kind=None):
    """Insert ONE schema-valid memory_embeddings row (supplies the NOT NULL `id`
    PK, which hand-written fixtures routinely omit). Returns the embedding id.
    ``vector_kind`` defaults to the schema's own default ('default') when None."""
    p = dialect.param()
    eid = emb_id or f"emb-{mid}"
    cols, vals = ["id", "memory_id", "embedding", "dim", "embed_model"], [eid, mid, embedding, dim, embed_model]
    if vector_kind is not None:
        cols.append("vector_kind")
        vals.append(vector_kind)
    conn.execute(
        f"INSERT INTO memory_embeddings ({', '.join(cols)}) VALUES ({', '.join([p] * len(vals))})",
        tuple(vals),
    )
    return eid


# ── Notification store routing for tests ─────────────────────────────────────
#
# Delivery records moved to their own store, so a test that hardcodes
# `table="notifications"` while `notify_impl` writes to `notification_dispatch`
# sets up a row in one place and asserts about another. It then fails for a
# reason that looks like a product bug and is not.
#
# These resolve the store the SAME WAY production does, so a test exercises the
# real routing rather than a copy of it that can drift.


def dispatch_table_for_tests() -> str:
    """The table `notify_impl` writes to, resolved by production's own code."""
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "bin"))
    from memory.orchestration import _dispatch_write_table

    return _dispatch_write_table()


def dispatch_conn_for_tests(table=None):
    """A read/write connection to the store `notify_impl` writes to.

    Context manager. Mirrors `_store_for_id` for ids at or above the dispatch
    floor, which is where every newly sent notification now lands.

    Emits a NOTE when the table name and this connection disagree about the
    backend -- see :func:`_warn_if_dispatch_backends_disagree`.
    """
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "bin"))
    from m3_core.context import M3Context
    from m3_core.paths import resolve_db_path

    _warn_if_dispatch_backends_disagree(table)
    return M3Context.for_db(resolve_db_path(None)).get_dispatch_conn()


def _warn_if_dispatch_backends_disagree(table=None) -> None:
    """Say WHY a dispatch query is about to fail, before it fails obscurely.

    THE FAILURE THIS EXPLAINS. A dispatch test can hold a table name resolved by
    one path and a connection resolved by another:

        _dispatch_write_table()  -> resolve_backend_name()   (names the TABLE)
        get_dispatch_conn()      -> active_backend()         (opens the CONN)

    When they disagree, the query dies as
    ``no such table: m3_dispatch.notification_dispatch`` -- a SQLite error
    naming a PostgreSQL schema, which says nothing about the disagreement that
    caused it. That message cost a long triage: the decisive facts (each side's
    backend, and WHEN each was resolved) were all available and none were
    reported.

    The known way to produce it is binding the table name at MODULE scope, which
    runs at COLLECTION -- before the autouse ``m3_sandbox`` fixture clears
    ``M3_DB_BACKEND``. The name then captures ``postgres`` while the connection,
    built per test, gets ``sqlite``.

    ⚠ COMPARE THE CALLER'S NAME, NOT THE TWO RESOLVERS. A first version of this
    guard compared ``resolve_backend_name()`` with ``active_backend()`` and was
    silent on a planted reproduction: by TEST time both say ``sqlite`` and agree
    perfectly. The stale value is the one the caller captured at COLLECTION, so
    a probe that only re-resolves cannot see it. ``table`` is passed in for
    exactly that reason.

    §3 evidence levels: the qualification and the live backend are OBSERVED; the
    import-time bind is the `possible:` cause, because this function cannot see
    where the caller's name came from. Silent when they agree -- a note that
    fires on a healthy run is itself a §3 violation.
    """
    try:
        if table is None:
            return
        name = str(table)
        qualified = "." in name
        # Annotated `str`: `.name` is Literal["sqlite","postgres"], so without it
        # mypy narrows the variable to that Literal from the first assignment and
        # rejects the "unavailable (...)" fallback below.
        conn_side: str
        try:
            from memory.backends import active_backend

            conn_side = active_backend().name
        except Exception as exc:  # noqa: BLE001 -- report, never raise from a probe
            conn_side = f"unavailable ({type(exc).__name__})"
        # A schema-qualified name only resolves on a server backend.
        if not qualified or conn_side == "postgres":
            return
        table_side = "postgres"
        print(
            f"\n[conftest] NOTE: dispatch backend mismatch. "
            f"observed: the table name {name!r} is schema-qualified, which "
            f"only resolves on {table_side!r}, but this connection is "
            f"{conn_side!r} (active_backend). The query is about to fail as "
            f"`no such table: {name.replace(chr(34), '')}`. "
            f"possible: the name was bound at MODULE scope, which runs at "
            f"COLLECTION -- before m3_sandbox clears M3_DB_BACKEND -- so it "
            f"captured a backend this test no longer has. "
            f"inspect: resolve the name per use (see the _LazyTable note in "
            f"tests/test_ack_respects_the_lease.py).",
            flush=True,
        )
    except Exception:  # noqa: BLE001 -- a diagnostic must never break a test
        return
