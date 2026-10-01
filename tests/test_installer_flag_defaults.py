"""`install.sh` must pass the wizard's RECOMMENDED defaults, not inherit
non-interactive's quieter ones.

The wizard calls the cognitive-loop daemon "recommended" and defaults to YES
when it can ask. Non-interactive setup defaults it OFF:

    plan.cognitive_loop = bool(args.cognitive_loop) and not no_cognitive_loop

`install.sh` runs `m3 setup --non-interactive` and passed no cognitive-loop flag
at all, so EVERY scripted install — the documented one-line path — silently ended
up without the background engine. Entity search stays empty, the knowledge graph
never fills, and the only hint is an `m3 doctor` warning the user has no reason
to run. Found 2026-09-30.

The same file already states the right convention for the native wheel: "on by
default in the wizard; only pass a flag to OPT OUT." The cognitive loop simply
did not follow it. These tests pin the convention for both, because the failure
is invisible — the install succeeds either way.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SH = Path(__file__).resolve().parents[1] / "install.sh"


def _src() -> str:
    return _SH.read_text(encoding="utf-8")


def test_install_sh_opts_IN_to_the_cognitive_loop():
    """THE regression: the scripted path must match the wizard's default."""
    s = _src()
    assert "SETUP_ARGS+=(--cognitive-loop)" in s, (
        "install.sh no longer passes --cognitive-loop, so a scripted install "
        "silently gets no background engine while the wizard calls it recommended"
    )


def test_there_is_an_explicit_opt_out():
    """On-by-default is only acceptable with a documented way to decline — it
    installs a boot service."""
    s = _src()
    assert "--no-cognitive-loop)" in s, "no --no-cognitive-loop flag is parsed"
    assert "SETUP_ARGS+=(--no-cognitive-loop)" in s, "the opt-out is never forwarded"


def test_the_opt_out_is_documented_in_BOTH_usage_blocks():
    """install.sh documents flags twice — a `#` header comment (readable in the
    file) and a heredoc (printed by --help, because `curl | bash` leaves $0 as
    "bash" with no file to read). A flag in only one is a flag half the users
    cannot find. A draft of this change put both copies in the header block,
    because "  --flag" is a substring of "#   --flag"."""
    s = _src()
    assert re.search(r"^#\s+--no-cognitive-loop\s+\S", s, re.M), \
        "missing from the header comment block"
    assert re.search(r"^\s\s--no-cognitive-loop\s+\S", s, re.M), \
        "missing from the --help heredoc block"


def test_the_native_wheel_convention_is_unchanged():
    """The precedent this fix follows: default on, flag to opt out. If this ever
    flips, the cognitive-loop handling above should be revisited with it."""
    s = _src()
    assert "NO_NATIVE_WHEEL=0" in s, "the native wheel is no longer on by default"
    assert "SETUP_ARGS+=(--no-native-wheel)" in s


def test_defaults_are_declared_as_opt_out_shaped():
    """Both knobs declare a NO_* default of 0, so 'unset' means 'do the
    recommended thing' rather than 'skip it'."""
    s = _src()
    for var in ("NO_NATIVE_WHEEL", "NO_COGNITIVE_LOOP"):
        assert re.search(rf"^{var}=0$", s, re.M), f"{var} is not defaulted to 0"


# ── executable checks, not just textual ──────────────────────────────────────
# The assertions above read the source. That is the right shape for "which flag
# is passed", but it cannot catch a script that no longer PARSES — and this
# change edited both usage blocks and the arg-parse case. These two run it.

_NEED_BASH = pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")


@_NEED_BASH
def test_install_sh_is_syntactically_valid():
    """`bash -n` on the real file. A broken installer is invisible to the Python
    suite otherwise — nothing else here executes this script."""
    r = subprocess.run(["bash", "-n", str(_SH)], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, f"bash -n failed: {r.stderr.strip()}"


@_NEED_BASH
def test_help_runs_and_lists_the_new_flag():
    """--help must work via `curl | bash` too, which is why the usage text is a
    self-contained heredoc rather than `sed "$0"` ($0 is "bash" there, with no
    file to read). Running it proves the heredoc is intact and the flag reaches
    the user who looks for it.
    """
    r = subprocess.run(["bash", str(_SH), "--help"], capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, f"--help exited {r.returncode}: {r.stderr[:300]}"
    assert "--no-cognitive-loop" in r.stdout, (
        "--help does not mention --no-cognitive-loop, so the only documented way "
        "to decline a boot service is invisible"
    )
    assert "--no-native-wheel" in r.stdout, "usage block looks truncated"


@_NEED_BASH
def test_an_unknown_flag_does_not_silently_proceed():
    """Pins that arg parsing still rejects typos. A `--no-cognitive-lop` typo
    must not quietly install the service the user tried to decline."""
    r = subprocess.run(["bash", str(_SH), "--no-cognitive-lop"],
                       capture_output=True, text=True, timeout=120)
    # Assert the SPECIFIC rejection, not merely a non-zero exit: this script
    # also exits non-zero on an unsupported OS and on failed prereqs, so a bare
    # `!= 0` would pass for the wrong reason on a machine that cannot install
    # at all. The parser's own branch is `*) echo "unknown flag: $1"; exit 2`.
    assert r.returncode == 2, (
        f"expected the arg parser's exit 2, got {r.returncode}; the test may be "
        f"passing for an unrelated failure. stderr={r.stderr[:200]!r}"
    )
    assert "unknown flag" in r.stderr, (
        "a misspelled flag was not rejected by the parser; a user trying to opt "
        f"OUT would get the service installed anyway. stderr={r.stderr[:200]!r}"
    )
