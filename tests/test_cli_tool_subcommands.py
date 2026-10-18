"""Regression tests for the generated `m3 <domain> <tool>` human CLI surface.

PR 2 of the dual-surface tool-access work (see
docs/DUAL_SURFACE_TOOL_ACCESS_PLAN.md). `m3_memory/cli.py` grows a codegen
helper (`_add_tool_domain_subcommands`) that turns every catalog tool into a
`m3 <domain> <tool>` subcommand, dispatched through the SAME
`mcp_tool_catalog.execute_tool_structured` path that `m3_call` uses — so the
two surfaces cannot drift.

These tests drive the CLI via subprocess (`python -m m3_memory.cli ...`) so we
can assert on real process exit codes, and compare the structured `result`
against an in-process `execute_tool_structured` call for round-trip parity.

Conventions mirrored from test_lazy_tool_loading.py: bin/ is on sys.path via
conftest.py; the files DB is built fresh per test by the `files_db` fixture.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# conftest.py already puts bin/ on sys.path; belt-and-suspenders for isolation.
_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import mcp_tool_catalog  # noqa: E402

from conftest import use_backend

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def files_db(tmp_path, monkeypatch) -> Path:
    """A fresh files DB built by the real files migrations.

    These tests used the developer-machine artifact memory/files_database.db
    (gitignored), so they skipped in every lane. files tools resolve their store
    as <engine root>/files_database.db (no env name is read), and the sandbox's
    m3_config.toml names the engine root, so building it there routes both the
    CLI child, which inherits M3_CONFIG_ROOT, and the in-process call. `database` is refused on files tools (catalog.dispatch._pop_database)
    and is not a way in. The build is a SQLite file, so the backend is pinned
    to sqlite."""
    import importlib
    import sqlite3

    use_backend("sqlite")
    for var in ("M3_PRIMARY_PG_URL", "M3_PG_URL"):
        monkeypatch.delenv(var, raising=False)
    importlib.import_module("memory.backends.selector")._reset_for_tests()
    from files_memory.migrate import run_pending

    from m3_memory import _roots

    path = Path(_roots.engine_root()) / "files_database.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        assert run_pending(conn), "no files migrations applied to a fresh DB"
        conn.commit()
    finally:
        conn.close()
    return path


def _run_cli(*argv: str) -> subprocess.CompletedProcess:
    """Invoke `python -m m3_memory.cli <argv...>` and capture output.

    cwd is the repo root so relative DB defaults / bin discovery behave like a
    dev checkout.
    """
    return subprocess.run(
        [sys.executable, "-m", "m3_memory.cli", *argv],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )


def _run_cli_force_reexec(*argv: str) -> subprocess.CompletedProcess:
    """Like _run_cli but with a CLEAN env so _ensure_utf8() takes the re-exec
    path (PYTHONUTF8 unset, re-exec sentinel cleared). On Windows that re-exec
    is a spawned subprocess, not a true exec — the historical bug let the
    parent fall through to exit 0, masking every non-zero child exit code
    (§3 fail-loud violation). This harness pins the propagation."""
    env = dict(os.environ)
    env.pop("PYTHONUTF8", None)
    env.pop("_M3_UTF8_REEXEC", None)
    return subprocess.run(
        [sys.executable, "-m", "m3_memory.cli", *argv],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env=env,
    )


# ── files_stats: happy path ──────────────────────────────────────────────────


def test_files_stats_returns_json_with_file_nodes_total(files_db):
    """`m3 files files_stats` exits 0 and emits parseable JSON
    carrying the corpus counter `file_nodes_total`."""
    proc = _run_cli("files", "files_stats")
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    data = json.loads(proc.stdout)
    assert "file_nodes_total" in data, data
    assert isinstance(data["file_nodes_total"], int)


def test_files_stats_roundtrip_parity_with_execute_tool_structured(files_db):
    """The `result` from the CLI surface must equal what the m3_call path
    (execute_tool_structured) returns for the same tool + args. Both surfaces
    share the impl, so any divergence is a dispatch bug.

    """
    proc = _run_cli("files", "files_stats")
    assert proc.returncode == 0, proc.stderr
    cli_result = json.loads(proc.stdout)

    spec = next(t for t in mcp_tool_catalog.TOOLS if t.name == "files_stats")
    direct = asyncio.run(mcp_tool_catalog.execute_tool_structured(spec, {}, agent_id="", dry_run=False))
    # Normalize through json so dict/tuple coercion matches the CLI's
    # json.dumps(default=str) boundary.
    direct_norm = json.loads(json.dumps(direct, default=str))
    assert cli_result == direct_norm, f"CLI surface diverged from m3_call path:\ncli={cli_result}\ndirect={direct_norm}"


def _seed_routing_probe(files_db) -> None:
    """One corpus row the default store lacks, so a read that finds it proves
    which store answered. Parity alone cannot: two surfaces that both read the
    wrong store agree."""
    import sqlite3

    from memory.backends.dialect import dialect_for

    p = dialect_for("sqlite").param()
    conn = sqlite3.connect(str(files_db))
    try:
        conn.execute(f"INSERT INTO corpus_settings (corpus_id) VALUES ({p})", ("routing-probe",))
        conn.commit()
    finally:
        conn.close()


def test_files_tools_read_the_pinned_files_store(files_db):
    """The fixture's store is the one answering, so the tests above measure it."""
    _seed_routing_probe(files_db)
    proc = _run_cli("files", "files_corpus_get", "--corpus_id", "routing-probe")
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    result = json.loads(proc.stdout)
    assert result and result.get("corpus_id") == "routing-probe", (
        f"files_corpus_get did not read the engine root's files store: {result}"
    )


