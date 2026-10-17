"""Environment coherence probe — does every wired path point at THIS install?

m3's runtime is spread across artifacts that each bake an ABSOLUTE path at
install time: Claude Code hook commands (interpreter + script), the console
launchers on PATH, the payload directory, and the venv holding it. Nothing
reconciles them afterwards. A reinstall, a relocation, or a second install via
a different mechanism moves one and leaves the others pointing at where it used
to be — and every one of those failures is SILENT.

The chatlog hooks are the highest-stakes case. They look like:

    <venv>/Scripts/python.exe <venv>/…/m3_memory/bin/hooks/chatlog/…py

Both halves live inside the pipx venv, which every reinstall churns. If either
goes stale the hook fails per-turn with no surfaced error and conversation
capture stops. That is the documented 48-hour context-loss failure in CLAUDE.md,
and until now nothing checked for it: agent_paths_probe covers MCP *server*
configs (mcpServers / OpenCode / Hermes), never Claude Code's `hooks` block.

What this probe checks, all by reading the real artifact rather than a config
flag that merely claims the thing is true:

  1. Every m3-owned hook command's interpreter and script EXIST on disk.
  2. Each hook's interpreter belongs to the same install as the running payload,
     or to another venv on the same m3 release (a hook pinned to an old venv
     runs different code than `m3` does).
  3. Config that claims a hook is wired is corroborated against settings.json.
     `chatlog_status` takes its claude-code "wired" from this probe rather than
     echoing the config flag, which asserted instead of measured.

Root pins are no longer required: m3 resolves its roots from the config file,
and only a non-default M3_CONFIG_ROOT is written into a hook (one-env-var rule).

DETECTION ONLY by default. Repair is behind `m3 doctor --fix --fix-hooks`,
because settings.json is the USER'S file — it holds non-m3 hooks, and a bad
merge breaks their whole Claude Code setup, not just m3. See repair() for the
backup-and-diff contract.

Bumps the doctor exit code on a broken hook: capture being silently dead is a
real degraded state, not a preference. Cross-platform, network-free, never
raises.
"""

from __future__ import annotations

import json
import os
import re
from typing import cast

from m3_core.seams import is_windows

# Ownership of a hook command: generate_configs._is_m3_command, the test the
# repair writer (install_claude_settings) strips by. Detection and repair must
# use the SAME test, or repair misses entries it should fix or touches entries
# it does not own.

# Hook events m3 wires. SessionStart is capture-health only; PreCompact and Stop
# are the actual turn-capture path.
CAPTURE_EVENTS = ("PreCompact", "Stop")


def _capture_support_gap(agent: str) -> str | None:
    """Why `agent` cannot be captured, or None if the machinery is all present.

    Checks the two things that make capture structurally impossible regardless
    of how the host's own config is written: a parser to read its transcript,
    and a hook file to invoke. Both were missing for agents that `chatlog
    status` nonetheless reported as `wired: true`, because that field is the
    config's own claim echoed back rather than evidence.

    Deliberately NOT a verdict on the host's config file — this probe cannot
    read OpenClaw's or Gemini's. Absence of a gap means "m3's side is ready",
    not "capture is confirmed running".
    """
    try:
        import chatlog_ingest
        import chatlog_init
        from chatlog_config import LIBRARY_HOST_AGENTS
    except Exception:  # noqa: BLE001 — probe context, never fatal
        return None
    if agent in LIBRARY_HOST_AGENTS:
        return None  # submits turns through the API: no parser or hook to need

    reasons = []
    if agent not in getattr(chatlog_ingest, "PARSERS", {}):
        reasons.append(f"m3 has no transcript parser for it (`--format {agent}` is not a valid ingest format)")
    try:
        sh_path, _ps1, desc = chatlog_init.get_hook_path_for_agent(agent)
        if "unknown" in desc or not os.path.exists(sh_path):
            reasons.append("no hook script ships for it under bin/hooks/chatlog")
    except Exception:  # noqa: BLE001
        pass
    return " and ".join(reasons) if reasons else None


