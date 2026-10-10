"""Every version LIMIT m3 declares says why (user rule, 2026-10-10).

A ceiling (`<`), an exact pin (`==`, `~=`) or an exclusion (`!=`) is a decision,
and a decision without its reason gets "cleaned up" by the next person, or kept
long after the reason expired. Floors (`>=`) are not limits. A limit is
documented when:
  * it is exactly the policy's default ceiling, `>=X.y,<X+1` (`<1` for a 0.x
    floor), whose one reason is docs/DEPENDENCY_NOTES.md "Version-limit
    policy"; or one of these names the package:
  * a `### \\`name\\`` section in docs/DEPENDENCY_NOTES.md,
  * a comment on the requirement's own line,
  * the comment block directly above it (no blank or code line between).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_LIMIT = re.compile(r"(<|==|~=|!=)")


def _name(spec: str) -> str:
    return re.split(r"[\s\[<>=!~;]", spec, maxsplit=1)[0].lower().replace("_", "-")


def _specs() -> list[tuple[str, str]]:
    d = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    out = [("dependencies", s) for s in d["project"]["dependencies"]]
    for k, v in d["project"].get("optional-dependencies", {}).items():
        out += [(f"optional-dependencies.{k}", s) for s in v]
    for k, v in d.get("dependency-groups", {}).items():
        out += [(f"dependency-groups.{k}", s) for s in v if isinstance(s, str)]
    out += [("build-system", s) for s in d.get("build-system", {}).get("requires", [])]
    out += [("tool.uv.override", s) for s in d.get("tool", {}).get("uv", {}).get("override-dependencies", [])]
    return out


def _noted() -> set[str]:
    notes = (_ROOT / "docs" / "DEPENDENCY_NOTES.md").read_text(encoding="utf-8")
    return {_name(m.group(1)) for m in re.finditer(r"^### `([^`]+)`", notes, re.MULTILINE)}


def _commented(lines: list[str], spec: str) -> bool:
    name = _name(spec)
    for i, line in enumerate(lines):
        if f'"{spec}"' not in line:
            continue
        if "#" in line.split(f'"{spec}"', 1)[1]:
            return True
        block = []
        j = i - 1
        while j >= 0 and lines[j].strip().startswith("#"):
            block.append(lines[j])
            j -= 1
        if any(re.search(rf"\b{re.escape(name)}\b", b, re.IGNORECASE) for b in block):
            return True
    return False


def _policy_ceiling(spec: str) -> bool:
    """`>=X.y...,<X+1` exactly (and nothing else): the documented default."""
    clauses = [c.strip() for c in re.sub(r"^[^<>=!~]+", "", spec.split(";")[0]).split(",")]
    floors = [c[2:] for c in clauses if c.startswith(">=")]
    ceils = [c[1:] for c in clauses if c.startswith("<") and not c.startswith("<=")]
    if len(clauses) != 2 or len(floors) != 1 or len(ceils) != 1:
        return False
    major = int(floors[0].split(".")[0])
    return ceils[0] == str(major + 1)


def test_the_policy_is_written_down():
    notes = (_ROOT / "docs" / "DEPENDENCY_NOTES.md").read_text(encoding="utf-8")
    assert "## Version-limit policy" in notes and "next major" in notes


def test_policy_ceiling_shape():
    assert _policy_ceiling("requests>=2.34.2,<3")
    assert _policy_ceiling("httpx[http2]>=0.28.1,<1")
    assert not _policy_ceiling("cryptography>=50.0.1,<52")  # wider than the next major
    assert not _policy_ceiling("mcp>=1.30.0,<1.31")  # narrower
    assert not _policy_ceiling("ruff==0.16.10")  # exact pin


def test_every_limit_is_documented():
    lines = (_ROOT / "pyproject.toml").read_text(encoding="utf-8").split("\n")
    noted = _noted()
    missing = [
        f"{where}: {spec}"
        for where, spec in _specs()
        if _LIMIT.search(spec.split(";")[0])
        and not _policy_ceiling(spec)
        and _name(spec) not in noted
        and not _commented(lines, spec)
    ]
    assert not missing, (
        "observed: version limits with no stated reason:\n  "
        + "\n  ".join(missing)
        + "\ninspect: add a `### `name`` section to docs/DEPENDENCY_NOTES.md, or a comment "
        "naming the package on or directly above the requirement"
    )


def test_noted_specs_match_the_declarations():
    """A section heading quotes the spec it explains; if pyproject moves and the
    note does not, the reason on record is for a limit that no longer exists."""
    notes = (_ROOT / "docs" / "DEPENDENCY_NOTES.md").read_text(encoding="utf-8")
    declared: dict[str, set[str]] = {}
    for _where, spec in _specs():
        declared.setdefault(_name(spec), set()).add(spec.replace(" ", ""))
    stale = []
    for m in re.finditer(r"^### `([^`]+)` \(`([^`]+)`\)", notes, re.MULTILINE):
        name, quoted = _name(m.group(1)), m.group(2).replace(" ", "")
        if quoted not in declared.get(name, set()):
            stale.append(
                f"{m.group(1)}: note says {m.group(2)}, pyproject has {sorted(declared.get(name, set())) or 'nothing'}"
            )
    assert not stale, (
        "observed: dependency notes for limits that changed:\n  "
        + "\n  ".join(stale)
        + "\ninspect: update the note (and its reason) with the spec"
    )
