"""Every released version has exactly one CHANGELOG section.

Two ways a release goes missing from the record, both of which shipped: a
version cut with no section of its own (2026.9.13.2), and a second section filed
under an existing version's heading (2026.9.13.0 appeared twice). Neither is
caught by reading the file, and the impact-header test checks bullets, not
headings.

The release version's one source of truth is pyproject (`[project].version`);
`sync_manifest_versions.py` derives every other copy from it, so a release is a
pyproject bump, and that is where a missing section is checked.
"""

from __future__ import annotations

import re
import tomllib
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_HEADING = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)


def _versions() -> list[str]:
    return [v for v in _HEADING.findall((REPO / "CHANGELOG.md").read_text(encoding="utf-8")) if v != "Unreleased"]


def test_no_version_heading_appears_twice():
    dupes = sorted(v for v, n in Counter(_versions()).items() if n > 1)
    assert not dupes, (
        f"observed: CHANGELOG.md has more than one section for {dupes}. "
        "inspect: merge the duplicate into the version's one section"
    )


def test_the_release_version_has_a_section():
    version = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert version in _versions(), (
        f"observed: pyproject is at {version} but CHANGELOG.md has no `## [{version}]` section. "
        "inspect: move [Unreleased] into a section for this version when cutting the release"
    )


def test_no_impact_footer_is_stranded_after_a_rule():
    """A footer belongs to the bullet above it. One placed after the section's
    `---` rule still passes the impact-header test (a rule is not a heading)
    but reads as attached to nothing; ten sections shipped that way."""
    lines = (REPO / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    stranded = [n + 1 for n in range(1, len(lines)) if lines[n - 1] == "---" and lines[n].startswith("  **Affected:**")]
    assert not stranded, (
        f"observed: an impact footer directly after a `---` rule at line(s) {stranded}. "
        "inspect: move it up onto the last bullet of its section"
    )


def test_the_guard_sees_a_duplicate(tmp_path):
    """Self-check: the heading reader counts a repeated version."""
    text = "## [Unreleased]\n## [1.0.1] — a\n## [1.0.0] — b\n## [1.0.0] — c\n"
    found = [v for v in _HEADING.findall(text) if v != "Unreleased"]
    assert Counter(found)["1.0.0"] == 2
