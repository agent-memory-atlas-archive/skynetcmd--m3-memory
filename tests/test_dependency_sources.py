"""pyproject.toml declares; uv.lock and requirements.txt are both derived.

Each link is checked without uv (CI and the pip-only lanes have none):
  * uv.lock's record of the project's dependencies equals pyproject's, so an
    edited pyproject without `uv lock` fails here;
  * requirements.txt (the lane set, resolved on its own; see
    bin/gen_requirements.py for why) carries a digest of the declarations it
    was resolved from, and every direct pin satisfies its declared range, so an
    edited pyproject without `python bin/gen_requirements.py` fails here.
Before this, pyproject and a hand-kept requirements.txt drifted
(types-setuptools <82 in one, >=84 in the other).
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import gen_requirements  # noqa: E402

_PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
_LOCK = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
_ROOT_PKG = next(p for p in _LOCK["package"] if p["name"] == "m3-memory")


def _key(name: str, extras, spec: str) -> tuple:
    return canonicalize_name(name), frozenset(extras or ()), SpecifierSet(spec or "")


def _from_pyproject(reqs: list[str]) -> set[tuple]:
    out = set()
    for r in reqs:
        q = Requirement(r)
        out.add(_key(q.name, q.extras, str(q.specifier)))
    return out


def _flatten(group: str) -> list[str]:
    """A dependency group with its {include-group = ...} entries expanded."""
    out: list[str] = []
    for item in _PYPROJECT["dependency-groups"][group]:
        out += _flatten(item["include-group"]) if isinstance(item, dict) else [item]
    return out


def _lock_entries(entries: list[dict]) -> set[tuple]:
    return {_key(e["name"], e.get("extras"), e.get("specifier", "")) for e in entries}


def test_lock_records_pyprojects_dependencies():
    dist = _ROOT_PKG["metadata"]["requires-dist"]
    base = [e for e in dist if "extra ==" not in e.get("marker", "")]
    assert _lock_entries(base) == _from_pyproject(_PYPROJECT["project"]["dependencies"]), (
        "uv.lock is stale for [project].dependencies; run `uv lock`"
    )
    for extra, reqs in _PYPROJECT["project"]["optional-dependencies"].items():
        mine = [e for e in dist if f"extra == '{extra}'" in e.get("marker", "")]
        assert _lock_entries(mine) == _from_pyproject(reqs), f"uv.lock is stale for the {extra!r} extra; run `uv lock`"


def test_lock_records_pyprojects_groups():
    dev = _ROOT_PKG["metadata"]["requires-dev"]
    for group in _PYPROJECT["dependency-groups"]:
        assert _lock_entries(dev.get(group, [])) == _from_pyproject(_flatten(group)), (
            f"uv.lock is stale for the {group!r} group; run `uv lock`"
        )


def test_requirements_txt_is_resolved_from_these_declarations():
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    m = re.search(r"^# declarations sha256: ([0-9a-f]{64})$", text, re.MULTILINE)
    assert m, "requirements.txt has no declarations digest; run `python bin/gen_requirements.py`"
    assert m.group(1) == gen_requirements.declarations_digest(), (
        "pyproject's dependencies or lane groups changed since requirements.txt was "
        "resolved; run `python bin/gen_requirements.py`"
    )
    pinned: dict[str, str] = {}
    for line in text.splitlines():
        s = line.split("#", 1)[0].strip()
        if not s:
            continue
        q = Requirement(s)
        pins = [sp.version for sp in q.specifier if sp.operator == "=="]
        assert len(pins) == 1, f"not an exact pin: {s}"
        pinned[canonicalize_name(q.name)] = pins[0]
    declared = list(_PYPROJECT["project"]["dependencies"])
    for g in gen_requirements.GROUPS:
        declared += _flatten(g)
    for r in declared:
        q = Requirement(r)
        platforms = ("darwin", "linux", "win32")
        if q.marker is not None and not any(q.marker.evaluate({"sys_platform": p}) for p in platforms):
            continue
        name = canonicalize_name(q.name)
        assert name in pinned, f"declared but not pinned: {r}"
        assert q.specifier.contains(pinned[name], prereleases=True), (
            f"pin {name}=={pinned[name]} is outside the declared {r}"
        )


def test_deprecated_dev_extra_equals_the_dev_group():
    extra = _from_pyproject(_PYPROJECT["project"]["optional-dependencies"]["dev"])
    # The group adds the test-only packages the suite exercises; the extra
    # must still cover every tool in lint and release.
    tools = _from_pyproject(
        _flatten("lint") + _flatten("release") + [r for r in _flatten("test") if r.startswith("pytest")]
    )
    assert extra == tools, "the deprecated dev extra drifted from the dev group"


def test_no_dead_stdlib_backports():
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert not re.search(r"^argparse==", text, re.MULTILINE), "argparse is stdlib; the PyPI backport is dead"
