"""
mcp-memory CLI — entry point for the M3 Memory MCP server.

Usage:
    mcp-memory                     Start the MCP server (stdio transport)
    mcp-memory --version           Print version and exit
    mcp-memory --help              Show this help

    mcp-memory install-m3          Fetch the m3-memory system payload from
                                   GitHub into the M3 root directory
                                   (default: ~/.m3-memory/repo)
    mcp-memory update              Re-fetch the payload for the current
                                   wheel version
    mcp-memory uninstall           Remove the cloned payload and config
    mcp-memory doctor              Print diagnostic info
"""

import argparse
import os
import sys
from pathlib import Path  # noqa: F401 - used in _auto_install return-type comment
from typing import cast, override

from m3_memory import _bin, _config
from m3_memory._platform import is_elevated, is_windows


def _ensure_utf8() -> None:
    """Guarantee the whole m3 process tree runs in Python UTF-8 mode.

    On Windows the default console code page is cp1252, and both stdio AND
    open() default to it. Any non-cp1252 character (em-dashes, arrows,
    box-drawing, emoji) then crashes with UnicodeEncodeError on print or
    UnicodeDecodeError on a no-encoding open(). `sys.stdout.reconfigure`
    (below) only fixes stdio — NOT open() defaults, and not the bin/ scripts
    this CLI execs/runpys. True UTF-8 mode (PEP 540) fixes all of it, but the
    interpreter reads it only at startup, so we set PYTHONUTF8 and re-exec
    once with -X utf8. The whole tree (CLI, in-process bridge, runpy'd bin
    scripts, child subprocesses inheriting the env) is then UTF-8.

    Safety: no-op if already in UTF-8 mode; a sentinel env var bounds the
    re-exec to exactly once so it can never loop.

    KNOWN LIMITATION — `python -c "<inline code>"` launches:
    Re-execing an inline `-c` payload can mangle on Windows because the OS
    re-quotes the program string when the process image is replaced; a
    multi-statement `-c` string may come back as a SyntaxError in the re-exec'd
    child. This does NOT affect any real m3 launch — the `m3` / `mcp-memory`
    console scripts launch a file path, and `python -m m3_memory.cli` and
    direct file-path launches all re-exec cleanly (verified). The `-c` form is
    only used for ad-hoc one-liners that import this module, which is not a
    supported entry path. If you must run such a one-liner and hit it, either
    set `PYTHONUTF8=1` in the env first (then _ensure_utf8 short-circuits, no
    re-exec) or write the code to a file and run that.
    """
    if sys.flags.utf8_mode:  # already -X utf8 or PYTHONUTF8=1
        return
    if os.environ.get("_M3_UTF8_REEXEC") == "1":  # re-exec already happened — stop
        return
    os.environ["PYTHONUTF8"] = "1"
    os.environ["_M3_UTF8_REEXEC"] = "1"
    # Rebuild the ORIGINAL launch faithfully. sys.orig_argv (3.10+) captures the
    # full command incl. the -c/-m/file form and interpreter flags; sys.argv
    # alone drops the launch form (re-exec'ing it breaks `python -c`/`-m`).
    # Insert -X utf8 right after the executable, preserving everything else.
    orig = list(getattr(sys, "orig_argv", [sys.executable, *sys.argv]))
    if not orig:  # defensive — shouldn't happen
        orig = [sys.executable, *sys.argv]
    new_argv = [orig[0], "-X", "utf8", *orig[1:]]

    # POSIX: os.execv truly REPLACES this process image — the child IS us, so
    # its exit code is the process exit code. Nothing to propagate.
    #
    # Windows: there is no real exec. Python emulates os.execv by SPAWNING a
    # child and returning control to the parent, which then exits 0 by falling
    # through — silently rewriting EVERY non-zero child exit code (argparse
    # errors, destructive-gate refusals, impl failures) to 0. That violates §3
    # ("fail loud, never silent"). So on Windows we run the re-exec'd command as
    # a real subprocess and propagate its exit code explicitly.
    if is_windows():
        import subprocess

        try:
            completed = subprocess.run(new_argv)
        except OSError:
            # Spawn failed (exotic launcher / permissions). Fall through — the
            # reconfigure block below still fixes stdio, the common case.
            return
        sys.exit(completed.returncode)
    else:
        try:
            os.execv(sys.executable, new_argv)
        except OSError:
            # Re-exec failed (rare). Fall through — the reconfigure block below
            # still fixes stdio, the common case.
            pass


_ensure_utf8()

# Belt-and-suspenders for stdio when re-exec didn't run (already-UTF8, sentinel
# set, or re-exec failed). Python 3.7+ .reconfigure() exists on the real
# TextIOWrapper; during tests pytest substitutes a plain StringIO that doesn't,
# so guard with hasattr.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")


def _auto_install(interactive: bool) -> "Path | None":
    """Fetch the m3-memory system payload without an explicit `install-m3`.

    Called from `_run_bridge()` when the bridge resolves to None. Behavior
    depends on whether we're talking to a human:

    - Interactive TTY: ask for confirmation, since auto-fetching a GitHub
      repo on first run is surprising enough to deserve a prompt.
    - Non-interactive (piped stdin, launched as an MCP subprocess, CI):
      auto-fetch silently and log to stderr. Prompting would deadlock
      the parent process waiting for input that will never come.

    Respects M3_AUTO_INSTALL=0 as a hard opt-out for either mode.
    Returns the resolved bridge path on success, None on refusal/failure.
    """
    # Look up via attribute (not `from ... import`) so tests can monkeypatch
    # `m3_memory.installer.install_m3` and have it take effect here.
    from m3_memory import installer

    if not _config.setting("install.auto_install"):  # M3_AUTO_INSTALL=0
        return None

    dest = installer.default_repo_path()
    if interactive:
        print(
            f"m3-memory system payload not found locally.\nFetch it from GitHub into {dest} now? [Y/n] ",
            end="",
            flush=True,
        )
        try:
            reply = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()  # newline after ^C / ^D
            return None
        if reply and reply not in ("y", "yes"):
            return None
    else:
        print(
            "[m3-memory] system payload not found; auto-fetching from GitHub "
            f"into {dest} (set M3_AUTO_INSTALL=0 to disable).",
            file=sys.stderr,
            flush=True,
        )

    try:
        return installer.install_m3()
    except RuntimeError as e:
        print(f"[m3-memory] auto-install failed: {e}", file=sys.stderr)
        return None


def _run_bridge() -> None:
    """Locate and execute the MCP server bridge.

    If no bridge can be resolved, try to auto-install the payload before
    giving up. See `_auto_install` for interactive vs non-interactive
    semantics.
    """
    from m3_memory.installer import config_file, find_bridge

    # Trigger self-healing for local embedder if it moved
    if _resolve_bin_script("setup_embedder.py"):
        _run_bin_script("setup_embedder.py", ["--heal"])

    bridge = find_bridge()
    if bridge is None:
        # Nothing on disk, no env override, no sibling — try to auto-install.
        bridge = _auto_install(interactive=sys.stdin.isatty())

    if bridge is None:
        print(
            "Error: m3-memory system payload is not installed.\n"
            "\n"
            "Run this once to fetch it:\n"
            "    m3 install-m3\n"
            "\n"
            f"Or, if you already have a clone, set M3_PATH_BIN to its bin/ directory or edit\n"
            f"    {config_file()}\n"
            "\n"
            "(Auto-install is gated by M3_AUTO_INSTALL; set M3_AUTO_INSTALL=0\n"
            "to permanently disable the prompt, or leave unset to accept.)\n",
            file=sys.stderr,
        )
        sys.exit(1)

    # Put bin/ on sys.path so the bridge's siblings (memory_core, etc.) import.
    _bin.ensure_on_path()  # bin/ (where bridge lives) on sys.path

    # Execute the bridge in this process - equivalent to `python memory_bridge.py`,
    # not dynamic code from an untrusted source; it's a file path from our own
    # config (or an explicit env var, or a sibling of this package).
    with open(bridge, encoding="utf-8") as f:
        code = compile(f.read(), str(bridge), "exec")
    exec(code, {"__file__": str(bridge), "__name__": "__main__"})  # nosec B102


def _cmd_install_m3(args: argparse.Namespace) -> int:
    from m3_memory.installer import install_m3

    try:
        install_m3(
            force=args.force,
            tag=args.tag,
            interactive=(False if args.non_interactive else None),
            endpoint=args.endpoint,
            capture_mode=args.capture_mode,
            cognitive_loop=args.cognitive_loop,
            db_backend=args.db_backend,
        )
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def _cmd_reinstall(args: argparse.Namespace) -> int:
    """Wipe and reinstall the system payload (alias for install-m3 --force)."""
    from m3_memory.installer import install_m3

    try:
        install_m3(force=True, interactive=sys.stdin.isatty())
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def _warn_if_pypi_newer() -> None:
    """Nudge toward `pipx upgrade` when PyPI has a newer release than the installed
    package. `m3 update` only RE-SYNCS the payload for the INSTALLED version — it
    does NOT upgrade the package (a common surprise for pipx installs). Best-effort:
    silent on any network/parse error, short timeout so it never stalls the command."""
    try:
        import json as _json
        import urllib.request as _url
        from importlib.metadata import version as _pkgver

        installed = _pkgver("m3-memory")
        # Hardcoded https:// literal — no user input, so no file:/custom scheme
        # can reach urlopen. Same justification (and marker) as every other
        # urlopen in this repo; see bin/doctor/*_probe.py.
        with _url.urlopen("https://pypi.org/pypi/m3-memory/json", timeout=2.5) as r:  # noqa: S310  # nosec B310
            latest = _json.load(r)["info"]["version"]

        def _key(v: str) -> list:
            return [int(p) for p in v.split(".") if p.isdigit()]

        if _key(latest) > _key(installed):
            # The upgrade line is OS-specific ON PURPOSE. A bare
            # `pipx upgrade m3-memory` is unsafe on Windows in two ways, both
            # observed on a real install (2026-08-10):
            #
            #  1. pip serves the index page from its HTTP cache, so pipx reports
            #     "already at latest version" while PyPI is serving a newer one.
            #     --no-cache-dir is the only reliable way to see a fresh release.
            #  2. pip UNINSTALLS before it installs. Windows holds an open .exe
            #     against deletion, so with m3 running (MCP servers, cognitive
            #     loop, dashboard, embed server) the upgrade deletes the package
            #     and then fails on `[WinError 32] ... Scripts\\m3.exe`, leaving
            #     NO m3 installed: `m3` then dies with ModuleNotFoundError. pip
            #     also leaves ~-prefixed junk dirs behind from the half-undone
            #     uninstall.
            #
            # `m3 stop` quiesces the writers first, which makes the upgrade a
            # normal one. POSIX replaces a running binary's inode happily, so
            # the plain form stays correct there.
            # `m3 upgrade` now owns every caveat above: it DETECTS the install
            # method (pipx / pip / pip --user / plugin) instead of assuming one,
            # stops the writers, upgrades, re-wires and verifies — identically
            # on all three OSes. Naming a specific package manager here was the
            # bug: `pipx upgrade m3-memory` against a PIP install exits 0 having
            # upgraded NOTHING, which reads as success.
            upgrade = "       m3 upgrade      # detects pipx/pip, stops, upgrades, verifies"
            print(
                f"\n[m3] A newer release is on PyPI: {latest} (you have {installed}).\n"
                f"     `m3 update` only re-syncs the payload for your INSTALLED version —\n"
                f"     it does NOT upgrade the package. To upgrade (any OS, any\n"
                f"     install method):\n"
                f"{upgrade}",
                file=sys.stderr,
            )
    except Exception:  # noqa: BLE001 — advisory only, never fail the command
        pass


