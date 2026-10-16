"""deep_sync.py — commit and push an EXPLICIT list of paths in this checkout.

Housekeeping (bin/cleanup_logs.sh when present), then stage only the paths
named on the command line, commit them if anything changed, and push to the
remote and branch named on the command line. There are no defaults for any of
the three: the old form ran ``git add .`` over the whole payload and pushed to
``origin main``, and in an m3-memory checkout ``origin`` is the PUBLIC repo.

Exit codes: 0 committed and pushed, or nothing changed in the named paths;
1 any git step failed (its stderr is logged).

Usage:
    python bin/deep_sync.py --remote private --branch main reports/ notes.md
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime

logger = logging.getLogger("deep_sync")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = BASE_DIR


def _no_window() -> dict:
    """No console window per git call on Windows (the OS seam owns the flag)."""
    try:
        sys.path.insert(0, os.path.join(BASE_DIR, "bin"))
        from m3_core.seams import hidden_window_kwargs

        return dict(hidden_window_kwargs())
    except ImportError:
        return {}


def run_git(args: list[str]) -> subprocess.CompletedProcess:
    """Run ``git -C WORKSPACE <args>``, capturing output; never raises on a
    non-zero exit (the caller reads returncode)."""
    # Fixed argv; git resolved from PATH by design.
    return subprocess.run(  # nosec B603 B607
        ["git", "-C", WORKSPACE, *args], capture_output=True, text=True, check=False, **_no_window()
    )


def _fail(step: str, proc: subprocess.CompletedProcess) -> int:
    logger.error(
        f"Deep-Sync FAILED at `git {step}` (exit {proc.returncode}) in {WORKSPACE}: "
        f"{(proc.stderr or proc.stdout or '').strip() or '(no output)'}"
    )
    return 1


def sync(paths: list[str], remote: str, branch: str) -> int:
    logger.info(f"Starting Deep-Sync of {paths} -> {remote}/{branch}")

    cleanup_script = os.path.join(WORKSPACE, "bin", "cleanup_logs.sh")
    if os.path.exists(cleanup_script):
        try:
            subprocess.run([cleanup_script], check=True, stdout=subprocess.DEVNULL, **_no_window())  # nosec B603
            logger.info("Housekeeping pass complete.")
        except (OSError, subprocess.CalledProcessError) as e:
            logger.warning(f"Housekeeping pass did not run ({type(e).__name__}: {e}); continuing with the sync.")

    # Only the named paths, never `.`: everything else in the tree stays unstaged.
    proc = run_git(["add", "--", *paths])
    if proc.returncode != 0:
        return _fail("add", proc)

    # Staged-diff check BEFORE commit: a failed commit used to read as "No changes".
    staged = run_git(["diff", "--cached", "--name-only", "--", *paths])
    if staged.returncode != 0:
        return _fail("diff --cached", staged)
    changed = [line for line in staged.stdout.splitlines() if line.strip()]
    if not changed:
        logger.info(f"No changes in {paths}; nothing committed or pushed.")
        return 0

    msg = f"Deep-Sync: {datetime.now(UTC).isoformat(timespec='seconds')}"
    proc = run_git(["commit", "-m", msg, "--", *paths])
    if proc.returncode != 0:
        return _fail("commit", proc)
    logger.info(f"Committed {len(changed)} file(s): {changed[:20]}")

    proc = run_git(["push", remote, f"HEAD:{branch}"])
    if proc.returncode != 0:
        return _fail(f"push {remote} HEAD:{branch}", proc)
    logger.info(f"Deep-Sync complete: pushed to {remote}/{branch}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s: [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Commit and push an explicit list of paths in this checkout.")
    ap.add_argument("paths", nargs="+", help="paths to stage (relative to the checkout); nothing else is staged")
    ap.add_argument("--remote", required=True, help="remote to push to (check it is the one you mean)")
    ap.add_argument("--branch", required=True, help="remote branch to push HEAD to")
    args = ap.parse_args(argv)
    return sync(args.paths, args.remote, args.branch)


if __name__ == "__main__":
    sys.exit(main())
