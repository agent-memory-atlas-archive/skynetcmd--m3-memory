"""End-of-run summary rendering for the setup wizard.

Extracted verbatim from setup_wizard.py. Pure rendering — not monkeypatched
by any test (confirmed via grep) and does not call any of the 7 patched
functions, so it's safe to live in a submodule.
"""

from __future__ import annotations

from m3_memory._platform import is_macos, is_windows

from .ui import _ok, _warn


def _custom_config_root(config_root: str) -> bool:
    """True when the chosen config folder is not the default (~/.m3/config)."""
    import os

    default = os.path.normcase(os.path.normpath(os.path.expanduser(os.path.join("~", ".m3", "config"))))
    return bool(config_root) and os.path.normcase(os.path.normpath(os.path.expanduser(config_root))) != default


def _summary(plan, governor_result: dict | None = None, verified: bool = True, brief: bool = False) -> None:
    """End-of-run summary so the user knows exactly what to do next.

    ``verified`` is the doctor's verdict from _step_doctor. It used to print
    "Setup complete." unconditionally — including over a doctor run that had
    just failed — which is the silent-success pattern the doctor exists to
    catch. The headline now states what is actually true.

    ``brief`` is for `m3 upgrade`, which prints its own summary after this:
    only what needs action is kept (a failed verification, governor tasks
    that could not be removed).
    """
    if brief:
        _brief_summary(plan, governor_result, verified)
        return
    print()
    if verified:
        _ok("Setup complete — verified healthy.")
    else:
        _warn("Setup finished, but VERIFICATION FAILED.")
        _warn(
            "m3 is installed and may partly work; the lines marked [!] above "
            "name each issue and its fix. Do not assume capture or search "
            "is running until they are fixed."
        )
    print()
    restart_lines = []
    if plan.targets.claude:
        restart_lines.append("  • Claude Code              — restart the CLI (or run `/plugin reload`)")
    if plan.targets.gemini:
        restart_lines.append("  • Gemini CLI               — restart the CLI")
    if plan.targets.antigravity:
        restart_lines.append("  • Antigravity CLI/Desktop  — restart the CLI/Desktop")
    if plan.targets.opencode:
        restart_lines.append("  • OpenCode                 — restart the CLI")
    if plan.targets.openclaw:
        restart_lines.append("  • OpenClaw                 — restart the CLI/gateway")
    if restart_lines:
        print("Next step — restart your agent so it picks up the new MCP server:")
        for line in restart_lines:
            print(line)
    elif plan.agents_kept:
        print(
            "Agent choices kept (paths into an older install repaired). Restart your agents so they load the new version."
        )
    # No agents wired: the closing line below says so once.
    print()
    # Every choice setup made is in m3_config.toml. The one thing a file cannot
    # carry is where that file is: a config folder outside the default needs
    # the M3_CONFIG_ROOT locator, so say so then, and only then.
    _custom_root = plan.decouple_roots and _custom_config_root(plan.config_root)
    if _custom_root:
        import os as _os
        import sys as _sys

        _is_win = _sys.platform == "win32"

        # Emit COPY-PASTE-USABLE commands for the actual shell: `setx` (persists
        # to the user environment) on Windows, `export` on POSIX. And show NATIVE
        # paths — normpath so Windows gets all-backslash (not the mixed
        # "<HOME>\.m3/config" mixed-separator form that expanduser leaves).
        def _p(path: str) -> str:
            return _os.path.normpath(path) if path else path

        print("Your settings folder:")
        print(f"  {_p(plan.config_root)} is not the default, so m3 finds it only through")
        if _is_win:
            print("  M3_CONFIG_ROOT. Run once, then open a new terminal:")
            print(f'    setx M3_CONFIG_ROOT "{_p(plan.config_root)}"')
        else:
            print("  M3_CONFIG_ROOT. Add to your shell profile (~/.bashrc, ~/.zshrc or ~/.profile):")
            print(f'    export M3_CONFIG_ROOT="{_p(plan.config_root)}"')
        print()

    # ── governor migration results ─────────────────────────────────────────
    if governor_result:
        removed = governor_result.get("removed", [])
        failed = governor_result.get("failed", [])
        cmds = governor_result.get("privileged_cmds", [])

        # Tasks that simply stay on their schedule need no word: listing them
        # (with internal names) told a new user nothing they could act on.
        if removed or failed:
            print("Background jobs:")
        if removed:
            print(f"  {len(removed)} scheduled job(s) replaced by m3's own pacing:")
            for name in removed:
                print(f"    • {name}")
        if failed:
            print()
            _warn(f"Could not remove {len(failed)} scheduled task(s) — insufficient privilege.")
            print("  Run these PRIVILEGED, OS-specific commands to remove them cleanly,")
            print("  then the governor (already active in-process) fully owns that work:")
            print()
            if _os_name_for_summary() == "Windows":
                print("  → Open an ELEVATED (Administrator) PowerShell or Command Prompt and run:")
            else:
                print("  → Run in your shell (prefix with sudo only if it's a system/root crontab):")
            for c in cmds:
                print(f"      {c}")
        if removed or failed:
            print()

    # ── embedder tier (Project Oxidation status) ────────────────────────────
    try:
        from m3_memory.rust_core_install import active_embedder_tier

        tier = active_embedder_tier()
        print("Embedder:")
        if tier.get("native") and not tier.get("others_server"):
            _ok(f"  {tier['summary']}")
        else:
            _warn(f"  {tier['summary']}")
        print()
    except Exception:  # noqa: BLE001 — summary is best-effort
        pass

    # ── clear "you're done" closer ──────────────────────────────────────────
    print("─" * 60)
    if not verified:
        # "live" is a health claim; the doctor just declined to make it.
        _warn("M3 is installed but failed verification. Fix what is marked")
        print("    [!] above, then run `m3 doctor` again.")
    elif plan.targets.any():
        _ok("M3 is installed and live. Restart your agent (above) and your")
        print("    memory + chatlog start working immediately — nothing else to do.")
    elif plan.agents_kept:
        _ok("M3 is updated and live. Agent wiring is unchanged.")
    else:
        _ok("M3 is installed. No agents were wired — run `m3 setup` again and")
        print("    pick at least one agent, or add the MCP server by hand.")
    print()
    print("  Try it:   m3 status      # one-line health check")
    print("            m3 doctor      # full diagnostics")
    print("            m3 --help      # every command")
    print("─" * 60)
    print()


def _brief_summary(plan, governor_result: dict | None, verified: bool) -> None:
    # Verified: the "services running" line and the upgrade's own summary say it.
    if not verified:
        print()
        _warn("Setup finished, but VERIFICATION FAILED — see the output above.")
    failed = (governor_result or {}).get("failed", [])
    if failed:
        _warn(
            f"Could not remove {len(failed)} legacy scheduled task(s) — insufficient "
            "privilege. Run `m3 setup` from an elevated shell to finish."
        )


def _os_name_for_summary() -> str:
    """Thin OS branch for summary phrasing (avoids importing governor_migration
    just for the OS check)."""
    if is_windows():
        return "Windows"
    if is_macos():
        return "Darwin"
    return "Linux"
