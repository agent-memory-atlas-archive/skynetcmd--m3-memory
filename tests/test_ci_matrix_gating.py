"""Every supported Python gates CI, on its final release; the PostgreSQL lane has pgvector.

3.15 was a non-blocking probe while it had no stable build. Since 3.15.0 final
(2026-10-09) it gates like 3.12/3.13/3.14, and no setup step may take a release
candidate: a version pyproject classifies as supported is tested on what users
install. The PostgreSQL service carries pgvector, or the in-database vector path
goes untested in CI. These tests pin each of those, so a later edit cannot
quietly excuse a version or drop the extension while CI still looks configured.
"""

from __future__ import annotations

import os
import re
import tomllib

import pytest

from conftest import skip_reason

yaml = pytest.importorskip("yaml", reason=skip_reason("dep", "pyyaml needed to parse the workflow"))

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_CI = os.path.join(_ROOT, ".github", "workflows", "ci.yml")


def _workflow() -> dict:
    with open(_CI, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _supported() -> list[str]:
    """The versions pyproject.toml classifies as supported (what CI must gate)."""
    with open(os.path.join(_ROOT, "pyproject.toml"), "rb") as fh:
        classifiers = tomllib.load(fh)["project"]["classifiers"]
    found = [m.group(1) for c in classifiers if (m := re.fullmatch(r"Programming Language :: Python :: (3\.\d+)", c))]
    assert found, "pyproject.toml lists no Python version classifiers"
    return found


def test_no_supported_version_is_excused():
    coe = str(_workflow()["jobs"]["test"].get("continue-on-error", ""))
    assert coe.strip().lower() not in ("true", "${{ true }}"), "the whole matrix is advisory"
    for v in _supported():
        assert v not in coe, f"Python {v} is classified as supported but excused by continue-on-error: {coe!r}"


def test_the_matrix_runs_every_supported_version():
    """The matrix is emitted as a JSON string by the matrix-setup job."""
    blob = " ".join(str(s.get("run", "")) for s in _workflow()["jobs"]["matrix-setup"]["steps"])
    for v in _supported():
        assert f'"{v}"' in blob, f"Python {v} is classified as supported but not in the CI matrix"


def test_no_setup_step_takes_a_prerelease():
    for name, job in _workflow()["jobs"].items():
        for step in job.get("steps") or []:
            if "setup-python" in str(step.get("uses", "")):
                assert not (step.get("with") or {}).get("allow-prereleases"), (
                    f"job {name!r} lets setup-python resolve a release candidate"
                )


def test_the_postgresql_service_has_pgvector():
    image = _workflow()["jobs"]["postgres"]["services"]["postgres"]["image"]
    assert "pgvector" in image, f"the PostgreSQL lane runs {image!r}, which has no pgvector"


def test_fail_fast_stays_off():
    """One lane failing must not cancel the others before they report."""
    assert _workflow()["jobs"]["test"]["strategy"].get("fail-fast") is False