def _offer_rust_core_upgrade(
    *,
    interactive: bool,
    assume_yes: bool = False,
    skip: bool = False,
) -> None:
    """Offer to upgrade the native m3-core-rs wheel when it is behind the pin.

    `m3 update` re-syncs the PAYLOAD; nothing in that path has ever touched the
    native core (only `install_os.py` and `m3 embedder install-gpu` call
    install_rust_core). So a user who runs `pipx upgrade m3-memory` and then
    `m3 update` lands on NEW Python code with an OLD Rust core, silently: the
    version skew is invisible and there is no prompt telling them a newer core
    exists.

    That skew is degraded, not broken -- memory/tokens.py cascades
    Rust -> caller exact_fn -> estimator, so an old wheel simply falls back to
    the estimator and stays correct. But the user quietly loses the exact
    count_tokens path they upgraded to get, which is precisely the kind of
    "it says it upgraded, so it must be fixed" trust gap that costs us.

    Default is YES: the pin names a Release that should carry every backend for
    this version's interpreter set, so accepting is the safe, expected answer.
    Declining is non-destructive -- the tier-2 HTTP fallback keeps embeddings
    working.

    ⚠ That "should" is doing real work, and it is the releaser's job to make it
    true. Do NOT restate it as a fixed count: the set changes (v2026.9.16 was
    7 x cp311-314; 3.10.1 is 7 x cp312-315, no cp311),
    and a count is not the property that matters anyway. v2026.9.16 shipped 28
    assets while carrying only SIX backends -- windows_cuda was missing and a
    generically-named m3_core_rs-* set filled the count. Since the cascade
    matches assets BY FILENAME, that is a hard miss for a Windows+NVIDIA host,
    not a fallback. Verify a release by confirming all seven
    m3_core_rs_<os>_<backend> prefixes are present, never by counting assets.

    Best-effort and advisory: any import/probe failure is swallowed, exactly
    like _warn_if_pypi_newer. NEVER prompts when stdin is not a TTY -- a
    scripted `m3 update` must not block on input, and a hung prompt in CI is a
    worse failure than a stale core.
    """
    try:
        from m3_memory.rust_core_install import (
            M3_CORE_RS_VERSION,
            installed_rust_core_version,
            is_rust_core_current,
        )

        if is_rust_core_current():
            return  # already at or ahead of the pin — say nothing
        if skip:
            return  # caller opted out entirely (--no-core)

        have = installed_rust_core_version() or "not installed"
        print(
            f"\n[m3] The native Rust core is behind this release: "
            f"{have} installed, {M3_CORE_RS_VERSION} expected.\n"
            f"     `m3 update` re-syncs the payload only — it does not replace "
            f"the native wheel.\n"
            f"     Embeddings still work (tier-2 fallback), but you are missing "
            f"this version's core.",
            file=sys.stderr,
        )

        if assume_yes:
            print(f"     Installing m3-core-rs {M3_CORE_RS_VERSION} (--yes-core).", file=sys.stderr)
        elif not interactive:
            # Non-TTY (CI, piped, hook): report and leave. Blocking a scripted
            # `m3 update` on a prompt is a worse failure than a stale core.
            print(
                "     Install it with:  m3 embedder install-gpu   (or `m3 update --yes-core`)",
                file=sys.stderr,
            )
            return
        else:
            try:
                ans = input(f"  Install m3-core-rs {M3_CORE_RS_VERSION} now? [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                print("     Skipped. Install later with:  m3 embedder install-gpu", file=sys.stderr)
                return
            if ans in ("n", "no"):
                print("     Skipped. Install later with:  m3 embedder install-gpu", file=sys.stderr)
                return

        from m3_memory.rust_core_install import install_rust_core

        # allow_source_fallback=False for the same reason install_os.py sets it:
        # `m3 update` must not silently launch a multi-minute Rust+cmake build.
        # If no prebuilt matches, install_rust_core prints the manual recipe.
        rc = install_rust_core(allow_source_fallback=False)
        if rc != 0:
            print(
                f"\n[m3] Native core install did not complete (exit {rc}). "
                f"Embeddings continue via the tier-2 fallback.",
                file=sys.stderr,
            )
    except Exception:  # noqa: BLE001 — advisory only, never fail the command
        pass


def _cmd_stop(args: argparse.Namespace) -> int:
    """`m3 stop` — quiesce every m3 DB-writer for this engine root.

    Exists because `pipx upgrade m3-memory` is unsafe while m3 is running on
    Windows: pip uninstalls before it installs, Windows holds an open .exe
    against deletion, and the upgrade therefore deletes the package and then
    fails on `[WinError 32] ... Scripts\\m3.exe` — leaving NO m3 installed.
    Stopping the writers first turns that into an ordinary upgrade.

    Also the honest answer to "how do I shut m3 down", which previously had
    none: users reached for `taskkill`/`pkill` by name, which is the
    substring-kill footgun kill_stale_daemons exists to avoid.

    Delegates to m3_halt.kill_stale_daemons: precise-PID only, never a
    name sweep, never its own pid or parent chain.
    """
    script = _resolve_bin_script("m3_halt.py")
    if script is None:
        print("Error: m3_halt.py not found (payload not installed?). Run `mcp-memory install-m3`.", file=sys.stderr)
        return 1
    _bin.ensure_on_path()  # bin/ (where script lives) on sys.path
    import m3_halt as halt

    # Supervised daemons are stopped through their service manager first;
    # killing them alone lets systemd/launchd start them again.
    services: list = []
    keep_roles: list = []
    if not is_windows():
        try:
            import install_schedules  # type: ignore[unused-ignore]

            # The Rust embed server stays up (see keeps_rust_embed_server).
            if install_schedules.keeps_rust_embed_server():
                keep_roles = ["embed-server"]
            services = install_schedules.stop_unix_services()
        except Exception as e:  # noqa: BLE001 — the PID reap below still runs
            print(
                f"  [!] could not stop m3's services ({type(e).__name__}: {e}); supervised daemons may restart.",
                file=sys.stderr,
            )
        from m3_memory.wizard.ui import called_by_upgrade

        if services and called_by_upgrade():
            # The upgrade's setup step restarts them two steps later.
            print(f"  stopped {len(services)} m3 service(s); setup restarts them")
        else:
            for name in services:
                print(f"  stopped service {name}")
            if services:
                print("[m3] services stay stopped until `m3 setup` starts them.")

    results = halt.kill_stale_daemons(timeout=getattr(args, "timeout", 8.0), exclude_roles=keep_roles)
    if not results:
        from m3_memory.wizard.ui import called_by_upgrade

        # --quiet: silent when nothing ran. Under the upgrade, the services
        # line above already says what happened.
        if not getattr(args, "quiet", False) and not (services and called_by_upgrade()):
            print(
                "[m3] no other m3 DB-writers running."
                if services
                else "[m3] nothing to stop — no m3 DB-writers running."
            )
        return 0

    killed = [r for r in results if r.get("killed")]
    survived = [r for r in results if not r.get("killed")]
    # A survivor that holds no store (the embed server) cannot block an
    # upgrade; report it as left running, not as a failed stop.
    left = [r for r in survived if not halt.holds_store(r.get("role", ""))]
    failed = [r for r in survived if r not in left]
    from m3_memory.wizard.ui import called_by_upgrade

    in_upgrade = called_by_upgrade()  # the tally below names them instead
    for r in [] if in_upgrade else killed:
        print(f"  stopped {r.get('role', '?')} (pid {r.get('pid')})")
    quiet = getattr(args, "quiet", False)
    for r in [] if quiet else left:
        print("  " + halt.describe_left_running(r.get("role", "?"), r.get("pid"), r.get("error")))
    for r in failed:
        print(
            f"  [!] could NOT stop {r.get('role', '?')} (pid {r.get('pid')}): {r.get('error') or 'unknown'}",
            file=sys.stderr,
        )

    note = f"; {len(left)} left running (no database)" if left else ""
    names = (" (" + ", ".join(r.get("role", "?") for r in killed) + ")") if in_upgrade and killed else ""
    if not (quiet and not killed and not failed):  # --quiet: silent when nothing was stopped
        print(f"[m3] stopped {len(killed)}/{len(results) - len(left)} writer(s){names}{note}.")
    if failed:
        # Almost always an elevated writer an unprivileged shell cannot touch.
        # Say so and exit non-zero — a partial stop must not read as success
        # (§3), because the caller's next step is an upgrade that will fail.
        print(
            "[m3] some writers survived — an upgrade may still hit a file lock. Re-run from an elevated shell.",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_upgrade(args: argparse.Namespace) -> int:
    """`m3 upgrade` — upgrade the PACKAGE, using the right command for how it
    was actually installed.

    Thin on purpose. The work lives in ``bin/m3_upgrade.py`` and is executed in
    a FRESH PROCESS that never imports ``m3_memory``, because the upgrade
    replaces the very package this function is running from. On Windows that is
    a file-locking failure, not a theoretical one: pip uninstalls before it
    installs, Windows holds an open ``.exe`` against deletion, and the upgrade
    deletes the package and then fails -- leaving NO m3 installed.

    So this is a LAUNCHER, not an implementation. Doing the upgrade inline here
    would reintroduce exactly the hazard the standalone script exists to avoid.

    Residual hazard: this process WAITS for the script, and its image is the
    venv's ``Scripts\\m3.exe`` whenever ``~/.local/bin/m3.exe`` is a symlink
    (pipx symlinks when Windows allows it). The script's lock gate detects that,
    stops before changing anything, and prints the Python command that avoids it.

    Why the command exists at all: the script shipped in the payload but nothing
    told anyone it was there. `m3 --help`, the README and HOW-TO-UPGRADE all
    pointed at bare `pipx upgrade m3-memory` -- which, against a pip install,
    exits 0 having upgraded NOTHING and reads as success. A correct tool nobody
    can find does not prevent that; a discoverable one does.
    """
    import subprocess  # nosec B404 - relaunches our own script, argv list, no shell

    # bin_dir() is the SINGLE owner of "where is the payload's bin/?" -- it
    # already handles both layouts (a dev checkout has bin/ at the repo root; an
    # installed wheel has it under m3_memory/bin/) and honours $M3_PATH_BIN.
    # A second resolver here would drift from it (§10a); a hand-rolled
    # `__file__/../bin` was wrong in the dev tree on the first try.
    from m3_memory.installer import bin_dir

    bd = bin_dir()
    script = (bd / "m3_upgrade.py") if bd else None
    if script is None or not os.path.isfile(script):
        print(
            f"Error: the upgrade helper is missing from this install "
            f"(looked in {bd or 'no resolvable bin/ directory'}). Reinstall "
            f"m3, or upgrade manually: "
            f"`m3 stop`, then your package manager's upgrade, then `m3 setup`.",
            file=sys.stderr,
        )
        return 1

    cmd = [_interpreter(), str(script)]
    if getattr(args, "dry_run", False):
        cmd.append("--dry-run")
    if getattr(args, "yes", False):
        cmd.append("--yes")
    if getattr(args, "skip_stop", False):
        cmd.append("--skip-stop")
    if getattr(args, "from_pypi", False):
        cmd.append("--from-pypi")
    if getattr(args, "stop_agents", False):
        cmd.append("--stop-agents")
    try:
        return subprocess.run(cmd).returncode  # nosec B603 - argv list, no shell
    except KeyboardInterrupt:
        print(chr(10) + "Upgrade interrupted.", file=sys.stderr)
        return 130


def _interpreter() -> str:
    """A real interpreter for relaunching our own script.

    `sys.executable` is the console-script SHIM (`m3.exe`) under a pipx/pip
    install, and handing that to subprocess re-enters this CLI with a .py path
    as its subcommand -- argparse rejects it and the child exits 2. The
    interpreter always lives beside the shim in Scripts/ or bin/.
    """
    exe = sys.executable or ""
    stem = os.path.splitext(os.path.basename(exe))[0].lower()
    if stem.startswith("python"):
        return exe
    if exe:
        here = os.path.dirname(exe)
        for name in ("python.exe", "python3", "python"):
            cand = os.path.join(here, name)
            if os.path.isfile(cand):
                return cand
    return exe or "python3"


def _cmd_update(args: argparse.Namespace) -> int:
    from m3_memory.installer import install_m3

    try:
        install_m3(force=True, tag=args.tag)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    _warn_if_pypi_newer()
    # The payload is now current but the NATIVE core is not covered by that
    # re-sync; offer it explicitly rather than leaving a silent version skew.
    _offer_rust_core_upgrade(
        interactive=sys.stdin.isatty(),
        assume_yes=getattr(args, "yes_core", False),
        skip=getattr(args, "no_core", False),
    )
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    from m3_memory.installer import uninstall_m3

    uninstall_m3(yes=args.yes)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """`m3 status` — a one-line health verdict (run `m3 doctor` for detail)."""
    from m3_memory.installer import status

    return status()


def _cmd_doctor(args: argparse.Namespace) -> int:
    from m3_memory.installer import doctor

    # Brief is the DEFAULT; --verbose opts into the full detail.
    verbose = getattr(args, "verbose", False)
    brief = not verbose
    code = doctor(fix=getattr(args, "fix", False), brief=brief)

    # Also run the project-specific doctor if payload is installed. Forward
    # --verbose so the payload probes match; it may already be in args.rest
    # (parse_known_args), so only add it if absent.
    if _resolve_bin_script("memory_doctor.py"):
        rest = list(getattr(args, "rest", []) or [])
        if verbose and "--verbose" not in rest:
            rest.append("--verbose")
        # Forward the repair flags: --fix is CONSUMED by this subparser (so it is
        # never in args.rest), and without forwarding it the payload doctor ran in
        # report mode — its whole repair pass (shared-embedder restart, agent
        # stale-payload rewrite, DB heal) silently never fired under `m3 doctor
        # --fix`. --fix-hooks gates the settings.json rewrite.
        if getattr(args, "fix", False) and "--fix" not in rest:
            rest.append("--fix")
        if getattr(args, "fix_hooks", False) and "--fix-hooks" not in rest:
            rest.append("--fix-hooks")
        if verbose:
            print("\n--- Project Payload Diagnostics ---")
        return _run_bin_script("memory_doctor.py", rest)

    return code


def _cmd_governor(args: argparse.Namespace) -> int:
    """Dispatch `m3 governor <status|migrate>` to bin/governor_cli.py."""
    if not _resolve_bin_script("governor_cli.py"):
        print("governor command requires the project payload (run `m3 install`).")
        return 1
    sub = getattr(args, "governor_cmd", None) or "status"
    argv = [sub]
    if sub == "migrate" and getattr(args, "yes", False):
        argv.append("--yes")
    return _run_bin_script("governor_cli.py", argv)


def _cmd_restore(args: argparse.Namespace) -> int:
    """`m3 restore FILE [--at TIME]`: list the versions a rewrite saved in
    FILE.backup_history, or write one back (itself backed up, so it can be undone)."""
    _bin.ensure_on_path()
    from m3_core.atomic_io import backup_paths, restore, versions

    args.file = str(Path(args.file).expanduser())
    found = versions(args.file)
    if not args.at:
        if not found:
            print(f"no saved versions of {args.file} (looked in {backup_paths(args.file)[1]})")
            return 1
        print(f"versions of {args.file} (each is the file as it was BEFORE that change):")
        for stamp, by, content in found:
            print(f"  {stamp}  before a change by {by}  ({len(content)} bytes)")
        print(f"restore one: m3 restore {args.file} --at <time>")
        return 0
    try:
        path = restore(args.file, args.at)
    except KeyError as e:
        print(f"observed: {e.args[0]}. inspect: m3 restore {args.file}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"observed: could not restore {args.file}: {e}; it is unchanged", file=sys.stderr)
        return 2
    print(
        f"restored {path} to the version before {args.at}; to undo, `m3 restore {path}` lists the version from just before this restore"
    )
    return 0


def _secret_set(args: argparse.Namespace) -> int:
    """`m3 secret set NAME [--keyring]`: ask for the value (hidden, twice) and
    store it in the secrets file, or with --keyring in the OS keyring (then the
    file points at it, replacing any value line). The value is never taken from
    the command line, and never printed."""
    import getpass

    _bin.ensure_on_path()
    from m3_core.secret_store import (
        SecretsFileError,
        get_secret,
        is_known_secret,
        read_secrets,
        secrets_file_path,
        set_secret,
        store_in_keyring,
    )

    from m3_memory._platform import console_has_person, volume_encryption

    name = args.name
    if not is_known_secret(name):
        print(
            f"observed: {name} is not one of m3's secrets. inspect: `m3 config show` lists them (the secrets section).",
            file=sys.stderr,
        )
        return 2
    if not console_has_person():
        print(
            f"observed: no one is at the console to type the value of {name}; nothing stored. "
            "inspect: run `m3 secret set` in a terminal (a value is never read from the command line).",
            file=sys.stderr,
        )
        return 2
    print(f"m3 now asks for the value of {name}. Typing is hidden; nothing is shown.")
    value = getpass.getpass(f"Value for {name}: ").strip()
    if not value:
        print("observed: the value was empty; nothing stored.", file=sys.stderr)
        return 2
    if getpass.getpass(f"Again, to confirm {name}: ").strip() != value:
        print("observed: the two entries differ; nothing stored. Run the command again.", file=sys.stderr)
        return 2
    try:
        if args.keyring:
            enc = volume_encryption(str(secrets_file_path()))
            store = enc.keyring if enc is not None else "the OS keyring"
            had_value = name in read_secrets()
            where = store_in_keyring(name, value)
            print(f"stored {name} in {store} (read back OK).")
            print(
                f"{secrets_file_path()} now lists {name} as {where.removeprefix('keyring:')} in the keyring"
                + ("; its value line there was replaced." if had_value else ".")
            )
        else:
            path = set_secret(name, value)
            if get_secret(name) != value:
                raise SecretsFileError(f"{path}: wrote {name} but the read-back returned a different value")
            print(f"stored {name} in the secrets file {path} (read back OK).")
    except (OSError, SecretsFileError) as e:
        print(f"observed: {e}; inspect: `m3 doctor` (the secrets checks).", file=sys.stderr)
        return 2
    return 0


# `m3 batch`: commands it refuses, by reason.
_BATCH_REFUSED: dict[str, str] = {
    **dict.fromkeys(("setup",), "it asks questions, and a batch's stdin is the batch"),
    **dict.fromkeys(("serve", "dashboard"), "it runs until stopped"),
    **dict.fromkeys(
        ("install-m3", "reinstall", "update", "upgrade", "uninstall", "stop"),
        "it changes or stops the m3 installation the batch is running on",
    ),
    "batch": "batches do not nest",
}


class _BatchLineError(ValueError):
    """A batch line that is refused before it runs; the message never echoes a value."""


def _batch_argv(line: str, n: int) -> list[str]:
    """The arguments of batch line ``n``: a JSON array when the line starts with
    '[', else command text split by POSIX shell-word rules (no shell sees stdin,
    so the same rules hold on every OS)."""
    import json
    import shlex

    text = line.strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise _BatchLineError(f"line {n}: not a JSON array ({e.msg} at column {e.colno})") from None
        if not (isinstance(parsed, list) and all(isinstance(a, str) for a in parsed)):
            raise _BatchLineError(f"line {n}: a JSON line must be an array of strings")
        argv: list[str] = [str(a) for a in parsed]
    else:
        try:
            argv = shlex.split(text, posix=True)
        except ValueError as e:
            raise _BatchLineError(f"line {n}: {e} (an unclosed quote?)") from None
    if not argv:
        raise _BatchLineError(f"line {n}: empty")
    for arg in argv:
        if arg in ("--set", "--config") or arg.startswith(("--set=", "--config=")):
            flag = arg.split("=", 1)[0]
            raise _BatchLineError(
                f"line {n}: {flag} is process-wide and would leak into the following lines; "
                f'give it once to the batch: m3 "{flag}=..." batch'
            )
    command = argv[0]
    why = _BATCH_REFUSED.get(command)
    if why is None and argv[:2] == ["secret", "set"]:
        why = "it asks for the secret, and a batch's stdin is the batch"
    if why is not None:
        raise _BatchLineError(f"line {n}: `{command}` is not run inside a batch: {why}; run it on its own")
    return argv


def _run_batch_line(argv: list[str]) -> tuple[int, str, str]:
    """Run one command in this process, as `m3 <argv>` would, capturing what it
    writes at the FILE-DESCRIPTOR level: a child process inherits the
    descriptors, and its output would otherwise bypass sys.stdout and corrupt
    the batch's JSON Lines. A command that exits does not end the batch."""
    import io
    import tempfile
    from contextlib import redirect_stderr, redirect_stdout

    saved_argv = sys.argv
    sys.stdout.flush()
    sys.stderr.flush()
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        saved_fds = os.dup(1), os.dup(2)
        os.dup2(out_f.fileno(), 1)
        os.dup2(err_f.fileno(), 2)
        out_s, err_s = io.StringIO(), io.StringIO()
        code = 0
        try:
            with redirect_stdout(out_s), redirect_stderr(err_s):
                sys.argv = ["m3", *argv]
                try:
                    main()
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
                except Exception as e:  # noqa: BLE001 -- one line's failure is reported, the batch goes on
                    print(f"m3 batch: the command failed: {type(e).__name__}: {e}", file=sys.stderr)
                    code = 1
        finally:
            sys.argv = saved_argv
            os.dup2(saved_fds[0], 1)
            os.dup2(saved_fds[1], 2)
            os.close(saved_fds[0])
            os.close(saved_fds[1])
        out_f.seek(0)
        err_f.seek(0)
        fd_out = out_f.read().decode("utf-8", errors="replace")
        fd_err = err_f.read().decode("utf-8", errors="replace")
    return code, out_s.getvalue() + fd_out, err_s.getvalue() + fd_err


def _cmd_batch(args: argparse.Namespace) -> int:
    """`m3 batch [--text] [--stop-on-error]`: run the commands on stdin, one per
    line, in this one process (one start-up for all of them)."""
    import json

    lines = sys.stdin.read().splitlines()
    any_failed = False
    for n, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        command: list[str] | None
        try:
            command = _batch_argv(line, n)
        except _BatchLineError as e:
            command, code, out, err = None, 2, "", str(e)
        else:
            code, out, err = _run_batch_line(command)
        failed = code != 0
        any_failed |= failed
        if args.text:
            shown = " ".join(command or ["(refused)"])
            print(f"=== line {n}: {shown} (exit {code}) ===")
            if out:
                print(out.rstrip("\n"))
            if err:
                print(err.rstrip("\n"))
        else:
            print(json.dumps({"line": n, "command": command, "exit": code, "stdout": out, "stderr": err}), flush=True)
        if failed and args.stop_on_error:
            break
    return 1 if any_failed else 0


def _cmd_secret(args: argparse.Namespace) -> int:
    """`m3 secret refresh | set NAME [--keyring]`. refresh moves the secrets
    generation marker (m3's own writes do this already; this is for a key changed
    outside m3)."""
    if getattr(args, "secret_cmd", None) == "set":
        return _secret_set(args)
    if getattr(args, "secret_cmd", None) != "refresh":
        print("usage: m3 secret refresh | m3 secret set NAME [--keyring]", file=sys.stderr)
        return 2
    _bin.ensure_on_path()
    from m3_core.secret_store import bump_secret_generation

    p = bump_secret_generation()
    print(f"secrets refresh signalled ({p}); running m3 processes re-read within a few seconds.")
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    """Dispatch `m3 config show|get|set|unset` to bin/config_cli.py (always starts)."""
    if not _resolve_bin_script("config_cli.py"):
        print("config command requires the project payload (run `m3 install`).")
        return 1
    argv = [getattr(args, "config_cmd", None) or "show"]
    argv += [v for v in (getattr(args, "key", None), getattr(args, "value", None)) if v is not None]
    for flag in ("origin", "json", "yes", "dry_run", "non_interactive"):
        if getattr(args, flag, False):
            argv.append(f"--{flag.replace('_', '-')}")
    if getattr(args, "config", None):
        argv += ["--config", args.config]
    return _run_bin_script("config_cli.py", argv)


def _cmd_schedules(args: argparse.Namespace) -> int:
    """Dispatch `m3 schedules <verify|repair|list|add|remove>` to bin/install_schedules.py.

    The verify/repair tooling existed but was reachable ONLY by invoking the
    script by absolute path inside the installed payload — so the command a
    stuck user needs was, in practice, undiscoverable. A repair tool nobody can
    find is a repair tool that does not exist (observed 2026-07-27, when a
    drifted schedule survived an upgrade because nothing surfaced it).
    """
    if not _resolve_bin_script("install_schedules.py"):
        print("schedules command requires the project payload (run `m3 install`).")
        return 1
    sub = getattr(args, "schedules_cmd", None) or "verify"
    name = getattr(args, "name", None)
    match sub:
        case "verify":
            argv = ["--verify", name] if name else ["--verify"]
        case "repair":
            argv = ["--repair"]
        case "list":
            argv = ["--list"]
        case "add":
            argv = ["--add", name or "all"]
        case "remove":
            argv = ["--remove", name or "all"]
        case _:
            argv = ["--verify"]
    port = getattr(args, "port", None)
    if port:
        argv += ["--port", str(port)]
    # The config file this command located, unless it is the default: an elevated
    # child starts from the user's environment, not this one, and the tasks it
    # registers run without any environment of ours.
    from m3_memory._roots import located_config_args

    argv += located_config_args()

    # On Windows, --repair and --add/--remove need admin: registering a boot
    # (ONSTART) task, and deleting one, are both privileged. Running unelevated
    # is not merely useless here — it is DESTRUCTIVE for --repair, which deletes
    # each task and then fails to recreate it, taking a working service down
    # (observed twice on 2026-07-27). So elevate BEFORE running, rather than
    # letting the child fail and print a command to paste into another shell.
    # A missed UAC dialog re-asks; declining falls through to the unelevated run
    # so nothing becomes unreachable just because elevation was refused.
    if is_windows() and sub in ("repair", "add", "remove") and not is_elevated():
        from .wizard.ui import console_has_person

        if not console_has_person():
            # Nobody can answer the UAC question here; asking would wait for ever
            # (a hidden elevated run did, 2026-10-11), and running unelevated is
            # destructive for `repair`. Refuse, with the fix.
            print(
                f"  `m3 schedules {sub}` needs an admin shell on Windows, and no one is at this "
                f"console to approve the UAC prompt. fix: run it from an elevated PowerShell."
            )
            return 1
        script = _resolve_bin_script("install_schedules.py")
        if script and _elevate_schedules_windows(str(script), argv, sub):
            return 0
    return _run_bin_script("install_schedules.py", argv)


def _elevate_schedules_windows(script: str, argv: list, sub: str) -> bool:
    """UAC-elevate an install_schedules.py invocation. True iff it succeeded.

    Reuses setup_wizard's retry-aware elevation helpers so a UAC dialog that
    opens behind the terminal, lands on another monitor, or eats a reflexive
    Enter is re-offered rather than silently downgrading the command.
    """
    try:
        from .setup_wizard import _ask_yes_no, _retry_elevated
    except Exception:  # noqa: BLE001 — wizard unavailable: run unelevated
        return False

    if sub == "repair":
        print(
            "  `--repair` DELETES and recreates each task. Unelevated, the "
            "delete succeeds\n  and the recreate fails — taking a running "
            "service down. It needs admin."
        )
    if not _ask_yes_no("  Run it elevated now? (opens a Windows admin prompt)", default=True):
        return False

    from .elevate import ElevationBatch

    label = f"m3 schedules {sub}"
    batch = ElevationBatch()
    batch.add(label, [sys.executable, script, *argv])

    def _run() -> bool:
        res = batch.run()
        if res is None:
            return False
        r = res.get(label, {"rc": -1, "output": ""})
        if r["output"]:
            print(r["output"])
        return cast("bool", r["rc"] == 0)

    return _retry_elevated(_run, what=f"elevated `m3 schedules {sub}`")


def _cmd_wiki(args: argparse.Namespace) -> int:
    """Dispatch `m3 wiki <generate|compile|status>` to bin/gen_wiki.py."""
    if not _resolve_bin_script("gen_wiki.py"):
        print("wiki command requires the project payload (run `m3 install`).")
        return 1
    sub = getattr(args, "wiki_cmd", None) or "generate"
    argv = [sub]
    if getattr(args, "out", None):
        argv += ["--out", args.out]
    if sub == "generate":
        if getattr(args, "check", False):
            argv.append("--check")
        if getattr(args, "no_files", False):
            argv.append("--no-files")
        if getattr(args, "check_drift", False):
            argv.append("--check-drift")
        if getattr(args, "no_networkx", False):
            argv.append("--no-networkx")
        if getattr(args, "synthesize", False):
            argv.append("--synthesize")
        if getattr(args, "importance_threshold", None) is not None:
            argv += ["--importance-threshold", str(args.importance_threshold)]
        if getattr(args, "exclude", None):
            argv += ["--exclude", args.exclude]
        if getattr(args, "html", False):
            argv.append("--html")
        if getattr(args, "obsidian", False):
            argv.append("--obsidian")
    elif sub == "compile":
        if getattr(args, "importance_threshold", None) is not None:
            argv += ["--importance-threshold", str(args.importance_threshold)]
        if getattr(args, "no_networkx", False):
            argv.append("--no-networkx")
        if getattr(args, "scope", None):
            argv += ["--scope", args.scope]
        if getattr(args, "user_id", None):
            argv += ["--user-id", args.user_id]
        if getattr(args, "dry_run", False):
            argv.append("--dry-run")
    return _run_bin_script("gen_wiki.py", argv)


def _cmd_fips(args: argparse.Namespace) -> int:
    """Dispatch `m3 fips <install-wolfssl|status>`."""
    sub = getattr(args, "fips_cmd", None)
    if sub == "install-wolfssl":
        if not _resolve_bin_script("install_wolfssl.py"):
            print("fips install-wolfssl requires the project payload (run `m3 install`).")
            return 1
        argv: list = []
        if getattr(args, "ref", None):
            argv += ["--ref", args.ref]
        if getattr(args, "dest", None):
            argv += ["--dest", args.dest]
        if getattr(args, "print_sha", False):
            argv.append("--print-sha")
        return _run_bin_script("install_wolfssl.py", argv)
    # Default / `m3 fips status`: show the doctor crypto section.
    try:
        from m3_memory.installer import _crypto_section

        _crypto_section()
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"could not read crypto status: {e}")
        return 1


def _import_http_auth():
    """Import bin/m3_http_auth.py, putting bin/ on sys.path the way _run_bridge does."""
    from m3_memory.installer import find_bridge

    bridge = find_bridge()
    if bridge is not None:
        _bin.ensure_on_path()  # bin/ (where bridge lives) on sys.path
    import m3_http_auth  # noqa: PLC0415 — needs bin/ on sys.path first

    return m3_http_auth


def _cmd_serve_generate_token(args: argparse.Namespace) -> int:
    """Mint, store and display a bearer token, then exit WITHOUT starting the server.

    Deliberately does not fall through to serving: the token is printed once, and
    a process that then streams server logs into the same terminal invites it into
    a scrollback buffer, a CI log, or a screen share.
    """
    auth = _import_http_auth()

    existing, state = auth.resolve_token()
    if state is auth.TokenState.PRESENT_UNDECRYPTABLE and not args.force:
        # Overwriting here is the WORST case: the row replicates by version bump,
        # so a new token would propagate and orphan the one that still works on
        # whichever machine can decrypt it.
        print(
            f"Error: a {auth.SECRET_NAME} already exists but cannot be decrypted on "
            "this device.\n"
            "  Generating a new one would replicate it and may clobber the working "
            "token elsewhere.\n"
            "  Restore this device's salt (M3_AGENT_OS_SALT_HEX) or confirm "
            "AGENT_OS_MASTER_KEY is in the keyring.\n"
            "  Override only if you are sure:  m3 serve --generate-token --force",
            file=sys.stderr,
        )
        return 1
    if existing and not args.force:
        print(
            f"Error: a {auth.SECRET_NAME} is already configured.\n"
            "  Rotating it invalidates every connector using the old value.\n"
            "  Replace it deliberately:  m3 serve --generate-token --force",
            file=sys.stderr,
        )
        return 1

    token = auth.generate_token()
    try:
        from auth_utils import set_api_key

        set_api_key(auth.SECRET_NAME, token)
    except ValueError as e:
        # set_api_key raises when AGENT_OS_MASTER_KEY is absent from the keyring.
        # Surface the remediation, not a traceback.
        print(
            f"Error: could not store the token: {e}\n  The vault needs AGENT_OS_MASTER_KEY in your OS keyring.",
            file=sys.stderr,
        )
        return 1
    except Exception as e:  # noqa: BLE001 — any storage failure must not look like success
        print(f"Error: could not store the token: {e}", file=sys.stderr)
        return 1

    print("Generated a new m3 serve token (shown once):\n")
    print(f"    {token}\n")
    print("Add it to your Claude connector under Request headers:")
    print("    Header : Authorization")
    print(f"    Value  : Bearer {token}")
    print(
        "\nEnter the value EXACTLY as shown -- Claude sends it verbatim and adds no\n"
        '"Bearer " prefix of its own; a bare token is rejected with 401.'
    )
    # No env check here: a secret is never read from the environment,
    # so an exported M3_SERVE_TOKEN cannot override the token above; `m3 doctor`
    # names a leftover one.
    return 0


def _cmd_serve_show_token(args: argparse.Namespace) -> int:
    """Report whether a token is configured. Never prints the token itself."""
    auth = _import_http_auth()
    _token, state = auth.resolve_token()
    if state is auth.TokenState.OK:
        print(f"{auth.SECRET_NAME}: configured")
        return 0
    if state is auth.TokenState.PRESENT_UNDECRYPTABLE:
        print(
            f"{auth.SECRET_NAME}: PRESENT but NOT DECRYPTABLE on this device.\n"
            "  Most likely written on another machine (the vault replicates, the\n"
            "  encryption salt does not). Restore M3_AGENT_OS_SALT_HEX or check\n"
            "  AGENT_OS_MASTER_KEY.",
            file=sys.stderr,
        )
        return 1
    print(f"{auth.SECRET_NAME}: not configured (run: m3 serve --generate-token)")
    return 1


def _cmd_serve(args: argparse.Namespace) -> int:
    """Run the bridge with the streamable-http transport for claude.ai connectors.

    The default `mcp-memory` invocation is stdio (used by Claude Code, Gemini CLI,
    etc.). This subcommand starts the same bridge in HTTP mode so remote MCP
    clients (claude.ai web/desktop, Anthropic API mcp connector tool) can reach
    it after the user exposes the port via cloudflared / tailscale / ngrok / a
    reverse proxy.

    The HTTP transport REQUIRES a bearer token (`--generate-token`); it refuses to
    start without one, loopback included, because tunnels bind loopback and
    publish it.

    Flags win over env vars: each has an argparse default, which is always set, so
    the env var only applies when this function is bypassed (e.g. running
    memory_bridge.py directly under systemd/docker).
    """
    if getattr(args, "generate_token", False):
        return _cmd_serve_generate_token(args)
    if getattr(args, "show_token", False):
        return _cmd_serve_show_token(args)

    # Preflight HERE as well as in the bridge: this process can print a clean
    # message and return an exit code, whereas the bridge is exec'd and its
    # failure surfaces as a log line. The bridge keeps its own check because
    # `M3_TRANSPORT=http python memory_bridge.py` bypasses this path entirely.
    auth = _import_http_auth()
    host = args.host or "127.0.0.1"
    try:
        token, state = auth.resolve_token()
        auth.preflight(host, token, state)
    except auth.AuthConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    os.environ["M3_TRANSPORT"] = "http"
    if args.host:
        os.environ["M3_HTTP_HOST"] = args.host
    if args.port:
        os.environ["M3_HTTP_PORT"] = str(args.port)
    if args.path:
        os.environ["M3_HTTP_PATH"] = args.path
    if getattr(args, "public_host", None):
        os.environ["M3_HTTP_PUBLIC_HOST"] = ",".join(args.public_host)
    _run_bridge()
    return 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    """`m3 dashboard` — start the local web dashboard as a DETACHED background service.

    By default the server is launched WINDOWLESS and DETACHED (via pythonw on
    Windows / a new session on POSIX): no startup window, no flash, no periodic
    flashes, and it KEEPS RUNNING after you close the terminal. The command
    prints the URL and returns. Control it with:
      m3 dashboard            start (or report an already-running instance)
      m3 dashboard --stop     stop the running dashboard
      m3 dashboard --status   report URL + pid
      m3 dashboard --foreground   run in THIS process (debugging; blocks)

    Loopback-only by design (no authentication) — do not expose without auth.
    """
    # --stop / --status don't need the web deps (they only read the PID registry
    # / kill a pid), so handle them before the fastapi preflight.
    passthrough: list = []
    if getattr(args, "stop", False):
        passthrough = ["--stop"]
    elif getattr(args, "status", False):
        passthrough = ["--status"]
    else:
        # Starting a server DOES need the web deps — preflight for a clear message.
        missing = []
        for mod in ("fastapi", "uvicorn"):
            try:
                __import__(mod)
            except ModuleNotFoundError:
                missing.append(mod)
        if missing:
            print(
                "m3 dashboard needs the web dependencies "
                f"({', '.join(missing)} not installed).\n"
                "Install them with:\n"
                '    pip install "m3-memory[dashboard]"',
                file=sys.stderr,
            )
            return 1
        if getattr(args, "foreground", False):
            passthrough = ["--foreground"]

    if not _resolve_bin_script("dashboard_server.py"):
        print(
            "dashboard requires the project payload (run `m3 install`).",
            file=sys.stderr,
        )
        return 1

    if getattr(args, "host", None):
        passthrough += ["--host", args.host]
    if getattr(args, "port", None):
        passthrough += ["--port", str(args.port)]

    # runpy dashboard_server.py as __main__; its argparse handles the flags. The
    # default (no --foreground) detaches a windowless server and returns.
    return _run_bin_script("dashboard_server.py", passthrough)


def _resolve_bin_script(name: str) -> "Path | None":
    """Find bin/<name> relative to the resolved bridge (installed or dev).

    Returns an absolute Path or None if no bridge is resolvable (meaning
    install-m3 hasn't run and we're not in a dev checkout either).
    """
    from m3_memory.installer import find_bridge

    bridge = find_bridge()
    if bridge is None:
        return None
    candidate = bridge.parent / name
    return candidate if os.path.isfile(candidate) else None


def _run_bin_script(script_name: str, argv: list) -> int:
    """Execute bin/<script_name> as __main__ with the given argv.

    Uses runpy so the script's own argparse handles its flags unchanged.
    sys.argv is rewritten for the duration of the call so the script sees
    its own name in argv[0] (what it would see when invoked directly).
    """
    import runpy

    script = _resolve_bin_script(script_name)
    if script is None:
        print(
            f"Error: {script_name} not found.\nRun `mcp-memory install-m3` first.",
            file=sys.stderr,
        )
        return 1

    # Put bin/ on sys.path so the script's sibling imports (chatlog_config,
    # m3_sdk, ...) resolve the same way they do when invoked directly.
    _bin.ensure_on_path()  # bin/ (where script lives) on sys.path

    saved_argv = sys.argv
    sys.argv = [str(script), *list(argv)]
    try:
        try:
            runpy.run_path(str(script), run_name="__main__")
            return 0
        except SystemExit as e:
            # argparse / main() calls sys.exit — propagate the code VERBATIM.
            # NOTE (single-instance contract): a service run via `m3 <svc>
            # --foreground` may exit m3_halt.EXIT_ALREADY_RUNNING (4) meaning
            # "another instance already holds the lock". That is a real, honest
            # non-zero signal — do NOT translate it to 0 here. Callers/supervisors
            # that must tolerate it handle it at their layer (systemd
            # SuccessExitStatus=4, launchd KeepAlive:Crashed-only, doctor probes
            # confirm by port not by exit code). The interactive `m3 <svc>`
            # (background) path already returns 0 for "already up" itself.
            code = e.code
            return int(code) if isinstance(code, int) else (0 if code is None else 1)
    finally:
        sys.argv = saved_argv


def _cmd_enrich_pending(args: argparse.Namespace) -> int:
    """Execute the enrich-pending subcommand."""
    import asyncio
    import sys

    # Import here to avoid dependency issues if memory_core is not yet installed
    core = _resolve_bin_script("memory_core.py")
    if core is None:
        print("Error: memory_core.py not found. Run `mcp-memory install-m3`.", file=sys.stderr)
        return 1
    _bin.ensure_on_path()
    import memory_core

    limit = args.limit or 0
    allowed_variants = args.allowed_variant or []

    try:
        # First call: dry-run to get count and ETA
        result = asyncio.run(
            memory_core.enrich_pending_impl(
                dry_run=True,
                limit=limit,
                allowed_variants=allowed_variants,
            )
        )

        if isinstance(result, dict):
            count = result.get("count", 0)
            eta_seconds = result.get("est_wall_clock_seconds", 0)
            sample_ids = result.get("sample_ids", [])

            # Format wall-clock time
            minutes = int(eta_seconds // 60)
            seconds = int(eta_seconds % 60)
            eta_str = f"{minutes}m {seconds}s"

            print(f"{count} items pending, ETA {eta_str}")
            if sample_ids:
                print(f"Sample IDs: {', '.join(sample_ids[:3])}")

            # If --yes is set and not --no-confirm, ask for confirmation
            if args.yes and not args.no_confirm:
                prompt = f"About to enrich {count} items. Estimated wall-clock: {eta_str}. Proceed? [y/N] "
                try:
                    reply = input(prompt).strip().lower()
                    if reply not in ("y", "yes"):
                        print("Cancelled.")
                        return 0
                except (EOFError, KeyboardInterrupt):
                    print("\nCancelled.")
                    return 0
            elif not args.yes:
                return 0

            # Execute enrichment
            print(f"Enriching {count} items...")
            result = asyncio.run(
                memory_core.enrich_pending_impl(
                    dry_run=False,
                    limit=limit,
                    allowed_variants=allowed_variants,
                )
            )

            if isinstance(result, dict):
                processed = result.get("processed", 0)
                succeeded = result.get("succeeded", 0)
                failed = result.get("failed", 0)
                errors = result.get("errors_summary", "")

                print(f"Processed: {processed}, Succeeded: {succeeded}, Failed: {failed}")
                if errors:
                    print(f"Errors: {errors}")
            else:
                print(result)
        else:
            print(result)
            return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


def _cmd_extract_pending(args: argparse.Namespace) -> int:
    """Execute the extract-pending subcommand."""
    import asyncio
    import sys

    # Import here to avoid dependency issues if memory_core is not yet installed
    core = _resolve_bin_script("memory_core.py")
    if core is None:
        print("Error: memory_core.py not found. Run `mcp-memory install-m3`.", file=sys.stderr)
        return 1
    _bin.ensure_on_path()
    import memory_core

    limit = args.limit or 0
    allowed_variants = args.allowed_variant or []

    try:
        # First call: dry-run to get count and ETA
        result = asyncio.run(
            memory_core.extract_pending_impl(
                dry_run=True,
                limit=limit,
                allowed_variants=allowed_variants,
            )
        )

        if isinstance(result, dict):
            count = result.get("count", 0)
            eta_seconds = result.get("est_wall_clock_seconds", 0)
            sample_ids = result.get("sample_ids", [])

            # Format wall-clock time
            minutes = int(eta_seconds // 60)
            seconds = int(eta_seconds % 60)
            eta_str = f"{minutes}m {seconds}s"

            print(f"{count} items pending, ETA {eta_str}")
            if sample_ids:
                print(f"Sample IDs: {', '.join(sample_ids[:3])}")

            # If --yes is set and not --no-confirm, ask for confirmation
            if args.yes and not args.no_confirm:
                prompt = (
                    f"About to extract entities from {count} items. Estimated wall-clock: {eta_str}. Proceed? [y/N] "
                )
                try:
                    reply = input(prompt).strip().lower()
                    if reply not in ("y", "yes"):
                        print("Cancelled.")
                        return 0
                except (EOFError, KeyboardInterrupt):
                    print("\nCancelled.")
                    return 0
            elif not args.yes:
                return 0

            # Execute extraction
            print(f"Extracting entities from {count} items...")
            result = asyncio.run(
                memory_core.extract_pending_impl(
                    dry_run=False,
                    limit=limit,
                    allowed_variants=allowed_variants,
                )
            )

            if isinstance(result, dict):
                processed = result.get("processed", 0)
                succeeded = result.get("succeeded", 0)
                failed = result.get("failed", 0)
                errors = result.get("errors_summary", "")

                print(f"Processed: {processed}, Succeeded: {succeeded}, Failed: {failed}")
                if errors:
                    print(f"Errors: {errors}")
            else:
                print(result)
        else:
            print(result)
            return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


def _cmd_chatlog(args: argparse.Namespace) -> int:
    """Dispatch `mcp-memory chatlog <init|status|doctor>` to bin/ scripts."""
    sub = args.chatlog_cmd
    if sub is None:
        # No subcommand given → show status by default (matches `doctor` ergonomics).
        return _run_bin_script("chatlog_status.py", [])
    if sub == "init":
        return _run_bin_script("chatlog_init.py", args.rest)
    if sub == "status":
        return _run_bin_script("chatlog_status.py", args.rest)
    if sub == "hook-path":
        # Print the absolute path to the chatlog hook script for the current OS.
        # Used by the Claude Code plugin's hooks/hooks.json so plugin hooks
        # don't need to hardcode an install location.
        from m3_memory.installer import find_bridge

        bridge = find_bridge()
        if bridge is None:
            print(file=sys.stdout)  # empty — caller should treat as no-op
            return 1
        if is_windows():
            script = bridge.parent / "hooks" / "chatlog" / "claude_code_precompact.ps1"
        else:
            script = bridge.parent / "hooks" / "chatlog" / "claude_code_precompact.sh"
        if not os.path.isfile(script):
            return 1
        print(str(script))
        return 0

    if sub == "doctor":
        # `doctor` = status + nonzero exit if the subsystem reports warnings.
        # Implemented inline so we can inspect the JSON output without forking.
        status_script = _resolve_bin_script("chatlog_status.py")
        if status_script is None:
            print("Error: chatlog_status.py not found. Run `mcp-memory install-m3`.", file=sys.stderr)
            return 1
        import json as _json
        import runpy

        _bin.ensure_on_path()  # bin/ (where status_script lives) on sys.path
        # Call the impl directly for clean JSON, bypassing the CLI formatter.
        mod = runpy.run_path(str(status_script), run_name="_chatlog_status_mod")
        data = _json.loads(mod["chatlog_status_impl"]())
        warnings = data.get("warnings") or []
        # Reuse the human-readable table formatter for output.
        print(mod["_format_table"](data))
        if warnings:
            print(f"\n[X] {len(warnings)} warning(s) — see above.", file=sys.stderr)
            return 1
        print("\n[OK] chatlog healthy.")
        return 0
    print(f"Error: unknown chatlog subcommand {sub!r}", file=sys.stderr)
    return 2


def _cmd_embedder(args: argparse.Namespace) -> int:
    """Manage the sovereign m3 CPU embedder (BGE-M3 on port 8082)."""
    sub = args.embedder_cmd
    if sub is None:
        print(
            "Error: `m3 embedder` requires a subcommand (install, start, stop, status, "
            "uninstall, install-gpu, shared, unshared, backfill, reembed). "
            "Run `m3 embedder --help`.",
            file=sys.stderr,
        )
        return 2
    # Each subcommand registered its own func= via embedder_admin.add_arguments.
    return cast("int", args.func(args))


# ── Generated tool subcommands: `m3 <domain> <tool>` ─────────────────────────
# The MCP tool catalog (bin/mcp_tool_catalog.py) is the single source of truth.
# We generate one top-level subcommand per tool domain (memory, files, …) and,
# under each, one subcommand per tool. Humans reach the full tool surface the
# same way the LLM reaches it via m3_call — both go through the catalog's
# execute_tool_structured, so behavior cannot drift. See
# to_be_deleted/DUAL_SURFACE_TOOL_ACCESS_PLAN.md (PR 2).
#
# The chatlog DOMAIN is exposed as `m3 chat <tool>` because the top-level
# `m3 chatlog` operational command (init/status/doctor/hook-path) predates this
# and is wired into hooks.json + every install guide — left untouched.
_DOMAIN_CMD = {"chatlog": "chat"}

# Tools the human CLI must not surface (meta/dispatcher — no human use, and
# m3_call's object/array args don't map to flat flags).
_CLI_TOOL_EXCLUDE = frozenset({"m3_call", "m3_index", "tools_list_domains", "tools_load_domain"})


def _bin_on_path() -> bool:
    """Put bin/ on sys.path so mcp_tool_catalog + siblings import. Returns
    False if no install/dev checkout is resolvable (tool subcommands are then
    silently skipped — the operational commands still work)."""
    core = _resolve_bin_script("memory_core.py")
    if core is None:
        return False
    _bin.ensure_on_path()  # bin/ (where core lives) on sys.path
    return True


def _json_type_to_argparse(t: str):
    return {"integer": int, "number": float}.get(t)  # str is the argparse default


class _CommaSplitAppend(argparse.Action):
    """Collect an array-of-scalar flag from space- AND comma-separated values,
    ACCUMULATING across repeats.

    Fixes two halves of the same bug (2026-07-31):
      * ``--patterns "a,b,c"`` used to bind the raw string, which the tool layer
        then iterated per-character into ['a', ',', 'b', ...].
      * ``--patterns a --patterns b`` used to OVERWRITE (last wins) rather than
        append, so there was no working way to pass several values at all.

    Both forms now yield the same list, and repeats add to it::

        --patterns api_keys jwt
        --patterns api_keys,jwt
        --patterns api_keys --patterns jwt
    """

    @override
    def __call__(self, parser, namespace, values, option_string=None):
        if isinstance(values, str):
            values = [values]
        out = list(getattr(namespace, self.dest, None) or [])
        for v in values:
            # Split on commas, drop empties from "a,,b" / trailing commas.
            out.extend(part for part in str(v).split(",") if part.strip())
        setattr(namespace, self.dest, out)


def _esc(s: str) -> str:
    """Escape % for argparse help (it %-expands help strings; a bare % in a
    tool description otherwise raises 'badly formed help string')."""
    return (s or "").replace("%", "%%")


def _spec_is_complex(spec) -> bool:
    """A tool has a complex arg (object / array-of-object / union) that can't be
    a flat flag — it takes a single --json blob instead."""
    props = spec.parameters.get("properties", {}) or {}
    for name, pdef in props.items():
        if name == "database":
            continue
        typ = pdef.get("type")
        if typ == "object" or "oneOf" in pdef or "anyOf" in pdef:
            return True
        if typ == "array":
            items = pdef.get("items", {}) or {}
            if items.get("type") == "object" or "oneOf" in items or "anyOf" in items:
                return True
    return False


class _VersionAction(argparse.Action):
    """``--version``, resolved when used: the version lookup costs ~55 ms, and
    almost no command needs it (lazy imports design, 2026-10-12)."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings=option_strings, dest=dest, default=default, nargs=0, help=help)

    @override
    def __call__(self, parser, namespace, values, option_string=None):
        from m3_memory import __version__

        print(f"m3-memory {__version__}")
        parser.exit()


# Global options that take a value: the word after them is not the command.
_GLOBAL_VALUE_OPTIONS = frozenset({"--config", "--caller"})


def _command_word(argv: list[str]) -> str | None:
    """The first command word on the command line (``memory`` in
    ``m3 --config f.toml memory memory_search``), or None. Only used to decide
    which domain's tool subcommands to build; argparse still does the parsing."""
    skip = False
    for word in argv:
        if skip:
            skip = False
            continue
        if word in _GLOBAL_VALUE_OPTIONS:
            skip = True
            continue
        if word.startswith("-"):
            continue
        return word
    return None


def _add_tool_domain_subcommands(subparsers, command: str | None = None) -> None:
    """Register `m3 <domain> <tool>` subcommands generated from the catalog.

    Only the domain ``command`` names gets its tools: the catalog import costs
    ~160 ms (with httpx and the Rust core under it), and `m3 config get` must
    not pay it. Every other domain is a stub carrying its name and description,
    so `m3 --help` still lists it.

    Best-effort: if the catalog can't be imported (no install yet), skip
    silently — the operational commands above are unaffected.
    """
    if not _bin_on_path():
        return
    try:
        import tool_domains as _td
    except Exception:
        return
    wanted = {d for d in _td.DOMAIN_DESCRIPTIONS if _DOMAIN_CMD.get(d, d) == command}
    for domain in sorted(set(_td.DOMAIN_DESCRIPTIONS) - wanted):
        cmd = _DOMAIN_CMD.get(domain, domain)
        stub = subparsers.add_parser(
            cmd, help=_esc(f"{_td.DOMAIN_DESCRIPTIONS[domain]} (run `m3 {cmd} --help` for its tools).")
        )
        stub.set_defaults(func=_cmd_tool_dispatch, _domain_cmd=cmd)
    if not wanted:
        return
    try:
        import mcp_tool_catalog as _cat
    except Exception as e:  # noqa: BLE001 -- reported when the named domain runs, below
        # The command line names this domain, so a silent skip would surface as
        # argparse's "invalid choice", which names the wrong problem (§3).
        for domain in wanted:
            cmd = _DOMAIN_CMD.get(domain, domain)
            broken = subparsers.add_parser(cmd, help=_esc(_td.DOMAIN_DESCRIPTIONS[domain]))
            broken.add_argument("_rest", nargs=argparse.REMAINDER)
            broken.set_defaults(func=_catalog_unavailable, _domain_cmd=cmd, _catalog_error=f"{type(e).__name__}: {e}")
        return

    import argparse as _ap
    from collections import defaultdict

    by_domain = defaultdict(list)
    for spec in _cat.TOOLS:
        if spec.name in _CLI_TOOL_EXCLUDE:
            continue
        domain = _td.domain_of_tool(spec.name)
        if domain in wanted:
            by_domain[domain].append(spec)

    for domain in sorted(by_domain):
        cmd = _DOMAIN_CMD.get(domain, domain)
        desc = _td.DOMAIN_DESCRIPTIONS.get(domain, "")
        dom_parser = subparsers.add_parser(
            cmd, help=_esc(f"{desc} ({len(by_domain[domain])} tools — run `m3 {cmd} --help`).")
        )
        tool_sub = dom_parser.add_subparsers(dest="_tool_cmd", metavar="<tool>")
        dom_parser.set_defaults(func=_cmd_tool_dispatch, _domain_cmd=cmd)

        # The chatlog domain (`m3 chat`) ALSO carries the operational
        # subcommands (init/status/doctor/hook-path) so `chat` is the single
        # chatlog namespace. `m3 chatlog <sub>` remains as a back-compat alias
        # (registered separately, untouched). These route to _cmd_chatlog via
        # chatlog_cmd; their flags are forwarded verbatim (parse_known_args).
        if cmd == "chat":
            for op, op_help in (
                ("init", "Interactive setup — wire hooks, choose DB path, configure redaction."),
                ("status", "Print a summary of chatlog state (row counts, queue, hooks)."),
                ("doctor", "Same as status, but exits nonzero on warnings."),
                ("hook-path", "Print absolute path to the chatlog hook script for plugin hooks."),
            ):
                op_p = tool_sub.add_parser(op, help=_esc(op_help), add_help=False)
                op_p.set_defaults(func=_cmd_chatlog, chatlog_cmd=op)

        for spec in sorted(by_domain[domain], key=lambda s: s.name):
            complex_ = _spec_is_complex(spec)
            summary = _esc((spec.description or "").split(".")[0].strip()[:75])
            tp = tool_sub.add_parser(spec.name, help=summary)
            props = spec.parameters.get("properties", {}) or {}
            required = set(spec.parameters.get("required", []))

            if not complex_:
                for pname, pdef in props.items():
                    if pname in _cat.ROUTING_ARGS:
                        continue  # added as the shared --database / --store below
                    ptype = pdef.get("type", "string")
                    phelp = _esc(pdef.get("description", ""))
                    # ⚠ NEVER argparse-required, even for a required property.
                    # A required arg can now arrive EITHER as a flag or in the
                    # piped/--json object, and argparse cannot see the latter --
                    # it rejects the call before the JSON is ever read. The
                    # requirement is enforced after the merge instead (see
                    # _cmd_tool_dispatch), so both routes satisfy it and a
                    # genuine omission still fails loudly with the same message.
                    req = False
                    if pname in required:
                        phelp = (phelp + "  [required]").strip()
                    if ptype == "boolean":
                        tp.add_argument(
                            f"--{pname}",
                            dest=pname,
                            action=_ap.BooleanOptionalAction,
                            default=None,
                            help=phelp,
                        )
                    elif ptype == "array":
                        # Array-of-SCALAR (array-of-object went to --json above).
                        # Without nargs this bound a bare str, and the tool layer
                        # then iterated it CHARACTER BY CHARACTER: `--patterns
                        # "api_keys,jwt"` became ['a','p','i',...]. Observed
                        # 2026-07-31 silently mis-configuring chatlog redaction —
                        # the config saved "successfully" with 49 one-letter
                        # groups. Accept BOTH shell-native forms:
                        #   --patterns api_keys jwt          (space-separated)
                        #   --patterns api_keys,jwt          (comma-separated)
                        tp.add_argument(
                            f"--{pname}",
                            dest=pname,
                            nargs="+",
                            action=_CommaSplitAppend,
                            required=req,
                            default=None,
                            help=(phelp + "  (space- or comma-separated)").strip(),
                        )
                    else:
                        tp.add_argument(
                            f"--{pname}",
                            dest=pname,
                            type=_json_type_to_argparse(ptype),
                            required=req,
                            default=None,
                            help=phelp,
                        )

            # Universal extras on every tool subcommand.
            #
            # --json / --json-file live HERE, not under `if complex_:` where they
            # started. Gated on complexity they reached only 10 of 118 tools, so
            # `echo '{...}' | m3 memory memory_search --json-file -` -- the
            # obvious UNIX thing -- failed on almost everything. The flags cost
            # nothing on a simple tool and make piping uniform across the whole
            # surface. Verified: no tool declares a property named `json`,
            # `json_file` or `stdin`, so there is no collision to resolve.
            mx = tp.add_mutually_exclusive_group()
            mx.add_argument(
                "--json",
                dest="_json_args",
                metavar="OBJ",
                help="Tool arguments as a single JSON object. Merges with any "
                "flags you also pass; an explicit flag wins on conflict. "
                "Large payloads must use --json-file instead: cmd.exe caps "
                "a command line at 8,191 chars and rejects the call before "
                "this program runs (Windows CreateProcess itself allows "
                "~32 KB, so the ceiling depends on the shell).",
            )
            mx.add_argument(
                "--json-file",
                dest="_json_file",
                metavar="PATH",
                help="Read tool arguments from a JSON file, or '-' for stdin "
                "(`echo '{...}' | m3 <domain> <tool> --json-file -`). Use "
                "this for any payload over a few KB -- --json passes the "
                "object through the command line, which the invoking shell "
                "may refuse outright.",
            )
            tp.add_argument(
                "--database",
                dest="database",
                default=None,
                help="SQLite DB path. Default: agent_memory.db. SQLite backend only, not files tools (refused there).",
            )
            tp.add_argument(
                "--store",
                dest="store",
                default=None,
                help="Named store for this call: 'main' or a [stores.<name>] "
                "table in m3_config.toml (core and files). Not with --database.",
            )
            tp.add_argument(
                "--dry-run",
                dest="_dry_run",
                action="store_true",
                help="Validate args + check the destructive gate without executing.",
            )
            tp.add_argument(
                "--yes",
                dest="_yes",
                action="store_true",
                help="Confirm a destructive (mutating) tool. Required for such tools.",
            )
            tp.set_defaults(
                func=_cmd_tool_dispatch,
                _tool_name=spec.name,
                _tool_complex=complex_,
                _tool_destructive=not spec.default_allowed,
            )


def _catalog_unavailable(args: argparse.Namespace) -> int:
    """`m3 <domain> ...` when the tool catalog did not import: say so, with the cause."""
    print(
        f"Error: `m3 {args._domain_cmd}` needs the tool catalog, which did not import "
        f"({args._catalog_error}). inspect: `m3 doctor`; repair: `m3 upgrade`.",
        file=sys.stderr,
    )
    return 1


def _cmd_tool_dispatch(args: argparse.Namespace) -> int:
    """Run one generated `m3 <domain> <tool>` invocation through the catalog's
    execute_tool_structured — the same path m3_call uses."""
    import asyncio
    import json as _json

    tool = getattr(args, "_tool_name", None)
    if tool is None:
        # `m3 <domain>` with no tool — list the domain's tools.
        cmd = getattr(args, "_domain_cmd", "<domain>")
        print(f"Error: `m3 {cmd}` requires a tool. Run `m3 {cmd} --help`.", file=sys.stderr)
        return 2

    if not _bin_on_path():
        print("Error: tool catalog unavailable. Run `m3 install-m3` first.", file=sys.stderr)
        return 1
    import mcp_tool_catalog as _cat

    spec = next((t for t in _cat.TOOLS if t.name == tool), None)
    if spec is None:
        print(f"Error: unknown tool {tool!r}.", file=sys.stderr)
        return 1

    # Destructive confirmation (independent of the MCP env gate).
    destructive = getattr(args, "_tool_destructive", False)
    dry_run = getattr(args, "_dry_run", False)
    if destructive and not dry_run and not getattr(args, "_yes", False):
        print(
            f"Error: {tool} mutates/deletes data — pass --yes to confirm (or --dry-run to validate without executing).",
            file=sys.stderr,
        )
        return 2

    # Assemble the tool args.
    #
    # Flags first, then JSON overlaid on top -- for EVERY tool, not just the
    # complex ones. A simple tool now accepts a piped object as well as flags,
    # so `echo '{"k":3}' | m3 memory memory_search --query x --json-file -`
    # composes instead of one silently winning. An explicit flag beats the piped
    # value: it is the more specific, more deliberate statement of intent, and it
    # lets a script pipe a base object and override one field per invocation.
    props = spec.parameters.get("properties", {}) or {}
    flag_args = {}
    if not getattr(args, "_tool_complex", False):
        for pname in props:
            if pname in _cat.ROUTING_ARGS:
                continue
            val = getattr(args, pname, None)
            if val is not None:
                flag_args[pname] = val

    raw = getattr(args, "_json_args", None)
    jfile = getattr(args, "_json_file", None)
    if raw is None and jfile is None:
        tool_args = dict(flag_args)
    else:
        if jfile is not None:
            try:
                if jfile == "-":
                    raw = sys.stdin.read()
                else:
                    with open(jfile, encoding="utf-8") as f:
                        raw = f.read()
            except Exception as e:
                print(f"Error: failed to read --json-file: {e}", file=sys.stderr)
                return 2
        if raw is None:
            print("Error: no JSON arguments were read.", file=sys.stderr)
            return 2
        try:
            tool_args = _json.loads(raw)
        except (ValueError, _json.JSONDecodeError) as e:
            print(f"Error: --json is not valid JSON: {e}", file=sys.stderr)
            return 2
        if not isinstance(tool_args, dict):
            print("Error: --json must be a JSON object.", file=sys.stderr)
            return 2
        # Reject unknown keys instead of dropping them. The flag path only
        # ever builds args from declared `properties`, so a typo there is a
        # loud argparse error; --json used to pass the object straight
        # through, so a near-miss key (`owner` for `owner_agent`) was
        # silently discarded and the call still reported ok. That produced a
        # task that looked assigned and was not. §3: fail loud, never
        # silent. The routing args and `timeout` are CLI-level extras the impls accept.
        _declared = set((spec.parameters.get("properties") or {}).keys())
        _declared |= {*_cat.ROUTING_ARGS, "timeout"}
        _unknown = sorted(set(tool_args) - _declared)
        if _unknown:
            import difflib as _difflib

            lines = [f"Error: --json has unknown key(s) for {tool}: " + ", ".join(repr(k) for k in _unknown)]
            for key in _unknown:
                near = _difflib.get_close_matches(key, sorted(_declared), n=1, cutoff=0.6)
                if near:
                    lines.append(f"  did you mean {near[0]!r} instead of {key!r}?")
            lines.append("  accepted: " + ", ".join(sorted(_declared)))
            print("\n".join(lines), file=sys.stderr)
            return 2

        # Explicit flags override the piped/JSON object. The JSON is the
        # base; a flag the user typed on THIS invocation is the override.
        tool_args.update(flag_args)

    # Required-argument check, AFTER the merge. argparse can no longer do this
    # (see the `req = False` note where the flags are declared): a required value
    # may arrive as a flag or inside the piped JSON, and argparse only sees the
    # former. Checking here means either route satisfies it, while a real
    # omission still fails loudly rather than reaching the impl as a TypeError.
    # `is None` as well as absent: a piped `{"query": null}` puts the key in the
    # dict while supplying nothing, so a presence-only check would pass it to the
    # impl and surface as a TypeError from inside the tool instead of a clear
    # CLI error here. Flags cannot produce this (flag_args drops None), so it is
    # reachable only through the JSON route the merge added.
    _missing = [p for p in (spec.parameters.get("required") or []) if tool_args.get(p) is None and p != "database"]
    if _missing:
        # A complex tool registers no per-argument flags (they take --json
        # only), so naming `--created_by` there sent the user to a flag that
        # argparse then rejected as unrecognized.
        if _spec_is_complex(spec):
            print(
                f"Error: {tool} requires {', '.join(repr(m) for m in _missing)} "
                f"— this tool takes its arguments only as --json / --json-file; include the key there.",
                file=sys.stderr,
            )
        else:
            print(
                f"Error: {tool} requires {', '.join('--' + m for m in _missing)} "
                f"— pass the flag, or include the key in --json / --json-file.",
                file=sys.stderr,
            )
        return 2

    for routing in _cat.ROUTING_ARGS:  # --database / --store: the dispatcher owns them
        value = getattr(args, routing, None)
        if value:
            tool_args[routing] = value

    # ⚠ RECORDS BY DEFAULT ON THE CLI ONLY — NOT IN THE TOOLSPEC.
    #
    # §3's first tenet: "return structured data, never message strings; a tool
    # that returns 'Found 21 groups.' forces the caller into prose-parsing."
    # Thirteen tools carry an `as_records` option whose default is False, and
    # that default is DELIBERATE and test-pinned: ~400 MCP callers predate the
    # param and parse the display string, so flipping it in the ToolSpec would
    # break every one of them (see memory/records.py's compatibility rule).
    #
    # But the CLI's consumer is a PIPE, not an LLM reading prose. Now that every
    # tool is pipeable, `m3 tasks task_list | jq '.items[]'` is the obvious
    # thing to write, and it silently matched nothing because stdout was
    # "Tasks (20):\n  [c0aab26a] P0 ...". So the default flips HERE, at the
    # surface whose consumer changed, and the MCP contract is untouched.
    #
    # An explicit --no-as_records still wins: the flag is parsed before this and
    # only an ABSENT value is defaulted.
    _props = (spec.parameters or {}).get("properties", {}) or {}
    if "as_records" in _props and "as_records" not in tool_args:
        tool_args["as_records"] = True

    async def _run():
        # allow_caller_agent_id: this is a human at a terminal, not an LLM, so an
        # explicit `--agent_id` is the user's own choice and must survive. The
        # m3_call dispatcher deliberately does NOT opt in — see the anti-spoofing
        # note in execute_tool_structured.
        return await _cat.execute_tool_structured(
            spec, tool_args, agent_id="", dry_run=dry_run, allow_caller_agent_id=True
        )

    async def _run_and_drain():
        out = await _run()
        # ⚠ DRAIN BEFORE THE LOOP DIES. Access stamps are batched by a 0.25s
        # async task that only exists while a loop runs. A CLI process exits
        # first, so a search from the shell stamped nothing — and since
        # `last_accessed_at` is what authorises a grade, `memory_search | ...
        # | memory_grade` could never apply a verdict. Best-effort: a failed
        # stamp must never fail the command that produced the answer.
        try:
            from memory.db import flush_access_stamps_now

            flush_access_stamps_now()
        except Exception:  # noqa: BLE001
            pass
        return out

    try:
        result = asyncio.run(_run_and_drain())
    except Exception as e:  # validation (ValueError) or impl failure
        print(
            _json.dumps({"ok": False, "error": "call_failed", "tool": tool, "detail": f"{type(e).__name__}: {e}"}),
            file=sys.stderr,
        )
        return 1
    # ⚠ DO NOT DOUBLE-ENCODE A RESULT THAT IS ALREADY JSON.
    #
    # Several impls return a JSON *string* rather than an object — `--as_records`
    # is the clearest case: memory_search builds {"count", "items", "query"} and
    # serializes it. Dumping that string again produced a JSON string CONTAINING
    # JSON, so every consumer had to json.loads twice. Piping is now universal,
    # so this is the difference between
    #
    #     m3 memory memory_search --query x --as_records | jq '.items[].id'
    #
    # working and silently returning nothing on a string.
    #
    # Only unwrap what genuinely parses as an object or array — a plain prose
    # result stays a JSON string, because emitting it raw would break the
    # "stdout is always valid JSON" contract the pipe depends on.
    if isinstance(result, str):
        stripped = result.lstrip()
        if stripped[:1] in ("{", "["):
            try:
                result = _json.loads(result)
            except ValueError:
                pass  # looked like JSON, is not — emit as the string it is
    print(_json.dumps(result, default=str, indent=2))
    _warn_if_not_applied(result, tool)
    return 0


def _warn_if_not_applied(result, tool: str) -> None:
    """Narrate a completed-but-inapplicable call on stderr.

    ⚠ THE CASE A PIPELINE CANNOT SEE. A tool can succeed at doing nothing:
    `memory_grade` returns `{"ok": true, "applied": false, "graded": 0,
    "reason": "outside_feedback_window"}` when every verdict arrived late. That
    is a correct RESULT, not a failure — the call completed and declined — so
    it keeps exit 0 per the UNIX convention (0 = the operation completed; 1 = a
    negative result that PREVENTS it; 2 = usage/refusal before it).

    But stdout carries only data, so nothing told the operator. The signal was
    machine-readable and human-invisible, which is the §3 shape: not a lie, but
    a success that reads as more than it was.

    So: data stays on stdout, the narration goes to stderr, exit code unchanged.
    A script that wants to branch still reads `applied` from the payload; a
    human watching a pipeline now sees why nothing happened.

    §3's evidence levels are preserved VERBATIM rather than reworded — the impl
    already emits `observed:` for measured values and `inspect:` for the knob,
    and paraphrasing them here would create a second wording to keep in step.
    """
    if not isinstance(result, dict):
        return
    # Only narrate an explicit non-application. A tool that never reports
    # `applied` is not making this claim, and inventing a warning for it would
    # be the false alarm §3 counts as a violation in its own right.
    if result.get("applied") is not False:
        return

    lines = [f"Note: {tool} completed but applied nothing."]
    for key in ("reason", "observed", "inspect", "note"):
        value = result.get(key)
        if value:
            lines.append(f"  {key}: {value}")
    for key in ("rejected_stale", "unknown"):
        value = result.get(key)
        if value:
            lines.append(f"  {key}: {value}")
    print("\n".join(lines), file=sys.stderr)


def main() -> None:
    # --set={...} is read once, applied, and removed here, before any parser or
    # module reads a setting; the command parses the rest untouched.
    _bin.ensure_on_path()
    from m3_core.paths import apply_early_argv

    apply_early_argv()
    # `m3 secret set NAME --keyring=<value>`: argparse's own error would PRINT the
    # value. --keyring is a switch; the value is asked for. Refuse without echo.
    if "secret" in sys.argv[1:] and any(a.startswith("--keyring=") for a in sys.argv[1:]):
        print(
            "m3 secret: --keyring is a switch, not a place for the value (what followed '=' is not shown); "
            "nothing stored. Run `m3 secret set NAME --keyring` and m3 asks you for the value.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    parser = argparse.ArgumentParser(
        prog="m3",
        description=(
            "M3 Memory - local-first agentic memory MCP server.\n"
            "Add to your agent's MCP config to get persistent, private memory.\n"
            "(Also installed as `mcp-memory` for backwards compatibility.)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Override settings for one run (any command, any m3 script): one table in
double quotes immediately after `m3`, the setting names of m3_config.toml;
strings in ^...^ (^^ is a literal ^), lists in [...], numbers and true/false bare:
  m3 "--set={db.backend=^postgres^, search.row_cap=50}" search "query"
  m3 "--set={llm.endpoints=[^http://a:1234/v1^, ^http://b:11434/v1^]}" doctor
  The same in bash, zsh, PowerShell and cmd. A value containing $ needs single
  quotes instead in bash/zsh/PowerShell. Not for secrets or paths: use the
  secrets file and --config.

Examples:
  # First-time one-command setup (interactive wizard):
  m3 setup

  # Install the sovereign CPU embedder (BGE-M3 on port 8082):
  m3 embedder install

  # Add GPU acceleration to the in-process embedder (CUDA/Vulkan/Metal):
  m3 embedder install-gpu

  # Start the MCP server (used by Claude Code / Gemini CLI / Aider):
  m3
""",
    )
    parser.add_argument("--version", action=_VersionAction, help="show the version and exit")
    parser.add_argument(
        "--caller",
        choices=("upgrade",),
        default=None,
        help="Set by `m3 upgrade` on the steps it runs: they report only changes and problems.",
    )
    parser.add_argument(
        "--config",
        dest="config_file",  # `m3 config show --config` has its own; a shared dest would be overwritten
        metavar="FILE",
        default=None,
        help="Read this m3_config.toml (its directory is the config root) instead of the located one.",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    p_install = subparsers.add_parser(
        "install-m3",
        help="Fetch the m3-memory system payload from GitHub into the M3 root (default: ~/.m3-memory/repo)",
    )
    p_install.add_argument(
        "--force",
        action="store_true",
        help="Wipe an existing repo before re-fetching.",
    )
    p_install.add_argument(
        "--tag",
        default=None,
        help="Override the GitHub tag to fetch (default: the tag of this m3's version).",
    )
    p_install.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip endpoint + capture-mode prompts; use defaults (probe both endpoints; both hooks).",
    )
    p_install.add_argument(
        "--endpoint",
        default=None,
        metavar="URL",
        help="Pin LLM_ENDPOINTS_CSV to this URL (skips the endpoint prompt).",
    )
    p_install.add_argument(
        "--capture-mode",
        default=None,
        choices=("both", "stop", "precompact", "none"),
        help="Chatlog capture hooks to enable (skips the capture-mode prompt).",
    )
    p_install.add_argument(
        "--cognitive-loop",
        action="store_true",
        help="Enable the background cognitive loop worker.",
    )
    p_install.add_argument(
        "--db-backend",
        default=None,
        choices=("sqlite", "postgres"),
        help="Primary database backend (default: sqlite). 'postgres' reads the "
        "DSN from M3_PRIMARY_PG_URL/M3_PG_URL and skips the backend prompt.",
    )
    p_install.set_defaults(func=_cmd_install_m3)

    # One-command interactive wizard: detects agents, asks a handful of
    # questions, then runs install-m3 + CPU-fallback service + per-agent
    # wiring + chatlog hooks + doctor end-to-end.
    p_setup = subparsers.add_parser(
        "setup",
        help="Interactive one-command setup. Run after `pip install m3-memory`.",
    )
    from m3_memory import setup_wizard as _setup_wizard

    _setup_wizard.add_arguments(p_setup)

    p_reinstall = subparsers.add_parser(
        "reinstall",
        help="Wipe and reinstall the system payload (alias for install-m3 --force).",
    )
    p_reinstall.set_defaults(func=_cmd_reinstall)

    p_stop = subparsers.add_parser(
        "stop",
        help="Stop every running m3 DB-writer (cognitive loop, dashboard, MCP). "
        "On macOS/Linux the Rust embed server holds no data and keeps "
        "running (stop it with `m3 embedder stop`). `m3 upgrade` does this "
        "for you; run it by hand only for a manual package upgrade on Windows.",
    )
    p_stop.add_argument(
        "--timeout",
        type=float,
        default=8.0,
        metavar="SECONDS",
        help="Seconds to wait for a writer to exit before reporting it stuck (default: 8).",
    )
    p_stop.add_argument(
        "--quiet",
        action="store_true",
        help="Report only writers actually stopped or that failed to stop; omit "
        "writers left running that hold no database (used by `m3 upgrade` "
        "after its first stop).",
    )
    p_stop.set_defaults(func=_cmd_stop)

    p_upgrade = subparsers.add_parser(
        "upgrade",
        help="Upgrade the m3 PACKAGE end to end, using the right command for "
        "how it was installed (pipx / pip / pip --user), then re-wire and "
        "verify. Works the same on Windows, macOS and Linux.",
    )
    p_upgrade.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Print the detected install method and the exact plan, change nothing.",
    )
    p_upgrade.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Do not prompt; for scripted/unattended upgrades.",
    )
    p_upgrade.add_argument(
        "--skip-stop",
        dest="skip_stop",
        action="store_true",
        help="Do not stop DB writers first. Only when you have already stopped "
        "them; on Windows an upgrade with writers running can delete the "
        "package and then fail, leaving NO m3 installed.",
    )
    p_upgrade.add_argument(
        "--from-pypi",
        dest="from_pypi",
        action="store_true",
        help="pipx installs: reinstall from PyPI, moving an install that tracks "
        "a local wheel or path onto the PyPI release.",
    )
    p_upgrade.add_argument(
        "--stop-agents",
        dest="stop_agents",
        action="store_true",
        help="Windows: stop m3 in agent sessions that hold its launcher, so the "
        "upgrade can replace it (they reconnect afterwards). At a console "
        "the upgrade asks instead; use this for unattended runs.",
    )
    p_upgrade.set_defaults(func=_cmd_upgrade)

    p_update = subparsers.add_parser(
        "update",
        help="Re-sync the payload for the INSTALLED version (NOT a package upgrade — use `m3 upgrade` for that).",
    )
    p_update.add_argument(
        "--tag",
        default=None,
        help="Override the GitHub tag to fetch (default: the tag of this m3's version).",
    )
    p_update.add_argument(
        "--yes-core",
        dest="yes_core",
        action="store_true",
        help="Install the native m3-core-rs wheel without prompting when it is "
        "behind this release (for scripted/non-interactive updates).",
    )
    p_update.add_argument(
        "--no-core",
        dest="no_core",
        action="store_true",
        help="Never offer the native m3-core-rs upgrade, even if it is stale.",
    )
    p_update.set_defaults(func=_cmd_update)

    p_uninstall = subparsers.add_parser(
        "uninstall",
        help="Remove the cloned payload and the config file.",
    )
    p_uninstall.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation prompt.",
    )
    p_uninstall.set_defaults(func=_cmd_uninstall)

    p_status = subparsers.add_parser(
        "status",
        help="One-line health check (healthy/degraded/broken + memory count).",
    )
    p_status.set_defaults(func=_cmd_status)

    p_doctor = subparsers.add_parser(
        "doctor",
        help="Full diagnostics: paths, resolved bridge, embedder, chatlog, crypto.",
    )
    p_doctor.add_argument(
        "--fix",
        action="store_true",
        help="Repair what self-repairs: repoint dead agent MCP configs, restart "
        "the shared embedder, heal the DB, etc. (runs the payload doctor's "
        "repair pass too).",
    )
    p_doctor.add_argument(
        "--fix-hooks",
        action="store_true",
        help="With --fix: also rewrite m3's Claude settings.json entries (hooks, "
        "statusline, aux bridges) to the current payload. settings.json is "
        "backed up first. Opt-in because settings.json is user-owned.",
    )
    p_doctor.add_argument(
        "--verbose",
        action="store_true",
        help="Show full detail (DB-repair steps, each probe's expanded report, "
        "model-load logs). Default is a compact high-yield summary.",
    )
    p_doctor.set_defaults(func=_cmd_doctor)

    p_governor = subparsers.add_parser(
        "governor",
        help="Inspect / migrate legacy scheduled tasks to the background governor.",
    )
    gov_sub = p_governor.add_subparsers(dest="governor_cmd", metavar="<status|migrate>")
    gov_sub.add_parser("status", help="Report governor-eligible scheduled tasks still installed.")
    p_gov_mig = gov_sub.add_parser("migrate", help="Remove governor-eligible scheduled tasks.")
    p_gov_mig.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt (headless use).")
    p_governor.set_defaults(func=_cmd_governor)

    p_config = subparsers.add_parser(
        "config",
        help="Show, read and change m3's configuration (m3_config.toml).",
    )
    cfg_sub = p_config.add_subparsers(dest="config_cmd", metavar="<show|get|set|unset|import-env>")
    p_cfg_show = cfg_sub.add_parser("show", help="Every setting with its value.")
    p_cfg_show.add_argument("--origin", action="store_true", help="Say where each value came from.")
    p_cfg_show.add_argument("--json", action="store_true", help="Machine-readable output.")
    p_cfg_get = cfg_sub.add_parser("get", help="One setting's value.")
    p_cfg_get.add_argument("key")
    p_cfg_get.add_argument("--origin", action="store_true", help="Say where the value came from.")
    p_cfg_set = cfg_sub.add_parser("set", help="Write one setting to m3_config.toml.")
    p_cfg_set.add_argument("key")
    p_cfg_set.add_argument("value", help="As the environment would spell it; a list comma-separated.")
    p_cfg_unset = cfg_sub.add_parser("unset", help="Remove one setting from m3_config.toml (it falls back).")
    p_cfg_unset.add_argument("key")
    for p_cfg in (p_cfg_set, p_cfg_unset):
        p_cfg.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation for a sensitive key.")
    p_cfg_import = cfg_sub.add_parser(
        "import-env", help="Move settings from env, rc files, .env and MCP env blocks into m3_config.toml."
    )
    p_cfg_import.add_argument("--dry-run", action="store_true", help="Say what would be imported; write nothing.")
    p_cfg_import.add_argument(
        "--non-interactive", action="store_true", help="Never ask; list each disagreement and import nothing for it."
    )
    for p_cfg in (p_cfg_show, p_cfg_get, p_cfg_set, p_cfg_unset, p_cfg_import):
        p_cfg.add_argument("--config", metavar="FILE", help="Use this file instead of the located one.")
    p_config.set_defaults(func=_cmd_config)

    p_batch = subparsers.add_parser(
        "batch",
        help="Run several m3 commands from stdin in one process (one per line; JSON Lines results).",
        description=(
            "Each stdin line is a command as it would follow `m3` (POSIX quoting, the same on every OS), "
            'or a JSON array of its arguments (["config", "get", "search.row_cap"]). Blank and # lines '
            'are skipped. --set and --config go on the batch itself: m3 "--set={...}" batch.'
        ),
    )
    p_batch.add_argument("--text", action="store_true", help="Readable sections instead of JSON Lines.")
    p_batch.add_argument("--stop-on-error", action="store_true", help="Stop at the first command that fails.")
    p_batch.set_defaults(func=_cmd_batch)

    p_restore = subparsers.add_parser(
        "restore",
        help="List or restore the versions m3 saved before rewriting a file (<file>.backup_history).",
    )
    p_restore.add_argument("file", help="The file m3 rewrote, e.g. ~/.claude/settings.json.")
    p_restore.add_argument("--at", metavar="TIME", help="The UTC time of the change to undo (from the list).")
    p_restore.set_defaults(func=_cmd_restore)

    p_secret = subparsers.add_parser(
        "secret",
        help="Secrets: `refresh` makes running m3 processes re-read changed keys.",
    )
    sec_sub = p_secret.add_subparsers(dest="secret_cmd", metavar="<refresh|set>")
    sec_sub.add_parser(
        "refresh",
        help="Signal every m3 process to drop its cached secrets (after changing a key outside m3).",
    )
    p_secret_set = sec_sub.add_parser(
        "set",
        help="Store one secret: m3 ASKS for the value (hidden); never type it on the command line.",
    )
    p_secret_set.add_argument("name", help="The secret's name, e.g. M3_PRIMARY_PG_URL (not its value).")
    p_secret_set.add_argument(
        "--keyring",
        action="store_true",
        help="A switch, not a place for the value: store it in the OS keyring instead of the secrets "
        "file. m3 then asks you for the value.",
    )
    p_secret.set_defaults(func=_cmd_secret)

    p_schedules = subparsers.add_parser(
        "schedules",
        help="Verify / repair the background scheduled tasks (dashboard, embed server, cognitive loop, maintenance).",
    )
    sched_sub = p_schedules.add_subparsers(dest="schedules_cmd", metavar="<verify|repair|list|add|remove>")
    p_sched_ver = sched_sub.add_parser(
        "verify", help="Check every registered task matches spec. Exits nonzero on drift."
    )
    p_sched_ver.add_argument("name", nargs="?", default=None, help="Verify one task by name or alias (default: all).")
    sched_sub.add_parser(
        "repair",
        help="Re-register every task. REPLACES existing definitions (delete + recreate) "
        "and restarts the long-lived services. Windows: needs an ADMIN shell.",
    )
    sched_sub.add_parser("list", help="List the configured schedules and exit.")
    p_sched_add = sched_sub.add_parser("add", help="Install one schedule by name, or 'all'.")
    p_sched_add.add_argument("name", nargs="?", default="all")
    p_sched_rm = sched_sub.add_parser("remove", help="Remove one schedule by name, or 'all'.")
    p_sched_rm.add_argument("name", nargs="?", default="all")
    p_schedules.add_argument("--port", type=int, default=None, help="Dashboard port (default 8088).")
    p_schedules.set_defaults(func=_cmd_schedules)

    p_wiki = subparsers.add_parser(
        "wiki",
        help="Generate a browsable, interlinked wiki from your core memories + files corpus.",
    )
    wiki_sub = p_wiki.add_subparsers(dest="wiki_cmd", metavar="<generate|compile|status>")
    p_wiki_gen = wiki_sub.add_parser("generate", help="Compile the wiki vault (default <engine_root>/wiki).")
    p_wiki_gen.add_argument("--out", default=None, help="Output vault dir.")
    p_wiki_gen.add_argument("--check", action="store_true", help="Exit non-zero if the on-disk vault is stale.")
    p_wiki_gen.add_argument("--no-files", action="store_true", help="Memory-only vault (skip the files corpus).")
    p_wiki_gen.add_argument(
        "--check-drift",
        action="store_true",
        help="Report-only: ask an LLM judge whether each compiled "
        "synthesis still matches its sources. Mutually "
        "exclusive with --check.",
    )
    p_wiki_gen.add_argument("--no-networkx", action="store_true", help="Force the pure-Python clustering fallback.")
    p_wiki_gen.add_argument(
        "--synthesize",
        action="store_true",
        help="Add an LLM prose lede per topic via a local chat endpoint (opt-in, cached). Not compatible with --check.",
    )
    p_wiki_gen.add_argument(
        "--importance-threshold", type=float, default=None, help="Min importance to count as 'core' (default 0.55)."
    )
    p_wiki_gen.add_argument(
        "--exclude",
        default=None,
        metavar="REGEX",
        help="Drop memories whose title/content matches this regex (e.g. to exclude private/bench notes).",
    )
    p_wiki_gen.add_argument(
        "--html",
        action="store_true",
        help="Also write a self-contained wiki.html viewer to click through the vault in a browser (offline).",
    )
    p_wiki_gen.add_argument(
        "--obsidian",
        action="store_true",
        help="Emit [[wikilinks]] so Obsidian's graph view and backlinks work (opt-in; literal text elsewhere).",
    )
    p_wiki_compile = wiki_sub.add_parser(
        "compile",
        help="WRITE compiled synthesis memories for topic clusters (mutates the "
        "store; idempotent — safe to re-run). Use --dry-run to preview.",
    )
    p_wiki_compile.add_argument(
        "--importance-threshold", type=float, default=None, help="Min importance to count as 'core' (default 0.55)."
    )
    p_wiki_compile.add_argument("--no-networkx", action="store_true", help="Force the pure-Python clustering fallback.")
    p_wiki_compile.add_argument("--scope", default="agent", help="Tenancy scope for written syntheses (default agent).")
    p_wiki_compile.add_argument("--user-id", default="", help="Owner user_id for written syntheses.")
    p_wiki_compile.add_argument(
        "--dry-run", action="store_true", help="Report what would be compiled; no model call, no write."
    )
    p_wiki_status = wiki_sub.add_parser("status", help="Report vault location, page count, last build.")
    p_wiki_status.add_argument("--out", default=None, help="Vault dir to inspect.")
    p_wiki.set_defaults(func=_cmd_wiki)

    p_fips = subparsers.add_parser(
        "fips",
        help="FIPS crypto: build/install open-source wolfSSL, or show crypto status.",
    )
    fips_sub = p_fips.add_subparsers(dest="fips_cmd", metavar="<install-wolfssl|status>")
    p_fips_inst = fips_sub.add_parser(
        "install-wolfssl",
        help="Build the OPEN-SOURCE wolfSSL from official source and install to "
        "~/.m3/lib (license-clean; not the CMVP-validated FIPS module).",
    )
    p_fips_inst.add_argument("--ref", default=None, help="wolfSSL git tag/branch.")
    p_fips_inst.add_argument("--dest", default=None, help="Install dir (default ~/.m3/lib).")
    p_fips_inst.add_argument(
        "--print-sha", action="store_true", help="Print the installed library's SHA-256 to self-pin."
    )
    fips_sub.add_parser("status", help="Show the active crypto backend / FIPS tier / lib path.")
    p_fips.set_defaults(func=_cmd_fips)

    p_serve = subparsers.add_parser(
        "serve",
        help="Run the bridge as a streamable-HTTP MCP server (for claude.ai connectors).",
    )
    p_serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1 — use 0.0.0.0 only behind a reverse proxy).",
    )
    p_serve.add_argument("--port", type=int, default=8080, help="TCP port (default: 8080).")
    p_serve.add_argument(
        "--path", default="/mcp", help="HTTP mount path for the streamable-http endpoint (default: /mcp)."
    )
    p_serve.add_argument(
        "--public-host",
        action="append",
        metavar="HOST",
        help="Public hostname a tunnel/proxy presents in the Host header "
        "(e.g. m3.example.com). Repeatable. Without it the transport "
        "rejects tunnelled requests with 421 before auth runs.",
    )
    p_serve.add_argument(
        "--generate-token",
        action="store_true",
        help="Mint a bearer token, store it in the m3 vault, print it once "
        "and exit. The HTTP transport refuses to start without one.",
    )
    p_serve.add_argument(
        "--show-token", action="store_true", help="Report whether a serve token is configured (never prints it)."
    )
    p_serve.add_argument(
        "--force",
        action="store_true",
        help="With --generate-token: replace an existing token. Rotating "
        "invalidates every connector using the old value.",
    )
    p_serve.set_defaults(func=_cmd_serve)

    p_dashboard = subparsers.add_parser(
        "dashboard",
        help="Start the local web dashboard as a detached background service (localhost only).",
    )
    p_dashboard.add_argument(
        "--host",
        default=None,
        help="Bind address (default: 127.0.0.1 — localhost only; do not expose without auth).",
    )
    p_dashboard.add_argument(
        "--port",
        type=int,
        default=None,
        help="TCP port (default: 8088, or $M3_DASHBOARD_PORT).",
    )
    p_dashboard.add_argument(
        "--stop",
        action="store_true",
        help="Stop the running dashboard (kills the detached server, frees the port).",
    )
    p_dashboard.add_argument(
        "--status",
        action="store_true",
        help="Report whether the dashboard is running and its URL.",
    )
    p_dashboard.add_argument(
        "--foreground",
        action="store_true",
        help="Run the server in THIS process (blocks; for debugging). Default detaches.",
    )
    p_dashboard.set_defaults(func=_cmd_dashboard)

    p_enrich = subparsers.add_parser(
        "enrich-pending",
        help="Enrich pending memory items with SLM-distilled facts.",
    )
    p_enrich.add_argument(
        "--yes",
        action="store_true",
        help="Execute enrichment (default is dry-run).",
    )
    p_enrich.add_argument(
        "--no-confirm",
        action="store_true",
        help="Skip confirmation prompt (for cron/headless use).",
    )
    p_enrich.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max items to enrich (0 = no limit, default: 0).",
    )
    p_enrich.add_argument(
        "--allowed-variant",
        action="append",
        default=[],
        help="Variant name to include (can be used multiple times).",
    )
    p_enrich.set_defaults(func=_cmd_enrich_pending)

    p_extract = subparsers.add_parser(
        "extract-pending",
        help="Extract pending entities from memory items.",
    )
    p_extract.add_argument(
        "--yes",
        action="store_true",
        help="Execute extraction (default is dry-run).",
    )
    p_extract.add_argument(
        "--no-confirm",
        action="store_true",
        help="Skip confirmation prompt (for cron/headless use).",
    )
    p_extract.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max items to extract (0 = no limit, default: 0).",
    )
    p_extract.add_argument(
        "--allowed-variant",
        action="append",
        default=[],
        help="Variant name to include (can be used multiple times).",
    )
    p_extract.set_defaults(func=_cmd_extract_pending)

    p_chatlog = subparsers.add_parser(
        "chatlog",
        help="Manage the chatlog subsystem (init|status|doctor).",
    )
    chatlog_sub = p_chatlog.add_subparsers(dest="chatlog_cmd", metavar="<subcommand>")
    # Declare the subcommands so help lists them, but accept no flags here —
    # we use parse_known_args below to collect everything after `chatlog <sub>`
    # and forward it verbatim to the underlying bin/ script.
    chatlog_sub.add_parser(
        "init", help="Interactive setup — wire hooks, choose DB path, configure redaction.", add_help=False
    )
    chatlog_sub.add_parser(
        "status", help="Print a summary of chatlog state (row counts, queue, hooks).", add_help=False
    )
    chatlog_sub.add_parser("doctor", help="Same as status, but exits nonzero on warnings.", add_help=False)
    chatlog_sub.add_parser(
        "hook-path", help="Print absolute path to the chatlog hook script for plugin hooks.", add_help=False
    )
    p_chatlog.set_defaults(func=_cmd_chatlog)

    p_embedder_mgmt = subparsers.add_parser(
        "embedder",
        help="Manage the sovereign m3 CPU embedder (install|start|stop|status|uninstall|install-gpu).",
    )
    from m3_memory import embedder_admin as _embedder_admin

    _embedder_admin.add_arguments(p_embedder_mgmt)
    p_embedder_mgmt.set_defaults(func=_cmd_embedder)

    # Generated tool subcommands: `m3 <domain> <tool>` (and `m3 chat <tool>` for
    # the chatlog domain). Best-effort — skipped if no install/dev checkout.
    _add_tool_domain_subcommands(subparsers, _command_word(sys.argv[1:]))

    # Use parse_known_args so flags after `chatlog <sub>` aren't fought over
    # by the outer parser — they're passed through to the child script.
    # args.rest carries them.
    args, extras = parser.parse_known_args()
    if args.config_file:
        from m3_memory._roots import pin_config_file

        pin_config_file(args.config_file)
    if args.caller:
        from m3_memory.wizard.ui import set_caller

        set_caller(args.caller)
    # chatlog (alias) + doctor forward unknown flags verbatim to the child
    # script. `chat` does too, but ONLY for its operational subcommands
    # (chatlog_cmd set) — for generated tool subcommands, unknown flags are
    # real errors just like every other domain.
    _forward = getattr(args, "command", None) in ("chatlog", "doctor") or (
        getattr(args, "command", None) == "chat" and getattr(args, "chatlog_cmd", None)
    )
    if _forward:
        args.rest = extras
    elif extras:
        # For any other subcommand, unknown flags are genuine errors. A stray '#'
        # is cmd.exe passing a pasted "# comment" on as words (measured): say so.
        hint = (
            " (cmd.exe does not treat # as a comment: drop everything from the # on)"
            if any(e.startswith("#") for e in extras)
            else ""
        )
        if getattr(args, "command", None) == "secret":
            # Never echo them: one could be the secret, typed where m3 would ask for it.
            parser.error(
                f"{len(extras)} unexpected word(s) after the secret's name (not shown){hint}. "
                "A value is never given on the command line: `m3 secret set NAME` asks for it."
            )
        parser.error(f"unrecognized arguments: {' '.join(extras)}{hint}")

    if args.command is None:
        # Bare `m3` → run the bridge (the MCP stdio server). This is how agents
        # invoke it, so stdout MUST stay clean (it's the protocol channel).
        # But a HUMAN running bare `m3` in a terminal usually wanted help, not a
        # silent server — print a one-line hint to STDERR (never stdout) so it
        # doesn't corrupt the protocol, only when attached to a TTY.
        if sys.stdin.isatty() and sys.stdout.isatty():
            print(
                "m3: starting the MCP memory server (this is what your agent runs).\n"
                "    New here? Run `m3 setup` to install + wire your agent,\n"
                "    or `m3 --help` for all commands.  Ctrl-C to stop.",
                file=sys.stderr,
            )
        _run_bridge()
        return

    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