def test_files_tools_refuse_the_database_argument(files_db, tmp_path):
    """`--database` on a files tool is refused, never silently ignored.

    Measured 2026-10-08: `m3 files files_stats --database <new
    path>` returned the default store's counts and created nothing at the path.
    The refusal stands until named stores (`--store`) route it."""
    _seed_routing_probe(files_db)
    elsewhere = tmp_path / "elsewhere.db"
    proc = _run_cli("files", "files_corpus_get", "--corpus_id", "routing-probe", "--database", str(elsewhere))
    assert proc.returncode != 0, f"refusal exited 0: stdout={proc.stdout}"
    out = proc.stdout + proc.stderr
    assert "`database` would be ignored" in out and "`store`" in out, out
    assert "routing-probe" not in proc.stdout, "the call ran against a store"
    assert not elsewhere.exists(), "the refusal created a file at --database"


# ── --help lists generated tools ─────────────────────────────────────────────


def test_files_help_lists_generated_tools():
    """`m3 files --help` exits 0 and lists the generated file tools."""
    proc = _run_cli("files", "--help")
    assert proc.returncode == 0, proc.stderr
    assert "files_stats" in proc.stdout
    assert "files_search" in proc.stdout


# ── destructive gate ─────────────────────────────────────────────────────────

_ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def test_destructive_tool_without_yes_is_gated():
    """`memory memory_delete` (a destructive tool) without --yes must refuse:
    exit 2, stderr mentions --yes."""
    proc = _run_cli("memory", "memory_delete", "--id", _ZERO_UUID)
    assert proc.returncode == 2, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "--yes" in proc.stderr, proc.stderr


def test_destructive_tool_dry_run_needs_no_yes():
    """`--dry-run` validates + gate-checks without executing and without --yes:
    exit 0, stdout JSON has dry_run True."""
    proc = _run_cli("memory", "memory_delete", "--id", _ZERO_UUID, "--dry-run")
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    data = json.loads(proc.stdout)
    assert data.get("dry_run") is True, data
    assert data.get("ok") is True, data


# ── complex-arg tools take --json ────────────────────────────────────────────


def test_complex_arg_tool_rejects_invalid_json():
    """A complex-arg tool (task_create) with malformed --json exits 2."""
    proc = _run_cli("tasks", "task_create", "--json", "{invalid")
    assert proc.returncode == 2, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "json" in proc.stderr.lower(), proc.stderr


def test_complex_arg_tool_accepts_valid_json_dry_run():
    """A valid --json object on a complex-arg tool validates cleanly under
    --dry-run (no mutation): exit 0, dry_run True.

    The payload must use REAL parameters. This used to pass ``{"subject": "x"}``
    -- not a task_create parameter at all -- and succeeded only because --json
    accepted any key and silently dropped it. That is the defect
    tests/test_cli_json_unknown_keys.py now pins, so the fixture is corrected
    here rather than the guard being loosened to keep a bad payload green.
    """
    proc = _run_cli(
        "tasks",
        "task_create",
        "--json",
        json.dumps({"title": "x", "created_by": "t"}),
        "--dry-run",
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    data = json.loads(proc.stdout)
    assert data.get("dry_run") is True, data


# ── chat namespace (chatlog domain → `chat` command + op subcommands) ─────────


def test_chat_status_is_operational_and_exits_zero():
    """`m3 chat status` runs the operational chatlog status command (not a
    generated data-tool) and exits 0."""
    proc = _run_cli("chat", "status")
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"


def test_chat_help_lists_both_operational_and_data_tools():
    """`m3 chat --help` is the single chatlog namespace: it lists BOTH the
    operational ops (init/status) AND the generated chatlog data-tools
    (chatlog_search)."""
    proc = _run_cli("chat", "--help")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "init" in out, out
    assert "status" in out, out
    assert "chatlog_search" in out, out


def test_chatlog_status_alias_still_works():
    """`m3 chatlog status` (the pre-existing back-compat alias) still exits 0."""
    proc = _run_cli("chatlog", "status")
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"


# ── exclusions: dispatcher/meta tools are NOT generated subcommands ───────────


def test_m3_call_is_not_a_generated_subcommand():
    """m3_call is excluded from the human CLI surface — `m3 admin m3_call`
    must fail (argparse rejects the unknown choice, exit 2)."""
    proc = _run_cli("admin", "m3_call")
    assert proc.returncode != 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"


def test_admin_help_does_not_list_m3_call():
    """The admin domain --help must not advertise the excluded m3_call."""
    proc = _run_cli("admin", "--help")
    assert proc.returncode == 0, proc.stderr
    assert "m3_call" not in proc.stdout, proc.stdout


# ── exit-code propagation through the UTF-8 re-exec (§3 fail-loud) ────────────


def test_error_exit_code_propagates_through_utf8_reexec():
    """A failing command MUST exit non-zero even when _ensure_utf8 takes the
    re-exec path. On Windows the re-exec is a spawned subprocess; the parent
    must propagate the child's exit code instead of falling through to 0.
    Regression for the bug where every CLI error silently became exit 0."""
    proc = _run_cli_force_reexec("admin", "m3_call")  # invalid choice -> argparse exit 2
    assert proc.returncode != 0, (
        f"error exit code was masked to {proc.returncode} through the re-exec "
        f"(§3 fail-loud violation)\nstderr={proc.stderr}"
    )


def test_success_exit_code_propagates_through_utf8_reexec():
    """The flip side: a clean command still exits 0 through the re-exec path."""
    proc = _run_cli_force_reexec("--version")
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
