"""A stale native core must name its FIX in brief mode, not only under --verbose.

`m3 doctor` is brief by default; `--verbose` opts into detail. The stale-wheel
warning used to read:

    ⚠️  oxidation: STALE (8/8 paths, version behind) — reinstall

naming a state to change with no way to change it. The command lived only in the
verbose block, which someone who does not yet understand the warning has no
reason to go looking for. And `m3 doctor --fix` cannot help: `oxidation_probe` is
report-only by design (see `bin/doctor/__init__.py`) and the orchestrator passes
`fix=` only to probes that repair — so a user who runs the obvious remediation
sees the warning survive and concludes --fix is broken.

A stale core is reached by an ORDINARY upgrade, not an edge case: the native
wheel is a separate distribution, so `pipx upgrade m3-memory` advances the Python
code and leaves the extension behind. Measured 2026-09-30 on the Linux build box:
m3-memory 2026.9.21.0 against m3_core_rs 3.9.7, expected 3.9.20.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PROBE = _ROOT / "bin" / "doctor" / "oxidation_probe.py"

sys.path.insert(0, str(_ROOT / "bin"))


def _brief_strings() -> str:
    """Every string literal reachable in a brief-mode output path.

    AST rather than a regex on purpose: the command sits on a CONTINUATION line
    of a multi-line `print(...)`, and a line-oriented regex reads only the first
    line and reports the command missing. A draft of this check did exactly that
    and declared the fix absent moments after it was added.
    """
    tree = ast.parse(_PROBE.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        is_brief_if = (isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                       and node.test.id == "brief")
        is_b_call = (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                     and node.func.id == "_b")
        if is_brief_if or is_b_call:
            out += [n.value for n in ast.walk(node)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    return " ".join(out)


def test_the_brief_stale_warning_names_a_command():
    """THE regression: a warning the default invocation shows must be actionable."""
    text = _brief_strings()
    assert "STALE" in text, (
        "the brief-mode stale warning disappeared from oxidation_probe; this test "
        "pins its remediation and can no longer see it"
    )
    assert "m3 embedder install-gpu" in text, (
        "the brief STALE warning must name the command that fixes it. It is the "
        "line `m3 doctor` prints by default, and `--fix` cannot repair this probe "
        f"(report-only). brief strings were: {text!r}"
    )


def test_the_command_matches_what_the_rest_of_the_cli_recommends():
    """Single owner for the remediation string. `m3 embedder install-gpu` is what
    cli.py tells users elsewhere, and only it and install_os.py drive the native
    installer — so a different spelling here would send them somewhere useless."""
    cli = (_ROOT / "m3_memory" / "cli.py").read_text(encoding="utf-8")
    assert "m3 embedder install-gpu" in cli, (
        "the probe recommends `m3 embedder install-gpu` but cli.py no longer "
        "mentions it — the command was renamed and this advice now dead-ends"
    )


def test_verbose_still_carries_the_fuller_explanation():
    """Brief gained the command; verbose must keep impact + remediation, so the
    one-liner stays a pointer rather than becoming the only account."""
    src = _PROBE.read_text(encoding="utf-8")
    assert "impact   :" in src
    assert "fix      :" in src


def test_the_probe_is_report_only():
    """Pins WHY brief must carry the command: nothing else will fix it.

    If `run()` ever grows a `fix` parameter, this test should fail and be
    revisited — at that point `m3 doctor --fix` could repair the core and the
    brief line's advice would change.
    """
    import importlib
    import inspect

    probe = importlib.import_module("doctor.oxidation_probe")
    params = inspect.signature(probe.run).parameters
    assert "fix" not in params, (
        "oxidation_probe.run() gained a `fix` parameter. `m3 doctor --fix` may "
        "now be able to repair a stale core — revisit the brief-mode advice, "
        "which currently assumes a manual command is the only route"
    )
