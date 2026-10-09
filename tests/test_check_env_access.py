"""bin/check_env_access.py: the environment-access inventory gate (plan Step 2A.1).

REPORT mode only: it must catch every planted shape (selftest) and must scan
every production file. It does not yet fail on violations; that is the
enforce step, once the config package and child_env exist.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "check_env_access.py"


def _run(*args):
    return subprocess.run([sys.executable, str(_SCRIPT), *args], capture_output=True, text=True)


def test_selftest_catches_every_planted_shape():
    r = _run("--selftest")
    assert r.returncode == 0, r.stdout + r.stderr


def test_every_production_file_is_scanned():
    """Exit 2 means a file did not parse: unscanned is not clean."""
    r = _run("--summary")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "REPORT MODE" in r.stdout


@pytest.mark.parametrize("snippet,expected", [
    ("import os\nos.environ.get('HOME')", "allowed-os-name"),
    ("import os\nos.environ.get('M3_X')", "violation"),
    ("import os\nkey = 'M3_X'\nos.environ.get(key)", "violation"),
])
def test_verdicts(snippet, expected):
    sys.path.insert(0, str(_SCRIPT.parent))
    try:
        import check_env_access as cea
    finally:
        sys.path.pop(0)
    verdicts = {f.verdict for f in cea.scan_source(snippet, "x.py")}
    assert verdicts == {expected}, verdicts


def test_docstring_mentioning_environ_is_not_a_program():
    """Prose about os.environ must not become a false 'unparsed' finding."""
    sys.path.insert(0, str(_SCRIPT.parent))
    try:
        import check_env_access as cea
    finally:
        sys.path.pop(0)
    src = 'def f():\n    """Reads os.environ first.\n\n    then the config file."""\n    return 1\n'
    assert cea.scan_source(src, "x.py") == []
