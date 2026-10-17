"""Run several administrator actions under ONE Windows UAC prompt.

Hazard: setup used to raise a separate UAC dialog for each privileged step
(boot-task registration, legacy task removal, ...), so one run could ask for
consent several times, and a dialog lost behind the terminal silently dropped
its step. Queuing the steps and running them in a single elevated PowerShell
process asks once; the per-step exit codes come back through a results file, so
each step is still reported as done or not.

Windows only. Never raises; a cancelled prompt or a missing results file means
no step ran.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - fixed argv lists, no shell
import tempfile
from datetime import datetime

from m3_memory._platform import is_elevated, is_windows


def _ps_quote(s: str) -> str:
    """A PowerShell single-quoted literal (a ' inside is doubled)."""
    return "'" + str(s).replace("'", "''") + "'"


# The words every prompt is framed with (ElevationBatch.run owns the framing).
ELEVATION_NOTICE = "You will be asked to authorize actions requiring elevated privileges:"
APPROVED = "approved"
DISAPPROVED = "disapproved"
TIMED_OUT = "timed out"
NOT_NEEDED = "not needed (this process is already elevated)"
NOT_CONFIRMED = "not confirmed"  # the launcher failed; whether anything ran is unknown
_ERROR_CANCELLED = 1223  # Win32: the user declined the UAC prompt
_LAUNCH_FAILED = 9009  # the launcher failed for a reason other than a declined prompt


class ElevationBatch:
    """Queued (label, argv) actions, run together under one UAC prompt."""

    def __init__(self) -> None:
        self.actions: list[tuple[str, list[str]]] = []

    def add(self, label: str, argv: list[str]) -> None:
        """Queue an action; the same command queued twice runs once."""
        if all(list(argv) != a for _, a in self.actions):
            self.actions.append((label, list(argv)))

    def __len__(self) -> int:
        return len(self.actions)

    def render_script(self, results_path: str, log_dir: str, expires_epoch: int | None = None) -> str:
        """The PowerShell the elevated process runs: each action in order, its
        output to its own log and its exit code under its label, then the codes
        written as JSON.

        `expires_epoch` (Unix seconds, UTC): a prompt approved after this runs
        nothing. A dialog outlives the setup that raised it when that setup is
        killed, and was once approved 44 minutes later, restarting a service
        mid-way through an unrelated run. Epoch seconds, not a parsed date:
        [datetime]::Parse turns "+00:00" into LOCAL time, so every prompt read
        as expired west of UTC.
        """
        lines = ["$ErrorActionPreference = 'Continue'", "$r = [ordered]@{}"]
        if expires_epoch is not None:
            lines.append(
                f"if ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -gt {int(expires_epoch)}) {{ "
                f"@{{expired = 1}} | ConvertTo-Json | Set-Content -LiteralPath "
                f"{_ps_quote(results_path)} -Encoding UTF8; exit 0 }}"
            )
        for i, (label, argv) in enumerate(self.actions):
            call = "& " + " ".join(_ps_quote(a) for a in argv)
            log = _ps_quote(os.path.join(log_dir, f"{i}.log"))
            lines.append(
                f"try {{ {call} *> {log}; $r[{_ps_quote(label)}] = $LASTEXITCODE }} "
                f"catch {{ $_ | Out-File -LiteralPath {log} -Append; "
                f"$r[{_ps_quote(label)}] = -1 }}"
            )
        lines.append(f"$r | ConvertTo-Json | Set-Content -LiteralPath {_ps_quote(results_path)} -Encoding UTF8")
        return "\r\n".join(lines) + "\r\n"

    def run(self, timeout: int = 300) -> dict[str, dict] | None:
        """Run every queued action in one elevated process. Returns
        {label: {"rc": exit code, "output": last lines}}, or None when the
        prompt was declined, expired, or the elevated process wrote no results
        (then nothing can be assumed to have run).

        Every prompt is framed the same way, by this primitive and not its
        callers: the standard notice, the steps and the time it was requested
        before the dialog, then "permission approved | disapproved | timed out"
        with the time it ended, printed and appended to <logs root>/elevation.log
        (local time and UTC both). ``self.outcome`` keeps that word. A process
        already elevated gets no dialog; its outcome says so.

        `timeout` bounds the wait for the dialog. It stays well inside the
        caller's own budget (`m3 upgrade` gives setup 900 s), so a missed
        prompt costs this step, not the whole setup; past it, the script
        refuses to run (see render_script).
        """
        self.outcome: str | None = None
        if not self.actions:
            return {}
        if not is_windows():
            return None
        import time
        from datetime import UTC, datetime

        expires = int(time.time()) + timeout
        already = is_elevated()
        tmp = _workdir()
        started = datetime.now(UTC)
        try:
            self.announce(started, already=already)
            out, self.outcome, detail = self._run_in(tmp, timeout, expires)
        finally:
            _remove_workdir(tmp)
        if already and self.outcome == APPROVED:
            self.outcome = NOT_NEEDED
        self._report(self.outcome, detail, started, datetime.now(UTC))
        return out

    def purpose(self) -> str:
        """What the prompt is for, one line: "m3: <label>; <label>".

        UAC shows no text a program supplies. Its dialog names the program
        (Windows PowerShell here) and, under "Show more details", the command
        line; this line rides on that command line as an argument the script
        ignores, so the details say what approving does."""
        text = "m3: " + "; ".join(label for label, _argv in self.actions)
        # It is quoted on the command line: a double quote would end the argument.
        return text.replace('"', "'")

    def announce(self, started: datetime, *, already: bool = False) -> None:
        """The standard notice before every prompt, with the steps it covers and
        when it was requested: the dialog itself names only Windows PowerShell."""
        steps = "\n".join(f"  - {label}" for label, _argv in self.actions)
        where = (
            "  (this process is already elevated: no prompt appears)"
            if already
            else '  (the prompt names Windows PowerShell; "Show more details" lists these steps)'
        )
        print(f"{ELEVATION_NOTICE}\n{steps}\n{where}\n  requested {_when(started)}", flush=True)

    def _report(self, outcome: str, detail: str, started: datetime, ended: datetime) -> None:
        """`permission <outcome>` (and which condition, when the word covers more
        than one) with the time it ended, on the console and in
        <logs root>/elevation.log. A log line leads with the UTC end time, so the
        file sorts; requested and ended follow in local time and UTC."""
        took = f"{(ended - started).total_seconds():.0f}s"
        said = f"permission {outcome}" + (f" ({detail})" if detail else "")
        print(f"{said}  ended {_when(ended)}, after {took}", flush=True)
        from m3_memory._roots import logs_root

        log = os.path.join(logs_root(), "elevation.log")
        try:
            os.makedirs(logs_root(), exist_ok=True)
            with open(log, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(
                    f"{_utc_iso(ended)} {said} | requested {_when(started)} | ended {_when(ended)} "
                    f"| after {took} | {self.purpose()}\n"
                )
        except OSError as e:
            print(f"  (could not record it in {log}: {type(e).__name__}: {e})", flush=True)

    def _run_in(self, tmp: str, timeout: int, expires: int) -> tuple[dict[str, dict] | None, str, str]:
        """(results or None, outcome word, which condition when the word covers
        several, else "")."""
        script = os.path.join(tmp, "actions.ps1")
        results = os.path.join(tmp, "results.json")
        with open(script, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(self.render_script(results, tmp, expires_epoch=expires))
        # Process.Start with the runas verb, not Start-Process: a declined prompt
        # throws Win32Exception 1223 (ERROR_CANCELLED) from it, which becomes the
        # exit code. Start-Process reported a decline only as a localized message
        # (measured 2026-10-17: a "No" read as a launcher failure). Arguments is one
        # string, so the script path and the purpose carry their own double quotes.
        args = f"-NoProfile -ExecutionPolicy Bypass -File {chr(34)}{script}{chr(34)} {chr(34)}{self.purpose()}{chr(34)}"
        launcher = (
            "$i = New-Object System.Diagnostics.ProcessStartInfo; $i.FileName = 'powershell'; "
            f"$i.Arguments = {_ps_quote(args)}; $i.Verb = 'runas'; $i.UseShellExecute = $true; "
            "$i.WindowStyle = 'Hidden'; "
            "try { $p = [System.Diagnostics.Process]::Start($i); $p.WaitForExit(); exit $p.ExitCode } "
            "catch { $e = $_.Exception; while ($e) { if ($e -is [System.ComponentModel.Win32Exception]) "
            f"{{ exit $e.NativeErrorCode }}; $e = $e.InnerException }}; "
            f"Write-Output $_.Exception.ToString(); exit {_LAUNCH_FAILED} }}"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", launcher],
                timeout=timeout,
                check=False,
                capture_output=True,
            )
        except subprocess.TimeoutExpired:
            return None, TIMED_OUT, f"no answer within {timeout}s; a later approval runs nothing"
        except (OSError, subprocess.SubprocessError) as e:
            return None, NOT_CONFIRMED, f"no prompt was shown: the launcher could not run ({type(e).__name__}: {e})"
        try:
            with open(results, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            if proc.returncode == _ERROR_CANCELLED:
                return None, DISAPPROVED, ""
            raw = getattr(proc, "stdout", None) or b""  # this module never raises
            said = (raw if isinstance(raw, str) else raw.decode(errors="replace")).strip().splitlines()
            cause = f": {said[0][:200]}" if said else ""
            return None, NOT_CONFIRMED, f"no results; launcher exit {proc.returncode}{cause}"
        if not isinstance(data, dict):
            return None, NOT_CONFIRMED, f"unreadable results; launcher exit {proc.returncode}"
        if data.get("expired"):
            return None, TIMED_OUT, f"approved after the {timeout}s deadline; nothing ran"
        out: dict[str, dict] = {}
        for i, (label, _argv) in enumerate(self.actions):
            rc = data.get(label)
            out[label] = {"rc": -1 if rc is None else int(rc), "output": _tail(os.path.join(tmp, f"{i}.log"))}
        return out, APPROVED, ""


def _remove_workdir(path: str) -> None:
    """Remove a batch workdir, saying so when it cannot.

    ``ignore_errors=True`` dropped every failure, and an elevated process's
    files are owned by Administrators, so whole batches (scripts and results)
    were left in %TEMP% without a word."""
    import logging
    import shutil

    def _report(_fn, p, exc):
        logging.getLogger(__name__).warning(
            "elevation workdir not fully removed. observed: %s: %s. "
            "inspect: an elevated process owns it; delete %s from an elevated shell",
            type(exc).__name__,
            p,
            path,
        )

    shutil.rmtree(path, onexc=_report)


def _workdir() -> str:
    """A fresh directory under %TEMP% that the elevated process's files stay
    readable in. tempfile.mkdtemp() is wrong here: on Python 3.13+ it applies an
    ACL of OWNER RIGHTS + SYSTEM + Administrators, and a file the ELEVATED
    process writes is owned by Administrators, so this unelevated caller could
    not read results.json and reported an approved, successful batch as
    declined. A plain mkdir inherits %TEMP%'s ACL, which names the user."""
    import uuid

    path = os.path.join(tempfile.gettempdir(), f"m3-elevate-{uuid.uuid4().hex[:12]}")
    os.mkdir(path)
    return path


def _utc_iso(dt: datetime) -> str:
    """``dt`` as m3's fixed-width UTC stamp (m3_core.runtime.utc_iso owns it)."""
    from m3_memory import _bin

    _bin.ensure_on_path()
    from m3_core.runtime import utc_iso

    return str(utc_iso(at=dt))


def _when(dt: datetime) -> str:
    """Local time with its zone, then UTC (m3_core.runtime.local_and_utc owns it)."""
    from m3_memory import _bin

    _bin.ensure_on_path()
    from m3_core.runtime import local_and_utc

    return str(local_and_utc(dt))


def _tail(path: str, lines: int = 8) -> str:
    """The last lines of an action's log (PowerShell writes UTF-16 by default)."""
    for enc in ("utf-16", "utf-8-sig"):
        try:
            with open(path, encoding=enc) as fh:
                return "\n".join(fh.read().strip().splitlines()[-lines:])
        except (OSError, UnicodeError):
            continue
    return ""