def _claude_settings_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def _load_settings() -> dict | None:
    """Parsed settings.json, or None when absent/unreadable.

    None means "cannot check" and must never be reported as healthy.
    """
    try:
        with open(_claude_settings_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 — absent or malformed both mean "unknown"
        return None
    return cast("dict", data) if isinstance(data, dict) else None


def _m3_hook_entries(settings: dict) -> list[tuple[str, str]]:
    """[(event, command), …] for every m3-owned hook entry."""
    from generate_configs import _is_m3_command, _m3_repo_root

    root_fwd = _m3_repo_root().replace(os.sep, "/")
    out: list[tuple[str, str]] = []
    hooks = settings.get("hooks")
    for event, entries in (hooks if isinstance(hooks, dict) else {}).items():
        for entry in entries if isinstance(entries, list) else []:
            inner = entry.get("hooks") if isinstance(entry, dict) else None
            for hook in inner if isinstance(inner, list) else []:
                command = str(hook.get("command", "")) if isinstance(hook, dict) else ""
                if _is_m3_command(command, root_fwd):
                    out.append((event, command))
    return out


def _extract_paths(command: str) -> list[str]:
    """Absolute filesystem paths referenced by a hook command string.

    Hook commands come in several shapes across platforms and install vintages
    (`powershell -File <ps1>`, `<python.exe> <script.py>`, `/bin/sh <sh>`), often
    behind `set VAR=… &&` prefixes. Rather than parse each shape, pull out
    anything that looks like an absolute path to a real file type — shape-
    agnostic, so a new launcher form does not silently go unchecked.
    """
    # A quoted path may hold spaces (generate_configs._quote_cmd_path); POSIX
    # interpreters have no extension (…/venv/bin/python3), so match those too.
    ext = r"\.(?:exe|py|ps1|sh|cmd|bat)|/python[0-9.]*"
    quoted = rf'"((?:[A-Za-z]:[\\/]|/)[^"]+?(?:{ext}))"|\'((?:[A-Za-z]:[\\/]|/)[^\']+?(?:{ext}))\''
    bare = rf'(?:[A-Za-z]:[\\/]|/)[^\s"\'&|;]+(?:{ext})(?=$|[\s"\'&|;])'
    seen: list[str] = []
    for m in re.finditer(rf"{quoted}|({bare})", command):
        cleaned = next(g for g in m.groups() if g)
        if cleaned not in seen:
            seen.append(cleaned)
    return seen


def _is_interpreter(path: str) -> bool:
    """A Python interpreter path (python.exe, pythonw.exe, …/bin/python3)."""
    return os.path.basename(path).lower().startswith("python")


def _payload_root() -> str:
    """Directory of the m3_memory package this process is running from."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _is_installed_layout(payload_root: str) -> bool:
    """Is the running payload an INSTALLED package (vs. a repo checkout)? The
    one owner is generate_configs._is_installed_layout (site- AND dist-packages).

    The same-install comparison is only meaningful for an installed layout. From
    a checkout the hooks correctly point at the installed venv — a different tree
    by design — so the comparison is skipped there.
    """
    from generate_configs import _is_installed_layout as owner

    return owner(os.path.abspath(payload_root))


def _venv_m3_version(interpreter: str) -> str | None:
    """The m3-memory version installed in an interpreter's venv, read from its
    ``m3_memory-<version>.dist-info`` (no subprocess), or None when none is found."""
    import glob

    bin_dir = os.path.dirname(os.path.abspath(interpreter))
    venv = os.path.dirname(bin_dir)
    patterns = (
        os.path.join(venv, "Lib", "site-packages", "m3_memory-*.dist-info"),  # Windows
        os.path.join(venv, "lib", "python3*", "site-packages", "m3_memory-*.dist-info"),  # POSIX
    )
    for pat in patterns:
        for d in sorted(glob.glob(pat)):
            name = os.path.basename(d)[len("m3_memory-") : -len(".dist-info")]
            if name:
                return name
    return None


def _same_m3_release(interpreter: str) -> bool:
    """Whether a hook's interpreter runs the same m3 release as this process.
    Another venv with the same version is the same code: in a Claude session the
    running payload is the plugin's copy, while hooks rightly use the install the
    `m3` CLI and services run from, and judging by tree alone flagged that as
    foreign (2026-10-17)."""
    try:
        from m3_memory import __version__ as running
    except ImportError:
        return False
    return _venv_m3_version(interpreter) == running


def _same_install(path: str, payload_root: str) -> bool:
    """Is `path` inside the same install tree as the running payload?

    Compared case-insensitively with normalized separators: hook commands are
    written with forward slashes on Windows while os.path yields backslashes,
    so a naive comparison reports a false mismatch on every Windows box.
    """

    def norm(p: str) -> str:
        return os.path.normcase(os.path.abspath(p)).replace("\\", "/").rstrip("/")

    # Anchor on the VENV ROOT specifically, not "some ancestor". The interpreter
    # lives in <venv>/Scripts (or <venv>/bin) while the payload lives in
    # <venv>/Lib/site-packages/m3_memory, so the shared ancestor is the venv.
    # Walking up a fixed number of levels instead would climb PAST the venv into
    # its parent directory, and every sibling venv there would then look like
    # "the same install" — the check would pass on exactly the mismatch it is
    # meant to catch.
    payload = norm(payload_root)
    idx = max(payload.find("/site-packages/"), payload.find("/dist-packages/"))
    if idx == -1:
        # Running from a REPO CHECKOUT, not an installed layout. The hooks
        # legitimately point at the installed venv, which is a different tree by
        # design, so "same install" is not a meaningful question here — the
        # doctor is simply being run from source. Returning False would flag
        # every correctly-wired hook on every dev machine. Treat as
        # not-comparable (the caller skips the check).
        return True

    # <venv>/Lib/site-packages/... -> <venv>
    anchor = os.path.dirname(payload[:idx])
    return norm(path).startswith(anchor + "/")


def check() -> dict:
    """Pure detection. Returns {'status', 'findings', 'hooks_seen'}.

    status is one of:
      'ok'       — every wired path verified against disk
      'unknown'  — settings.json absent/unreadable; nothing was verified
      'issues'   — at least one finding
    """
    settings = _load_settings()
    # No ~/.claude/settings.json is normal for a user on another host, and the
    # check of the OTHER enabled agents below must still run: returning here
    # left exactly those users without the finding (CI, 2026-10-17).
    entries = _m3_hook_entries(settings) if settings is not None else []
    payload_root = _payload_root()
    findings: list[dict] = []

    for event, command in entries:
        paths = _extract_paths(command)
        if not paths:
            findings.append(
                {
                    "kind": "unparsable",
                    "event": event,
                    "detail": "no filesystem path found in the hook command; cannot verify it points at a real script",
                }
            )
            continue

        for path in paths:
            if not os.path.exists(path):
                findings.append(
                    {
                        "kind": "dead_path",
                        "event": event,
                        "path": path,
                        "detail": "hook target does not exist — this hook fails "
                        "silently on every fire, so capture is DEAD",
                    }
                )
            elif (
                _is_interpreter(path)
                and _is_installed_layout(payload_root)
                and not _same_install(path, payload_root)
                and not _same_m3_release(path)
            ):
                findings.append(
                    {
                        "kind": "foreign_interpreter",
                        "event": event,
                        "path": path,
                        "detail": "hook runs an interpreter outside the running "
                        "payload's install — it executes different m3 code",
                    }
                )

        # No root pins are required, on any OS: m3 reads one variable,
        # M3_CONFIG_ROOT, and takes every root from m3_config.toml (user decision
        # 2026-10-17). This probe once required inline M3_ENGINE_ROOT /
        # M3_CONFIG_ROOT on POSIX hooks and reported their absence as "CAPTURE
        # MAY BE DEAD" while capture worked, and `--fix-hooks` put the pins back.
        # On Windows the inline form is not just unneeded but fatal: cmd.exe
        # cannot run `VAR=val cmd`, which the branch below catches.
        if event in CAPTURE_EVENTS and is_windows():
            # Windows: the FATAL shape is the opposite — a POSIX prefix present.
            # A command whose first token carries an `=` is one cmd.exe cannot
            # run, so capture really is dead. This is the check that would have
            # caught the 2026-08-09..11 outage on the day it started.
            first = command.split(" ", 1)[0] if command else ""
            if "=" in first:
                findings.append(
                    {
                        "kind": "posix_prefix_on_windows",
                        "event": event,
                        "detail": f"command starts with {first!r} — a POSIX "
                        "`VAR=val cmd` prefix that cmd.exe cannot parse. "
                        "The hook fails instantly on every fire "
                        "('is not recognized as an internal or external "
                        "command'), so capture is DEAD",
                    }
                )

    # Corroborate the config's own claim against the file. `wired` in
    # chatlog_status is spec.enabled echoed back, not evidence.
    config_error: str | None = None
    try:
        import chatlog_config

        cfg = chatlog_config.resolve_config()
        claimed = {n for n, s in cfg.host_agents.items() if s.enabled}
        if "claude-code" in claimed and not entries and settings is not None:
            findings.append(
                {
                    "kind": "claimed_not_wired",
                    "event": "claude-code",
                    "detail": "config reports the claude-code hook enabled, but "
                    "settings.json contains no m3 hook entry — capture "
                    "is not actually running",
                }
            )

        # ⚠ EVERY OTHER AGENT WAS INVISIBLE HERE. This probe reads only
        # ~/.claude/settings.json, so for a user on OpenClaw (or Gemini, or
        # OpenCode) it inspected a file with nothing to do with them, found
        # nothing, and reported clean — including under `--fix --fix-hooks`,
        # whose repair path is install_claude_settings. A user reported exactly
        # that on 2026-09-20: chatlog never wired for OpenClaw, and doctor never
        # said so.
        #
        # A full per-agent wiring check needs each host's own config format, so
        # this does the part that is both cheap and decisive: an agent cannot be
        # capturing if m3 has no way to ingest it. That catches the real defect
        # class (registered-but-unsupported) without pretending to verify a
        # config file this probe cannot read.
        for agent in sorted(claimed - {"claude-code"}):
            gap = _capture_support_gap(agent)
            if gap:
                findings.append(
                    {
                        "kind": "claimed_not_supported",
                        "event": agent,
                        "detail": f"config reports the {agent} hook enabled, but "
                        f"{gap} — capture cannot run for this agent",
                    }
                )
    except Exception as e:  # noqa: BLE001 — reported as an unverified claim check
        config_error = f"{type(e).__name__}: {e}"

    if findings:
        status = "issues"
    elif settings is None or config_error:
        # settings.json absent: Claude Code's wiring was not verified. Config
        # unreadable: which agents SHOULD be wired is unknown. "OK" would be
        # unverified either way.
        status = "unknown"
    else:
        status = "ok"
    return {
        "status": status,
        "findings": findings,
        "hooks_seen": len(entries),
        "config_error": config_error,
    }


def repair(dry_run: bool = True) -> dict:
    """Re-wire m3-owned hook entries. Behind `--fix --fix-hooks`.

    Contract, in order:
      1. Never touch an entry that fails the ownership test — the user's own
         hooks in the same file must survive untouched.
      2. Always write a timestamped backup of settings.json first.
      3. dry_run=True (the DEFAULT) reports the intended change and writes
         nothing, so the operator sees the diff before consenting.

    Repair delegates to the installer's canonical writer rather than editing
    JSON here: that path already owns idempotent merge + statusLine handling,
    and a second writer would be a second thing to keep in sync.
    """
    result: dict = {"dry_run": dry_run, "backup": None, "actions": []}
    findings = check()["findings"]
    if not findings:
        result["actions"].append({"action": "rewire", "status": "skipped", "detail": "no hook issues to repair"})
        return result

    if dry_run:
        result["actions"].append(
            {
                "action": "rewire",
                "status": "skipped",
                "detail": f"dry_run=True; would back up settings.json and re-wire "
                f"{len(findings)} issue(s) via chatlog_init",
            }
        )
        return result

    settings_path = _claude_settings_path()
    # install_claude_settings backs settings.json up (<name>.bak + a
    # .backup_history section) and writes nothing when it cannot.
    from m3_core.atomic_io import backup_paths

    backup = str(backup_paths(settings_path)[0])

    try:
        import generate_configs

        # THE canonical Claude settings writer: it replaces the m3-owned
        # hooks / statusLine / mcpServers entries with pinned .py hooks and the
        # correct decoupled roots in ONE idempotent pass, so the doctor's hook
        # repair and `--fix-hooks`'s payload rewrite use the same writer and the
        # same format (no /bin/sh-then-.py churn), and this path now also corrects
        # the server env root blocks. install_claude_settings strips m3-owned
        # entries itself, so no separate drop step is needed.
        res = generate_configs.install_claude_settings(assume_yes=True)
        result["backup"] = backup if res.get("changed") else None
        result["actions"].append(
            {
                "action": "rewire",
                "status": "ok",
                "detail": f"re-wired {len(findings)} issue(s) via install_claude_settings "
                f"(changed={res.get('changed')}); backup at {result['backup'] or 'none (nothing written)'}",
            }
        )
    except Exception as e:  # noqa: BLE001
        result["actions"].append(
            {
                "action": "rewire",
                "status": "error",
                "detail": f"{type(e).__name__}: {e}; settings.json is unchanged or restorable from {backup}",
            }
        )
    return result


def run(brief: bool = False, fix: bool = False) -> int:
    """Print findings; return 1 when capture is broken, else 0."""
    try:
        res = check()
    except Exception as e:  # noqa: BLE001 — the doctor must never die on a probe
        if not brief:
            print(f"  environment: probe failed (non-fatal): {type(e).__name__}: {e}")
        return 0

    if res["status"] == "unknown" and res.get("config_error"):
        print(
            f"⚠️  chatlog hooks: SKIPPED — the chatlog config could not be read ({res['config_error']}); "
            f"{res['hooks_seen']} hook path(s) checked, but which agents should capture was NOT verified. "
            "Diagnose: m3 chatlog doctor"
        )
        return 0

    if res["status"] == "unknown":
        import shutil

        if shutil.which("claude") is None and not os.path.isdir(os.path.dirname(_claude_settings_path())):
            # No Claude Code here at all: nothing to verify, and nothing wrong.
            print("chat capture: Claude Code is not installed here (nothing to check)")
            return 0
        # Not "healthy" — nothing was verified. Saying OK here would be the
        # same lie this probe exists to catch.
        print("⚠️  chatlog hooks: SKIPPED — ~/.claude/settings.json absent or unreadable; hook wiring was NOT verified")
        return 0

    if res["status"] == "ok":
        # `brief` means ONE LINE, not silence — see entrypoint_probe.run.
        print(f"✅ chatlog hooks: OK ({res['hooks_seen']} verified against disk)")
        return 0

    print("⚠️  chatlog hooks: [FAIL] wiring does not match this install — CAPTURE MAY BE DEAD")
    for f in res["findings"]:
        where = f.get("path") or f.get("event")
        print(f"    - {f['event']} ({f['kind']}): {where}")
        print(f"      {f['detail']}")

    if fix:
        rep = repair(dry_run=False)
        for act in rep["actions"]:
            print(f"      [{act['status']}] {act['action']}: {act['detail']}")
    else:
        print("      fix: m3 doctor --fix --fix-hooks")
        print("           (backs up settings.json first; only m3-owned entries are touched)")

    return 1
