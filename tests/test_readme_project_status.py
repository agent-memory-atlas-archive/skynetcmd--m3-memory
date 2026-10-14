"""The README's "Project status" section stays true.

Search and AI agents often read only the README, so that section is where m3
states its own maturity, limits and plans: one summary line per point, each linked
to the doc that owns the detail. These tests tie it to its sources, so a shipped
feature, a resolved limit or a changed classifier cannot leave it stale.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_README = (_ROOT / "README.md").read_text(encoding="utf-8")
_ROADMAP = (_ROOT / "docs" / "ROADMAP.md").read_text(encoding="utf-8")

# A reader that truncates long pages must still reach it.
_MAX_OFFSET = 8_000


def _section() -> str:
    start = _README.index('<a id="project-status"></a>')
    return _README[start : _README.index("\n---\n", start)]


def _row(label: str) -> list[str]:
    m = re.search(rf"^\| \*\*{label}\*\* \|[^|]*\|([^|]*)\|$", _section(), re.MULTILINE)
    assert m, f"Project status has no {label} row"
    return [t.strip() for t in m.group(1).split("·")]


def _words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", text)}


def _roadmap_section(heading: str) -> str:
    start = _ROADMAP.index(heading)
    nxt = _ROADMAP.find("\n## ", start + 1)
    return _ROADMAP[start : nxt if nxt != -1 else None]


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, punctuation dropped, spaces to '-'."""
    s = heading.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def test_the_section_is_near_the_top():
    offset = _README.index('<a id="project-status"></a>')
    assert offset < _MAX_OFFSET, f"Project status starts at char {offset}; readers that truncate may miss it"


def test_every_link_in_the_section_resolves():
    for target in re.findall(r"\]\(([^)#]+)(?:#([^)]+))?\)", _section()):
        path, anchor = target
        f = _ROOT / path
        assert f.is_file(), f"Project status links to a missing file: {path}"
        if anchor:
            slugs = {_slug(h) for h in re.findall(r"^#{1,6} (.+)$", f.read_text(encoding="utf-8"), re.MULTILINE)}
            assert anchor in slugs, f"Project status links to {path}#{anchor}, which has no such heading"


def test_beta_matches_the_roadmaps_hardening_list():
    hardening = re.findall(r"^- \[x\] \*\*([^*]+)\*\*", _roadmap_section("## 🚧 In progress"), re.MULTILINE)
    beta = _row("Beta")
    assert len(beta) == len(hardening), f"README Beta {beta} vs ROADMAP still-hardening {hardening}"
    for item in beta:
        # Short README names ("Web dashboard"): every significant word must be in the item.
        assert any(_words(item) <= _words(h) for h in hardening), (
            f"README Beta item {item!r} matches nothing the ROADMAP lists as still hardening: {hardening}"
        )


def test_every_planned_item_is_still_open_in_the_roadmap():
    open_items = re.findall(r"^- \[ \] \*\*([^*]+)\*\*(.*)$", _ROADMAP, re.MULTILINE)
    for item in _row("Planned"):
        hits = [name for name, rest in open_items if len(_words(item) & _words(name + rest)) >= 2]
        assert hits, f"README lists {item!r} as planned, but no open ROADMAP item matches (shipped? update the README)"


def test_the_at_a_glance_status_line_names_exactly_the_beta_items():
    """The summary list near the top restates the Beta row (readers that truncate
    see only it); a copy that drifts would tell them something the table does not."""
    m = re.search(r"^- \*\*Status:\*\* (.+)$", _README, re.MULTILINE)
    assert m, "README's 'm3 at a glance' list has no Status line"
    line = _words(m.group(1))
    for item in _row("Beta"):
        assert _words(item) <= line, f"the at-a-glance Status line does not name the Beta item {item!r}"


def test_the_read_attribution_limit_tracks_its_roadmap_item():
    says_limit = "reads are not yet attributed" in _section().lower()
    open_item = re.search(r"^- \[ \] \*\*Memory read-access audit log\*\*", _ROADMAP, re.MULTILINE) is not None
    assert says_limit == open_item, (
        "README's read-attribution limit and the ROADMAP's read-access audit log item disagree; "
        "when the log ships, close the item and drop the limit together"
    )


def test_the_pypi_classifier_matches_the_stated_core_maturity():
    classifiers = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["classifiers"]
    status = next(c for c in classifiers if c.startswith("Development Status"))
    core_stable = "the core is stable" in _section().lower()
    assert core_stable == (status == "Development Status :: 5 - Production/Stable"), (
        f"README says core stable={core_stable}, PyPI says {status!r}"
    )
