#!/usr/bin/env python3
"""Single source of truth for the tool-catalog pre-push drift gate.

`bin/mcp_tool_catalog.py` is the canonical MCP tool registry. Several
artifacts are *generated* from it and are NOT auto-refreshed on write:

  - docs/tools/MCP_CATALOG.json   (via bin/gen_tool_manifest.py)
  - docs/MCP_TOOLS.md             (via bin/gen_mcp_inventory.py)
  - hardcoded "N tools" counts in README.md / docs/COMPARISON.md /
    docs/MYTHS_AND_FACTS.md / docs/tools/files_memory.md

If a tool is added/removed/renamed and these aren't regenerated, the docs
silently lie. This check regenerates the artifacts and fails if anything
changed (drift) or if the drift tests fail.

It is invoked by BOTH:
  - .githooks/pre-push  (local gate, every agent + human, before push)
  - .github/workflows/tool-catalog-drift.yml  (required CI check, on PR/push)

so the rule holds regardless of which agent (Claude / Gemini / Antigravity /
human) authored the change or which AGENTS file they read. This is the
mechanical enforcement behind the prose in docs/AGENT_INSTRUCTIONS.md.

Exit codes:
  0  no drift, tests pass
  1  drift detected or a drift test failed (the artifacts have been
     regenerated in the working tree — stage and commit them, then re-run)
  2  the check itself could not run (missing generator, etc.)

Usage:
    python bin/check_tool_catalog_drift.py            # check
    python bin/check_tool_catalog_drift.py --fix      # regen + leave staged-ready
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PY = sys.executable

# Generators to run, in order. Each must be idempotent.
_GENERATORS = [
    ["bin/gen_tool_manifest.py"],
    ["bin/gen_mcp_inventory.py"],
    ["bin/gen_tool_inventory.py"],
]

# Paths the generators (and hand-maintained count claims) touch. Drift is
# measured as `git diff` over exactly these — nothing else in the tree.
_GENERATED_PATHS = [
    "docs/tools/MCP_CATALOG.json",
    "docs/MCP_TOOLS.md",
    "docs/tools/",
]

# Drift tests that gate the count claims + manifest freshness.
_DRIFT_TESTS = [
    "tests/test_tool_count_drift.py",
    "tests/test_mcp_catalog_manifest_fresh.py",
    # Covers gen_tool_inventory's per-script pages (hash-stamped), which the two
    # above do not; ~2 s.
    "tests/test_generated_docs_fresh.py",
]


class GitError(Exception):
    """git could not answer; drift is unknown, never zero."""


def _run(cmd: list[str]) -> int:
    return subprocess.run(cmd, cwd=_ROOT).returncode


def _regenerate() -> bool:
    """Run all generators. Returns True on success."""
    for gen in _GENERATORS:
        rc = _run([_PY, *gen])
        if rc != 0:
            print(f"[drift] generator failed: {' '.join(gen)} (exit {rc})", file=sys.stderr)
            return False
    return True


def _git_drift() -> list[str]:
    """Generated paths git reports as modified, deleted or UNTRACKED.

    `git diff` alone misses a page a generator newly created (untracked), so
    this reads `git status --porcelain`. Raises GitError when git fails.
    """
    cmd = ["git", "status", "--porcelain", "--untracked-files=all", "--", *_GENERATED_PATHS]
    try:
        out = subprocess.run(cmd, cwd=_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise GitError(f"git not found on PATH ({e})") from e
    if out.returncode != 0:
        raise GitError(f"`{' '.join(cmd)}` exited {out.returncode}: {out.stderr.strip()}")
    return [line[3:] for line in out.stdout.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--fix",
        action="store_true",
        help="regenerate and leave changes in the working tree (don't fail on drift — for interactive fixing)",
    )
    args = parser.parse_args(argv)

    if not _regenerate():
        return 2

    try:
        drift = _git_drift()
    except GitError as e:
        print(f"[drift] cannot measure drift: {e}", file=sys.stderr)
        return 2
    if drift and not args.fix:
        print("[drift] generated artifacts are STALE vs bin/mcp_tool_catalog.py:", file=sys.stderr)
        for p in drift:
            print(f"          {p}", file=sys.stderr)
        print(
            "[drift] they have been regenerated in your working tree. Also update "
            "any hardcoded 'N tools' counts in README/COMPARISON/MYTHS_AND_FACTS/"
            "files_memory, then stage + commit and re-run.",
            file=sys.stderr,
        )
        return 1

    # Drift tests also gate the prose 'N tools' counts the generators don't own.
    # Run pytest with plugin autoload disabled (these tests need neither anyio
    # nor asyncio) and the cache provider off — both shave fixed startup cost
    # off a check that runs on the push hot path.

    from m3_core.seams import childenv

    env = childenv.child_env(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    rc = subprocess.run(
        [_PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", *_DRIFT_TESTS],
        cwd=_ROOT,
        env=env,
    ).returncode
    if rc != 0:
        print(
            f"[drift] drift tests failed (pytest exit {rc}) — a hardcoded 'N tools' "
            "count, the committed manifest, or a docs/tools/ page is out of sync. "
            "Regenerate with: python bin/check_tool_catalog_drift.py --fix, update "
            "the counts the test names, commit, and re-run.",
            file=sys.stderr,
        )
        return 1

    if args.fix and drift:
        print("[drift] regenerated; review/stage the changes.", file=sys.stderr)
        return 0

    print("[drift] tool catalog and generated docs are in sync.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
