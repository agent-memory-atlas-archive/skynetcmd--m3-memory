#!/usr/bin/env python3
"""m3 shared-embedder watchdog — liveness-based self-heal for all three OSes.

WHY THIS EXISTS (2026-09-13)
The tier-2 shared embed server (:8082) was down for 30 hours and nothing
brought it back. The sequence, reconstructed from the Windows event log:

  1. The server crashed 9 times in ~2.5h inside the GPU driver (nvcuda64.dll,
     0xc0000005) while that driver was in a bad state.
  2. Windows SCM applied its recovery policy — RESTART x3 — and, when the
     crashes kept coming, gave up permanently (event 7023, "Incorrect
     function"). That is SCM working AS DESIGNED.
  3. The driver was reinstalled the next day. The environment was healthy
     again, but SCM's restart budget was already spent, so nothing ever tried
     again. The server stayed stopped until a human noticed.

THE GAP THIS CLOSES — restart budgets have no memory. Every OS supervisor
(SCM FAILURE_ACTIONS, systemd StartLimitBurst, launchd's throttle) is designed
to stop retrying a service that keeps failing, so none of them can recover one
whose ENVIRONMENT healed after the budget was exhausted. A periodic liveness
check has no budget: it asks "is it serving?" every tick, forever, and so it
recovers on the first tick after the environment is fixed.

This is the same lesson as m3_loop_watchdog (a supervisor satisfied by a live
PID cannot see a wedged process) applied one layer out: a supervisor satisfied
by "I already tried" cannot see a healed environment.

DESIGN (§1 three-OS, §3 fail-loud/never-silent, §4 cheap, §10a one owner)
  * Detection is OS- and backend-agnostic: one HTTP GET of the server's own
    /health endpoint, the same contract every tier-2 implementation serves
    (the Rust binary and bin/embed_server_inproc.py are interchangeable here).
    Nothing in this module touches a database, so SQLite vs PostgreSQL is
    irrelevant to it by construction — and nothing imports the embedder, so a
    broken GPU stack cannot take the watchdog down with it.
  * Only the RESTART ACTION is per-OS, isolated in _restart_backend(), exactly
    as m3_loop_watchdog does it. Restart goes through the platform supervisor
    (sc.exe / systemctl / launchctl), never by spawning a server directly: two
    processes on one port is the mutual-exclusion failure the single-:8082
    contract exists to prevent.
  * SHARED MODE ONLY. When .embed_config.json does not enable shared mode this
    is a no-op — an unshared host has per-process tier-1 embedders and no
    :8082 to guard. Guarding a port nobody uses would be a §3 false alarm.
  * Backoff, so a server that crashes on startup is not a fork bomb. The
    budget resets every tick (that is the entire point), but a start is not
    attempted more often than _MIN_RESTART_GAP_S.
  * Degrade, never guess: the server is declared down only after
    _FAILED_CHECKS consecutive misses, so one slow /health during a model load
    does not trigger a needless bounce.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from m3_core.tier2_client import Diagnosis

_BIN = Path(__file__).resolve().parent
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

try:
    from _task_runtime import no_window_kwargs
except Exception:  # pragma: no cover - fallback if _task_runtime unavailable

    def no_window_kwargs() -> dict:
        # getattr, not subprocess.CREATE_NO_WINDOW: the attribute is
        # Windows-only and a direct reference fails mypy on the Linux runner.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        return {"creationflags": flags} if flags else {}


_WINDOWS_SERVICE = "m3-embed-server"
_SYSTEMD_UNIT = "m3-embed-server.service"
_LAUNCHD_LABEL = "com.m3memory.embedserver"

_DEFAULT_URL = "http://127.0.0.1:8082"
_HEALTH_TIMEOUT_S = 5.0
# Two consecutive misses before acting: one slow /health during a model load is
# normal and must not trigger a bounce (§3 — a false alarm is a violation).
_FAILED_CHECKS = 2
_MIN_RESTART_GAP_S = 600


def _os_name() -> str:
    """WMI-safe OS name — platform.system() hangs on a WMI query on
    Py3.14/Windows.

    Deliberately a COPY of m3_memory._platform.os_name, the seam's one
    exemption: a watchdog is the repair path and must run when the package it
    watches is half-upgraded or broken, so it cannot depend on importing it.
    tests/test_os_seam_watchdog_parity.py holds the copy to the seam.
    """
    if os.name == "nt":
        return "Windows"
    if sys.platform == "darwin":
        return "Darwin"
    return "Linux"


def _config_root() -> Path:
    """Config root, honoring the Homecoming overrides, from the stdlib-only roots
    owner (it imports even from a half-upgraded venv, so no fallback copy)."""
    from m3_core.seams import roots

    return Path(roots.config_root())


CONFIG_ROOT = _config_root()
STATE = CONFIG_ROOT / ".embed_watchdog_state.json"
EMBED_CONFIG = CONFIG_ROOT / ".embed_config.json"


def _log_path() -> Path:
    if _os_name() == "Darwin":
        return Path(os.path.expanduser("~/Library/Logs/m3_embed_watchdog.log"))
    return CONFIG_ROOT.parent / "logs" / "m3_embed_watchdog.log"


def log(msg: str) -> None:
    line = f"[{datetime.now(UTC).isoformat(timespec='seconds')}] {msg}"
    # Match the codebase's cp1252-safe print: the console encoding can reject
    # the stream's default even for ASCII-only payloads.
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
    try:
        p = _log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def shared_mode() -> tuple[bool, str]:
    """(enabled, url). Shared mode is the only configuration with a :8082 to
    guard; on an unshared host every process embeds in-process and this
    watchdog must stay silent."""
    try:
        with open(EMBED_CONFIG, encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except FileNotFoundError:
        return False, _DEFAULT_URL
    except (OSError, ValueError) as e:
        # §3: a malformed config is loud, never a silent revert to defaults.
        log(
            f"WARNING: {EMBED_CONFIG} is unreadable "
            f"(observed: {type(e).__name__}: {e}) — cannot tell whether shared "
            f"mode is on; standing down. inspect: that file, or `m3 embedder shared`"
        )
        return False, _DEFAULT_URL
    url = str(cfg.get("fallback_url") or _DEFAULT_URL).rstrip("/")
    try:
        # The cascade's own precedence: a set embed.fallback_url wins over this
        # file. The config package is heavier than this module; if it cannot
        # load, guard the file's URL and say so rather than stand down.
        from m3_core.tier2_client import fallback_url

        url = fallback_url(cfg)[0]
    except Exception as e:  # noqa: BLE001 — logged; the file's URL is still guarded
        log(
            f"WARNING: cannot resolve embed.fallback_url (observed: {type(e).__name__}: {e}); "
            f"guarding {url} from {EMBED_CONFIG}. inspect: `m3 config get embed.fallback_url`"
        )
    return bool(cfg.get("disable_inproc_embedder")), url


def _is_loopback_url(url: str) -> bool:
    """True when ``url`` names this machine. tier2_client imports only the
    stdlib, so the watchdog stays independent of the embedder."""
    from m3_core.tier2_client import is_loopback_host

    return is_loopback_host(urllib.parse.urlsplit(url).hostname)


def server_check(url: str) -> Diagnosis:
    """The classified /health answer (m3_core.tier2_client, stdlib-only): the
    server's own contract, identical for the Rust binary and the Python
    embed_server_inproc.py — neither is imported, only asked. The request
    carries the bearer token and TLS trust a token-protected or https server
    needs; without them a healthy server answers 401 or fails the handshake,
    which once read as "down" and restarted it every tick.

    The scheme is checked before the request. `url` comes from configuration,
    so a typo could hand urlopen a `file:` or custom scheme, which would read a
    local path instead of probing a server. tier2_client refuses it unread
    (bad-scheme), and the refusal is logged here.
    """
    from m3_core.tier2_client import health

    diag = health(url, _HEALTH_TIMEOUT_S)[0]
    if diag.state == "bad-scheme":
        log(f"refusing health probe: {url!r} is not an http(s) URL")
    return diag


def server_healthy(url: str) -> bool:
    """True when the server answers its health contract (ok or loading)."""
    return server_check(url).serving


def _read_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _write_state(d: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except OSError as e:
        # Without the state file the miss count and the restart backoff reset
        # every tick: a crash-looping server is then started every tick.
        log(f"WARNING: cannot write {STATE} (observed: {type(e).__name__}: {e}); backoff is not remembered")


def recently_restarted(state: dict) -> bool:
    last = state.get("last_restart_ts")
    if not last:
        return False
    return (time.time() - float(last)) < _MIN_RESTART_GAP_S


def _restart_backend() -> tuple[list[list[str]], str]:
    """(commands, description) to bring the embed server up via its platform
    supervisor. A START, not a restart: this only fires when the server is not
    serving, and a start is a no-op for one already running.

      Windows — sc.exe start. Needs Administrator; when the watchdog runs
                unelevated the command fails and we say so rather than
                pretending it worked (§3 observed-vs-inferred).
      Linux   — systemctl --user start.
      Darwin  — launchctl kickstart, never with -k: SIGKILL on a live server
                is the damage a watchdog exists to avoid.

    All three are registered by an install path, so this watchdog can act on
    every supported OS: Windows via `m3 embedder install` (the Rust binary
    with SCM), Linux/macOS via install_schedules.install_unix_embed_server()
    (the Python server as a systemd --user unit / launchd agent). A host that
    somehow has neither still gets an honest observed-level error from
    restart() naming the missing supervisor, never silence.
    """
    osn = _os_name()
    if osn == "Darwin":
        uid = os.getuid() if hasattr(os, "getuid") else 0
        return ([["launchctl", "kickstart", f"gui/{uid}/{_LAUNCHD_LABEL}"]], f"launchd {_LAUNCHD_LABEL}")
    if osn == "Linux":
        return ([["systemctl", "--user", "start", _SYSTEMD_UNIT]], f"systemd {_SYSTEMD_UNIT}")
    return ([["sc.exe", "start", _WINDOWS_SERVICE]], f"sc.exe {_WINDOWS_SERVICE}")


def restart(reason: str, state: dict) -> int:
    if recently_restarted(state):
        log(f"WOULD start ({reason}) but backoff window is open — standing down")
        return 0
    cmds, what = _restart_backend()
    log(f"STARTING via {what}: {reason}")
    started = False
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, **no_window_kwargs())
        except (OSError, subprocess.SubprocessError) as e:
            log(f"  observed: {cmd[0]} failed to spawn ({type(e).__name__}: {e}); inspect: `m3 embedder status`")
            continue
        if r.returncode == 0:
            started = True
            log(f"  observed: {cmd[0]} exit=0; the next tick checks that the server answers")
        else:
            # Name what was OBSERVED and what to inspect; never assert a cause
            # the code has not confirmed (§3).
            detail = (r.stderr or r.stdout or "").strip().splitlines()
            log(f"  observed: {cmd[0]} exit={r.returncode}" + (f" — {detail[0][:160]}" if detail else ""))
            if _os_name() == "Windows":
                log(
                    "  possible: insufficient rights — an unelevated watchdog "
                    "cannot start a LocalSystem service; "
                    "inspect: `m3 embedder status`"
                )
            else:
                log(
                    f"  possible: no {_SYSTEMD_UNIT if _os_name() == 'Linux' else _LAUNCHD_LABEL} "
                    f"is registered on this host; inspect: `m3 embedder status`; "
                    f"register the service: `m3 embedder install`"
                )
    if not started:
        log(f"  observed: the start did not succeed; next attempt after the {_MIN_RESTART_GAP_S}s backoff")
    state["last_restart_ts"] = time.time()
    state["consecutive_failures"] = 0
    _write_state(state)
    return 1


def main() -> int:
    enabled, url = shared_mode()
    if not enabled:
        # Not an error and not a warning: an unshared host has no :8082 by
        # design, and a message here would be the false alarm §3 forbids.
        return 0
    if not _is_loopback_url(url):
        # The shared server is on another machine (its listener for other
        # machines); that machine's own watchdog supervises it. Restarting the
        # local service could not bring a remote server back.
        return 0

    state = _read_state()
    diag = server_check(url)
    if diag.serving:
        if state.get("consecutive_failures") or state.get("answering_fault"):
            log(f"OK: {url} healthy again after {state.get('consecutive_failures') or 0} failed check(s)")
        state["consecutive_failures"] = 0
        state.pop("answering_fault", None)
        _write_state(state)
        return 0

    if diag.answering:
        # Reachable but not serving: a 401, an untrusted certificate, an
        # unhealthy reply. Starting the service cannot change that (a start is
        # a no-op for a running one), so say it once per state, never restart.
        if state.get("answering_fault") != diag.state:
            log(f"observed: {url} answers but is not serving ({diag.state}): {diag.message()}. not restarting it")
        state["answering_fault"] = diag.state
        state["consecutive_failures"] = 0
        _write_state(state)
        return 0

    state.pop("answering_fault", None)
    fails = int(state.get("consecutive_failures") or 0) + 1
    state["consecutive_failures"] = fails
    _write_state(state)

    if fails < _FAILED_CHECKS:
        log(f"observed: {diag.detail} ({fails}/{_FAILED_CHECKS}) — waiting one more tick before acting")
        return 0

    return restart(
        f"observed: {diag.detail}, on {fails} consecutive checks; shared mode is ON, so every m3 process depends on it",
        state,
    )


if __name__ == "__main__":
    raise SystemExit(main())
