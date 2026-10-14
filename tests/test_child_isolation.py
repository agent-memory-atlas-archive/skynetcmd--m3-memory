"""A test's child processes stay in the sandbox.

A child gets isolated roots ONLY by inheriting the sandbox's environment: an
in-process pin (``--config``, ``pin_database``) does not cross a process boundary.
So the guard runs a real child with the inherited environment and checks that
every root it resolves lies under this test's tmp dir. ``lib_root`` is excluded on
purpose: the sandbox keeps it on the real installed library (FIPS-mode collection).

The guard must be able to fail: the same check, fed a child whose environment lost
the sandbox roots, must flag it (resolution only; nothing is created).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_ROOTS = ("memory_root", "config_root", "engine_root", "logs_root", "models_root")
_PROBE = (
    f"import json; from m3_memory import _roots as r; print(json.dumps({{n: getattr(r, n)() for n in {_ROOTS!r}}}))"
)
_ROOT_ENV = ("M3_MEMORY_ROOT", "M3_CONFIG_ROOT", "M3_ENGINE_ROOT", "M3_LOGS_ROOT", "M3_MODELS_ROOT")


def _child_roots(env: dict[str, str]) -> dict[str, str]:
    out = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=_REPO, env=env, capture_output=True, text=True, timeout=60, check=False
    )
    assert out.returncode == 0, f"probe child failed (exit {out.returncode}): {out.stderr[-2000:]}"
    return json.loads(out.stdout.strip().splitlines()[-1])


def _escapes(roots: dict[str, str], base: Path) -> dict[str, str]:
    """The roots that resolve outside ``base``."""
    b = os.path.realpath(base)

    def inside(p: str) -> bool:
        try:
            return os.path.commonpath([os.path.realpath(p), b]) == b
        except ValueError:  # another drive (Windows): outside by definition
            return False

    return {n: p for n, p in roots.items() if not inside(p)}


def test_a_child_inherits_the_sandbox_roots(tmp_path):
    escaped = _escapes(_child_roots(dict(os.environ)), tmp_path)
    assert not escaped, (
        f"observed: a test child resolves these roots outside its tmp dir {tmp_path}: {escaped}. "
        "cause: the sandbox's root env vars did not reach the child. fix: tests/conftest.py's "
        "sandbox (and the repo-root conftest) must set every root a child resolves."
    )


def test_the_guard_flags_a_child_that_lost_the_roots(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in _ROOT_ENV}
    env.update(HOME=str(fake_home), USERPROFILE=str(fake_home))
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    escaped = _escapes(_child_roots(env), sandbox)
    assert set(escaped) >= {"config_root", "engine_root"}, escaped
    assert all(str(fake_home) in p for p in escaped.values()), escaped
