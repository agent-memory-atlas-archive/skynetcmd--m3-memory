"""`m3 doctor --fix` reaches the embed-server repairs, and is a superset of the
report.

The --fix branch used to return before doctor's report section. The exec-bit
repair and the stale-core restart both sat in that section under `if
args.fix:`, so neither ran. Moving them into the branch fixed that, but every
check only the report ran still never showed under --fix: on 2026-10-17 a
service registered without --config was reported by `m3 doctor` and not by
`m3 doctor --fix`. Now --fix repairs, then runs the whole report:
`doctor --fix --verbose` ⊇ `doctor --fix` ⊇ `doctor`.

Every probe is stubbed with a recorder, so these tests exercise only
memory_doctor's control flow and touch no real state.
"""

from __future__ import annotations

import importlib
import importlib.util
import pathlib
import sys

import pytest

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"

_PROBES = (
    "agent_guidance_probe",
    "agent_paths_probe",
    "cascade_probe",
    "claude_mcp_probe",
    "cognitive_loop_probe",
    "dashboard_probe",
    "db_repair",
    "dispatch_probe",
    "embed_server_probe",
    "embed_space_probe",
    "entrypoint_probe",
    "environment_probe",
    "files_extraction_probe",
    "fips_probe",
    "governor_probe",
    "job_inventory",
    "lock_probe",
    "misplaced_rows",
    "oxidation_probe",
    "plugin_version_probe",
    "schedule_probe",
    "shared_embedder_probe",
    "startup_tools_probe",
    "touchid_probe",
    "vector_copy_probe",
)


def _load():
    if str(_BIN) not in sys.path:
        sys.path.insert(0, str(_BIN))
    spec = importlib.util.spec_from_file_location("memory_doctor_fix_test", _BIN / "memory_doctor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def doctor(monkeypatch):
    """memory_doctor with every probe recorded instead of run. Yields (module,
    calls, rcs): calls lists "<probe>.<function>" in order; rcs sets a probe's
    return code."""
    md = _load()
    doctor_pkg = importlib.import_module("memory.doctor")

    async def _fix_impl(dry_run=False):
        return {"summary": "nothing_to_do", "actions": []}

    monkeypatch.setattr(doctor_pkg, "memory_doctor_fix_impl", _fix_impl)
    calls: list[str] = []
    rcs: dict[str, int] = {}
    for name in _PROBES:
        mod = importlib.import_module(f"doctor.{name}")
        for fn in ("run", "repair_and_report", "repair"):
            if hasattr(mod, fn):
                monkeypatch.setattr(
                    mod, fn, lambda *a, _n=f"{name}.{fn}", _p=name, **k: calls.append(_n) or rcs.get(_p, 0)
                )
    monkeypatch.setattr(md, "_repair_embed_server", lambda args: calls.append("_repair_embed_server"))
    return md, calls, rcs


@pytest.mark.parametrize("extra, expect_called", [([], True), (["--skip-embed-server"], False)])
def test_fix_reaches_the_embed_server_repairs(doctor, monkeypatch, extra, expect_called):
    md, calls, _rcs = doctor
    monkeypatch.setattr(sys, "argv", ["memory_doctor.py", "--fix", *extra])
    assert md.main() == 0
    assert ("_repair_embed_server" in calls) is expect_called


def test_fix_runs_every_check_the_report_runs(doctor, monkeypatch):
    md, calls, _rcs = doctor
    monkeypatch.setattr(sys, "argv", ["memory_doctor.py"])
    md.main()
    report = [c for c in calls if c.endswith(".run")]
    calls.clear()
    monkeypatch.setattr(sys, "argv", ["memory_doctor.py", "--fix"])
    md.main()
    missing = sorted(set(report) - set(calls))
    assert report and not missing, f"checks the report runs and --fix does not: {missing}"


def test_verbose_fix_runs_every_check_fix_runs(doctor, monkeypatch):
    md, calls, _rcs = doctor
    monkeypatch.setattr(sys, "argv", ["memory_doctor.py", "--fix"])
    md.main()
    brief = set(calls)
    calls.clear()
    monkeypatch.setattr(sys, "argv", ["memory_doctor.py", "--fix", "--verbose"])
    md.main()
    assert not brief - set(calls), sorted(brief - set(calls))


def test_the_embed_repair_runs_before_the_shared_embedder_repair(doctor, monkeypatch):
    """The shared-embedder repair starts an unsupervised server on the port when
    nothing serves; a service restarted after it could not bind."""
    md, calls, _rcs = doctor
    monkeypatch.setattr(sys, "argv", ["memory_doctor.py", "--fix"])
    md.main()
    assert calls.index("_repair_embed_server") < calls.index("shared_embedder_probe.run")


def test_a_problem_the_report_finds_fails_fix_too(doctor, monkeypatch):
    md, _calls, rcs = doctor
    rcs["fips_probe"] = 1
    monkeypatch.setattr(sys, "argv", ["memory_doctor.py", "--fix"])
    assert md.main() == 1
