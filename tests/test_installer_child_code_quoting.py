"""The installer's PG-schema child program must survive any install path.

`_run_pg_migrations` builds a `python -c` program that puts the bridge's
directory on sys.path. It embedded the path as `r'%s'`, which is a SyntaxError
when the path contains a quote (a home directory like `O'Brien`) or ends in a
backslash, and the schema step then fails on that user's machine. The path is
now embedded with repr(), which round-trips every str.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("name", ["O'Brien", 'say "hi"', "plain"])
def test_child_program_compiles_and_names_the_path(monkeypatch, tmp_path, name):
    from m3_memory import installer

    bin_dir = tmp_path / name / "bin"
    seen: dict = {}

    def _fake_run(argv, **kw):
        seen["code"] = argv[argv.index("-c") + 1]
        return subprocess.CompletedProcess(argv, 0, "OK", "")

    monkeypatch.setattr(installer.subprocess, "run", _fake_run)
    installer._run_pg_migrations(bin_dir / "memory_bridge.py", "postgresql://u@h/db")

    tree = ast.parse(seen["code"])  # raises SyntaxError on a broken quote
    inserted = [
        ast.literal_eval(n.args[1])
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "sys.path.insert"
    ]
    assert [Path(p) for p in inserted] == [bin_dir]
