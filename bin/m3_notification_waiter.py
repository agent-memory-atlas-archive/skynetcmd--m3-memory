#!/usr/bin/env python3
"""Single-shot m3 notification waiter — blocks until YOUR inbox has something new.

Exits 0 the moment a new notification appears for --agent-id. Exits 2 on timeout.
Run it as a background/async task: your runtime's process-completion signal is
what pushes the wake-up turn to you, so this costs ZERO conversation turns while
it waits.

WHY IT WATCHES A CHANGE TOKEN, NOT THE DATABASE
SQLite has no cross-process blocking change notification. Update hooks are
in-process only and fire solely for the connection's OWN writes, so a waiter
CANNOT be woken by another process inserting a row (verified, sqlite 3.50.4).
The seam's `Dialect.store_change_token()` is the substitute: on SQLite it is the
(mtime, size) of each store and its -wal, which a notify write changes
(measured: 263712 -> 280192 bytes). A backend with no such signal returns None
and the waiter polls by query instead.

The cheap 1s polling happens HERE, in a subprocess that costs no context. That
is the whole point: on-change delivery to the agent, without a turn per tick.
"""

from __future__ import annotations

import sys as _sys

# --config FILE (the file this job was registered with) is pinned before any
# import below binds a root at import time. Guarded like this module's other m3
# imports: a stripped payload still runs, on the default roots, and says so.
try:
    from m3_core.paths import apply_early_argv as _apply_early_argv

    _apply_early_argv()
except ImportError as _e:
    if any(a in ("--config", "--set") or a.startswith(("--config=", "--set=")) for a in _sys.argv[1:]):
        print(
            f"observed: --config/--set given but m3_core is not importable ({_e}); running on the "
            "default config root with no --set. inspect: the payload's bin/ (reinstall with `m3 upgrade`)",
            file=_sys.stderr,
        )

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from typing import Any


def refresh_agent_ids(current: list[str]) -> list[str]:
    """The live agent list, or ``current`` unchanged if it cannot be read.

    ⚠ WHY A LONG-LIVED WAITER MUST RE-READ THIS. Agent ids are resolved at
    INSTALL time. On Windows the PT10M re-fire restarts the task, so a newly
    registered agent is picked up within ten minutes. launchd and systemd hold
    ONE process for as long as it lives, so a baked-in list would go stale and
    STAY stale -- a new agent would never be watched on macOS or Linux.

    That is not hypothetical. Measured 2026-09-12: the waiter ran with
    `antigravity-agent` (last seen in MAY) while the live agent `agy` went
    unwatched -- 36 notifications to it, ZERO with receipt recorded, for hours.
    Watching a dead inbox costs one poll; MISSING a live one is the failure this
    whole feature exists to prevent.

    Returns ``current`` on any error: a waiter watching a slightly stale list is
    degraded, a waiter that dies watches nothing.
    """
    try:
        import re as _re

        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from memory.orchestration import agent_list_impl

        # "@" is in the class: an id may be "type@instance", and excluding it
        # would silently truncate every instance-addressed inbox to its bare
        # type -- re-creating the shared inbox the scheme exists to split.
        ids = _re.findall(r"\[([A-Za-z0-9_.~@+-]+)\]", agent_list_impl("", "") or "")
        return ids or current
    except Exception as exc:  # noqa: BLE001 -- degraded beats dead
        _warn(
            f"could not refresh the agent list, keeping the current one. "
            f"observed: {type(exc).__name__}: {exc}. "
            f"possible: the registry is unreadable from this interpreter. "
            f"inspect: memory.orchestration.agent_list_impl"
        )
        return current


def notification_stores(engine_root: str) -> list[str]:
    """EVERY store a notification can land in.

    ⚠ WHY THIS IS NOT ONE STORE. Delivery records live in their own store
    (`agent_dispatch.db`) by default, because notifications are ephemeral and
    memories are not. A waiter watching only `agent_memory.db` never sees a
    dispatch write land -- detection goes blind and the failure is SILENT: no
    error, no log, the receipt SLA simply stops being met. An agent that trusts
    an empty inbox drops work addressed to it.

    Store enumeration itself is NOT done here. `m3_core.paths
    .notification_store_paths()` owns "which files hold notifications", exactly
    as `chat_store_paths()` owns it for chat turns -- that question was
    hand-rolled three times for chat and got the split topology wrong three
    times.

    Falls back to the main store if `m3_core` cannot be imported: a waiter that
    watches one store is degraded, but a waiter that crashes on startup watches
    none.
    """
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
        from m3_core.paths import notification_store_paths

        stores = notification_store_paths()
    except Exception as exc:  # noqa: BLE001 -- degraded beats dead
        _warn(
            f"could not enumerate notification stores, watching the main store "
            f"only. observed: {type(exc).__name__}: {exc}. "
            f"possible: m3_core is not importable from this interpreter. "
            f"inspect: m3_core.paths.notification_store_paths"
        )
        stores = [os.path.join(engine_root, "agent_memory.db")]
    return [str(s) for s in stores]


def change_token(stores: list[str]) -> tuple:
    """One comparable token across every watched store, from the seam.

    A tuple of per-store tokens rather than a merged value: a change in ANY
    store must compare unequal, and a checkpoint shrinking one file must not
    be able to cancel out growth in another. Deferred import: see
    `_has_change_signal`.
    """
    from memory.backends import dialect

    d = dialect()
    return tuple(d.store_change_token(s) for s in stores)


def _no_window() -> dict:
    """Windows: hide the console window without detaching the child's stdio.

    STARTUPINFO/SW_HIDE, deliberately NOT creationflags=CREATE_NO_WINDOW.
    Measured 2026-09-12 in a real console: CREATE_NO_WINDOW suppresses the
    child's INHERITED stdout as well as the window, so a failing subprocess
    reports nothing at all. SW_HIDE hides the window and keeps the pipes.
    Single owner so the three call sites below cannot drift apart.

    Gated on the capability (STARTUPINFO exists only where there is a console
    window to hide), not on the platform name: this supervisor stays stdlib-only
    at import time, so it does not reach for the OS seam.
    """
    if not hasattr(subprocess, "STARTUPINFO"):
        return {}
    si = subprocess.STARTUPINFO()
    # Typeshed defines these two names only for win32; the alias keeps mypy quiet
    # on POSIX (same convention as _platform.hidden_window_kwargs).
    win: Any = subprocess
    si.dwFlags |= win.STARTF_USESHOWWINDOW
    si.wShowWindow = win.SW_HIDE
    return {"startupinfo": si}


def _log_path() -> pathlib.Path | None:
    """<logs root>/notification_waiter.log (``m3_memory._roots.logs_root``).

    It used to derive the directory from M3_ENGINE_ROOT's parent, which ignored
    M3_LOGS_ROOT and put this log apart from every other m3 log. The import is
    deferred and stdlib-only, so the supervisor still survives `pipx upgrade`.
    Returns None rather than raising: logging is best-effort and must never take
    down the thing it is observing.
    """
    try:
        from m3_core.seams import roots

        d = pathlib.Path(roots.logs_root())
        d.mkdir(parents=True, exist_ok=True)
        return d / "notification_waiter.log"
    except Exception:
        return None


def _warn(msg: str) -> None:
    """One place that reports a subprocess failure. Writes to stderr AND a file.

    This function is the fix for the outage of 2026-09-12. `unread_ids` used to
    swallow every failure into `return set()`, which is INDISTINGUISHABLE from
    "the inbox is empty". The waiter then ran for an hour detecting nothing,
    through three separate probes from another agent, while Task Scheduler
    reported Running and every health check we had reported OK.

    The root cause when it finally surfaced was trivial -- the task launched an
    interpreter that could not `import m3_memory` unless the working directory
    happened to be the source checkout, and the task has no WorkingDirectory.
    One line of stderr would have found it in seconds.

    THE FILE IS NOT REDUNDANT WITH STDERR. The scheduled task runs under
    pythonw.exe, which has no console: stderr is written to a handle nobody
    reads. A "fail loud" path whose only output goes into the void is still
    fail-silent for the exact process this feature exists to supervise -- which
    is precisely how the outage above stayed invisible. The file is the half
    that a human or a later session can actually read.

    DESIGN_PHILOSOPHIES section 3: fail loud. A detector that cannot tell you it
    is broken is worse than no detector, because it also consumes the attention
    you would have spent noticing.
    """
    line = f"[waiter] {msg}"
    print(line, file=sys.stderr, flush=True)
    p = _log_path()
    if p is None:
        return
    try:
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {line}\n")
    except Exception:
        # Best-effort. A full disk or a locked file must not kill the waiter:
        # stderr already carried the message, and losing the log is strictly
        # better than losing the detector.
        pass


# The result line each admin tool prints on success (stdout is the JSON-encoded
# string the impl returns). Anything else -- including rc 0 with no such line --
# is a failure: the tool did not report doing what was asked.
_ADMIN_RESULT = {
    "notifications_mark_received": re.compile(r"^Marked (\d+) notifications received for "),
    "notifications_ack_all": re.compile(r"^Acked (\d+) notifications for "),
}
# notifications_mark_received_impl appends this when a store was not stamped.
_PARTIAL_MARK = "; NOT stamped in: "


def _m3_admin(tool: str, agent_id: str) -> bool:
    """Run one `m3 admin <tool> --agent_id <id> --yes`. True iff it succeeded
    completely (reported its result line, and no store was skipped).

    Single owner for the three things every one of these calls must get right,
    each of which was wrong somewhere in this file on 2026-09-12:

    * the INTERPRETER -- `sys.executable`, never a bare ``m3`` on PATH and never
      a hardcoded absolute path. A bare ``m3`` resolves, but without
      M3_ENGINE_ROOT in the environment it targets a DIFFERENT store and starts
      migrating it from v000 while returning rc 0. A hardcoded path bakes one
      machine's home directory into a shipped file.
    * SW_HIDE, via the one helper, so a background service never flashes a
      console.
    * FAILING LOUD. `received[a] = (r.returncode == 0)` recorded a clean False
      for an hour while nothing worked.

    rc 0 is necessary but NOT sufficient, so the tool's own result line is
    required as well (``_ADMIN_RESULT``), and a partial stamp (a store skipped)
    is reported and counted as failure. The wrong-root case above is closed by
    ``sys.executable`` plus the inherited environment, not detected here: its
    output would look like success.
    """
    try:
        r = subprocess.run(  # nosec B603 - argv list, no shell
            [sys.executable, "-m", "m3_memory.cli", "admin", tool, "--agent_id", agent_id, "--yes"],
            capture_output=True,
            text=True,
            timeout=90,
            **_no_window(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _warn(f"{tool} for {agent_id!r} did not run: {exc!r}")
        return False
    if r.returncode != 0:
        _warn(
            f"{tool} for {agent_id!r} exited {r.returncode} using "
            f"{sys.executable!r}. stderr: {(r.stderr or '').strip()[:400]}"
        )
        return False
    try:
        text = json.loads(r.stdout or "")
    except ValueError:
        text = None
    pattern = _ADMIN_RESULT.get(tool)
    if not isinstance(text, str) or (pattern is not None and not pattern.match(text)):
        _warn(
            f"{tool} for {agent_id!r} exited 0 but did not report its result using "
            f"{sys.executable!r}, so it is NOT counted as done. "
            f"stdout: {(r.stdout or '').strip()[:400]!r} stderr: {(r.stderr or '').strip()[:400]!r}"
        )
        return False
    if _PARTIAL_MARK in text:
        _warn(f"{tool} for {agent_id!r} was PARTIAL: {text[:400]}. inspect: the store named above")
        return False
    return True


def unread_ids(agent_id: str) -> tuple[set[str], bool] | None:
    """``(unread ids, partial)`` from m3's structured read, or None when the read failed.

    None is NOT an empty inbox: a failed read used to return ``set()``, which
    emptied the baseline so the next good read reported every unread message as
    new (and ``--ack`` then marked them read). ``partial`` is True when a store
    was skipped -- see ``_advance``.
    """
    try:
        from memory.orchestration import notifications_unread_ids_checked

        ids, skipped = notifications_unread_ids_checked(agent_id)
    except Exception as exc:  # noqa: BLE001 -- reported; the caller keeps its baseline
        _warn(
            f"could not read {agent_id!r}'s inbox using {sys.executable!r} -- detection is BLIND for "
            f"this agent until a read succeeds; the previous baseline is kept. "
            f"observed: {type(exc).__name__}: {exc}. "
            f"inspect: memory.orchestration.notifications_unread_ids_checked"
        )
        return None
    return {str(x) for x in ids}, bool(skipped)


def _advance(baseline: set[str] | None, read: tuple[set[str], bool] | None) -> tuple[set[str], set[str] | None]:
    """``(gained, next_baseline)`` for one inbox read. The single owner of the
    baseline rule, shared by every detection path so they cannot drift.

    * failed read (None): nothing gained, baseline kept.
    * no baseline yet (the first read failed): this read becomes the baseline,
      nothing is reported -- reporting it would flag the whole inbox as new.
    * partial read (a store skipped): ids from the unread store are absent but
      not gone, so the baseline keeps them (union); otherwise they would be
      reported as new when the store comes back.
    """
    if read is None:
        return set(), baseline
    now, partial = read
    if baseline is None:
        return set(), now
    return now - baseline, (now | baseline) if partial else now


def _stamp_receipt(agent_id: str) -> bool:
    """Stamp receipt in-process; True only when it was checked to be complete.

    Rows stamped may be 0 without failure: another watcher of the same inbox (a
    bare type and its instance) can stamp first, and the stamp is idempotent.
    """
    try:
        from memory.orchestration import notifications_mark_received_checked

        _n, skipped = notifications_mark_received_checked(agent_id)
    except Exception as exc:  # noqa: BLE001 -- reported; receipt is then recorded as not stamped
        _warn(f"receipt for {agent_id!r} NOT stamped. observed: {type(exc).__name__}: {exc}")
        return False
    if skipped:
        _warn(f"receipt for {agent_id!r} PARTIAL: not stamped in {', '.join(skipped)}")
        return False
    return True


def pg_child_loop(args) -> int:
    import json
    import sys
    import threading
    import time

    shutdown_flag = threading.Event()

    def stdin_watcher():
        try:
            for line in sys.stdin:
                if line.strip() == "stop":
                    shutdown_flag.set()
                    break
        except Exception:
            pass
        shutdown_flag.set()  # EOF seen

    t = threading.Thread(target=stdin_watcher, daemon=True)
    t.start()

    try:
        from memory.backends import dialect
        from memory.orchestration import (
            notifications_ack_all_impl,
            notifications_unread_ids_checked,
        )

        from memory import db
    except ImportError as e:
        _warn(f"ImportError in child: {e}")
        return 1

    baselines: dict[str, set[str] | None] = {}
    try:
        for a in args.agent_ids:
            # Startup sweep: stamp receipt on anything that arrived while
            # this child was down, BEFORE the baseline below decides what
            # counts as "already seen". _stamp_receipt reports a failure --
            # a silent sweep failure leaves stranded notifications looking
            # delivered.
            _stamp_receipt(a)
            ids, _skipped = notifications_unread_ids_checked(a)
            baselines[a] = {str(x) for x in ids}
    except Exception as e:
        _warn(f"could not read the baseline inboxes {args.agent_ids!r}: {type(e).__name__}: {e}")
        return 1

    deadline = time.time() + args.timeout

    while time.time() < deadline:
        if shutdown_flag.is_set():
            print(json.dumps({"t": "bye"}), flush=True)
            return 0

        print(json.dumps({"t": "hb", "n": 0}), flush=True)

        try:
            hits = {}
            # One statement-timeout call per poll, through the seam. `dialect`
            # lives in memory.backends, NOT memory.db -- `db.dialect()` raises
            # AttributeError, and wrapped in a bare `except: pass` it left the
            # timeout silently unapplied on every backend.
            with db._db() as conn:
                try:
                    dialect().set_statement_timeout(conn, 2000)
                except Exception as exc:
                    # Report it. A swallowed failure here disables the only
                    # bound on a hung query -- the connection-exhaustion
                    # protection this whole child was designed around.
                    _warn(f"set_statement_timeout failed: {exc!r}")

                for a in args.agent_ids:
                    ids, skipped = notifications_unread_ids_checked(a)
                    gained, baselines[a] = _advance(baselines.get(a), ({str(x) for x in ids}, bool(skipped)))
                    if gained:
                        hits[a] = sorted(gained)

            # Receipt goes through the SINGLE OWNER, not hand-written SQL.
            #
            # This block previously ran `UPDATE notifications SET received_at =
            # now() WHERE agent_id = ...` inline, which was wrong three ways:
            # it duplicated the predicate that notifications_mark_received_impl
            # already owns (10a: "duplicated predicate logic is the defect,
            # independent of correctness"), and `now()` is PostgreSQL-only --
            # SQLite raises "no such function: now", so the SQLite path would
            # have failed at runtime. "received" reports what was CHECKED to
            # be stamped, not what was attempted.
            received = {a: _stamp_receipt(a) for a in hits}

            if hits:
                acked = {}
                if args.ack:
                    for a in hits:
                        notifications_ack_all_impl(a)
                        acked[a] = hits[a]
                print(
                    json.dumps({"t": "hit", "new_notifications": hits, "received": received, "acked": acked}),
                    flush=True,
                )
                return 0
        except Exception as e:
            _warn(f"PG poll error: {e}")
            return 1

        time.sleep(args.interval)

    print(json.dumps({"t": "timeout"}), flush=True)
    return 2


def _supervise_postgres(args) -> int:
    import json
    import queue
    import subprocess
    import sys
    import threading
    import time

    backoff = 1.0
    deadline = time.time() + args.timeout
    restart_count = 0

    while time.time() < deadline:
        cmd = [sys.executable, "-u", __file__, "--child"]
        for a in args.agent_ids:
            cmd.extend(["--agent-id", a])
        if args.ack:
            cmd.append("--ack")
        cmd.extend(["--interval", str(args.interval), "--timeout", str(args.timeout)])

        kwargs = _no_window()
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.PIPE, text=True, **kwargs)
        child_start_time = time.time()

        q: queue.Queue[str | None] = queue.Queue()  # None == EOF sentinel

        # The thread gets ITS child's stream and queue as arguments. Read from
        # the enclosing scope at run time, a reader still alive after a restart
        # put its EOF sentinel into the NEW child's queue, and the supervisor
        # took the healthy new child for dead (a restart storm).
        def reader(stream, out_q):
            try:
                for line in stream:
                    out_q.put(line)
            except Exception:
                pass
            out_q.put(None)

        t = threading.Thread(target=reader, args=(p.stdout, q), daemon=True)
        t.start()

        child_failed = False
        last_hb = time.time()

        while True:
            try:
                line = q.get(timeout=0.1)
                if line is None:
                    child_failed = True
                    break

                line = line.strip()
                if not line:
                    continue

                try:
                    msg = json.loads(line)
                except Exception:
                    continue

                match msg.get("t"):
                    case "hb":
                        last_hb = time.time()
                    case "hit":
                        # Forward the detection: the child's stdout is a pipe,
                        # so without this the runtime woken by our exit never
                        # learns which notifications arrived.
                        print(json.dumps({k: v for k, v in msg.items() if k != "t"}), flush=True)
                        p.wait(timeout=2.0)
                        return 0
                    case "timeout":
                        p.wait(timeout=2.0)
                        return 2
                    case "bye":
                        break
            except queue.Empty:
                if time.time() - last_hb > (args.interval * 3):
                    # Flush before breaking. Backgrounded, this process can be
                    # killed while the child still holds the WAL watch, and a
                    # buffered warning dies with it -- so the fail-loud path
                    # reports nothing for its most common failure.
                    _warn(f"PG child wedged (no hb for {args.interval * 3}s). Restarting.")
                    sys.stderr.flush()
                    child_failed = True
                    break

        if child_failed:
            try:
                if p.stdin is not None:
                    p.stdin.write("stop\n")
                    p.stdin.flush()
            except Exception:
                pass

            try:
                p.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
            if p.stdin:
                p.stdin.close()
            if p.stdout:
                p.stdout.close()
            if p.stderr:
                p.stderr.close()

            if time.time() - child_start_time > 60.0:
                backoff = 1.0
                restart_count = 0

            restart_count += 1
            _warn(f"Child restarted {restart_count} times. Backing off for {backoff}s")
            time.sleep(backoff)
            backoff = min(60.0, backoff * 2.0)

    return 2


def _has_change_signal(probe_store: str) -> bool:
    """Does the SEAM give this backend a connection-free change signal?

    True -> watch `change_token()` and confirm by query only when it moves.
    False -> no cheap signal (PostgreSQL today): poll by query in a supervised
    child. A CAPABILITY, never the backend name: a third backend without a
    signal gets the polling path by default instead of watching files it never
    writes.

    #163: ask the seam, do not infer. Reading `M3_DB_BACKEND` directly looked
    equivalent and was not -- the selector also honours the `DB_BACKEND`
    alias. Measured 2026-09-12 with `DB_BACKEND=postgres`::

        SEAM resolves to  : postgres
        WAITER would take : SQLITE WAL path

    -- a blind waiter that polls forever, with no error and no log line.

    The import is DEFERRED: the supervisor must stay stdlib-only at import time
    to survive `pipx upgrade` (#163).

    Fails CLOSED to the file-signal lane when the seam cannot be imported and
    the configured backend is SQLite (the default): a stripped supervisor
    payload is almost certainly SQLite-only, and guessing the polling lane would
    spawn a child that cannot connect. The backend name comes from the selector's
    single owner (`m3_core.paths.resolve_backend_name`); when even `m3_core` is
    missing, the registry's default ``sqlite`` (nothing can locate the config file
    then, and a stripped payload is SQLite-only). Gated `== "sqlite"`, never
    `!= "postgres"`: any other backend, or an unrecognised name, takes the
    polling lane and fails loud there rather than watching files nobody writes.
    """
    try:
        from memory.backends import dialect  # deferred: see docstring

        return dialect().store_change_token(probe_store) is not None
    except Exception as exc:
        try:
            from m3_core.paths import resolve_backend_name  # the selector's single owner

            env = resolve_backend_name()
        except ImportError:  # stripped payload: no m3_core, so no registry to read [db] backend
            # Without m3_core neither the registry nor the seams that locate
            # m3_config.toml exist; take the registry's default, as documented.
            env = "sqlite"
        except ValueError as sel_exc:  # an unrecognised name; the selector says which
            env = f"unrecognised ({sel_exc})"
        _warn(
            f"could not ask the backend seam for a change signal "
            f"(observed: {exc!r}); falling back to backend={env!r}. "
            f"inspect: memory.backends.dialect().store_change_token"
        )
        return env == "sqlite"


def _default_engine_root() -> str:
    """The engine root from its one owner (m3_memory._roots, which reads [paths]
    from m3_config.toml; the inline default here did not). Degraded beats dead: a
    stripped payload without m3_core gets the default location, not a crash."""
    try:
        from m3_core.seams import roots

        return roots.engine_root()
    except ImportError:
        return str(pathlib.Path.home() / ".m3" / "engine")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--agent-id",
        required=True,
        help="Inbox to watch. Repeatable: pass once per agent to serve several "
        "inboxes from ONE process. The WAL trigger is shared -- a single file "
        "change covers every inbox -- so N agents cost N cheap confirm-polls "
        "after a change, not N watchers.",
        action="append",
        dest="agent_ids",
    )
    ap.add_argument("--engine-root", default=_default_engine_root())
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument(
        "--timeout", type=float, default=3600.0, help="Give up after N seconds so a forgotten waiter cannot leak."
    )
    ap.add_argument(
        "--supervise",
        action="store_true",
        help="Never exit: after each detection, re-arm and keep waiting. For an "
        "ONSTART scheduled task, which needs a long-lived process. Without it "
        "the waiter is single-shot, which is what a runtime wants when the "
        "process EXIT is the wake signal.",
    )
    ap.add_argument(
        "--ack",
        action="store_true",
        help="Ack on detection. OFF BY DEFAULT, deliberately: the notifications table "
        "has only `read_at` -- no separate 'received' column -- so acking here "
        "destroys the only record that a message was unread. Measured on this "
        "machine: 29 of 30 recent notifications carried NO task_id, so 'the task "
        "state machine tracks it' is false for ~97 percent of real traffic. Enable this "
        "only where every watched kind is backed by a task whose own state "
        "survives the ack.",
    )
    ap.add_argument("--child", action="store_true")
    ap.add_argument(
        "--confirm",
        action="store_true",
        help="Internal: run by m3-wait on each wake. Stamp receipt on new mail and print it "
        "(exit 0), or exit 75 when nothing is new.",
    )
    try:
        from m3_core.paths import add_config_arg

        add_config_arg(ap)  # pinned already (pin_from_argv); declared for --help
    except ImportError:  # stripped payload: accept the flag, the guard above reported it
        ap.add_argument("--config", dest="config_file", default=None)
    args = ap.parse_args()

    # Supervise mode loops the single-shot body instead of duplicating it, so
    # there is exactly one implementation of detect-confirm-ack and the two
    # modes cannot drift.

    if args.child:
        return pg_child_loop(args)
    if args.confirm:
        return _confirm(args)

    # With m3-dispatchd running and m3-wait installed, the wait is held there and
    # this interpreter imports none of the store. Otherwise, the paths below.
    bell, wait_bin = _doorbell_endpoint(args.engine_root), _m3_wait_binary()
    if bell and wait_bin:
        while _ensure_daemon(bell):
            if args.supervise:
                args.agent_ids = refresh_agent_ids(args.agent_ids)
            rc = _wait_via_m3_wait(args, bell, wait_bin)
            if rc is None:
                break  # fall through to polling
            if not args.supervise:
                return rc

    if not _has_change_signal(notification_stores(args.engine_root)[0]):
        if args.supervise:
            while True:
                # Same re-read as the file-signal lane below: a long-lived
                # supervisor outlives the install that chose its agent list.
                args.agent_ids = refresh_agent_ids(args.agent_ids)
                rc = _supervise_postgres(args)
                if rc not in (0, 2):
                    return rc
        return _supervise_postgres(args)

    if args.supervise:
        while True:
            # Re-read the registry between iterations. A long-lived supervisor
            # outlives the install that chose its agent list; see
            # refresh_agent_ids for the incident this prevents.
            args.agent_ids = refresh_agent_ids(args.agent_ids)
            rc = _wait_once(args)
            if rc not in (0, 2):  # 0 = delivered, 2 = idle timeout
                return rc  # a real failure must surface, not spin
    return _wait_once(args)


# Longest one doorbell wait blocks before the waiter confirms by query anyway: a
# ring lost in transit then costs at most this much delay, never a lost message.
_BELL_WAIT_S = 60.0


def _doorbell_endpoint(engine_root: str) -> str | None:
    """The m3-dispatchd endpoint for this engine root, or None (no doorbell:
    a stripped payload, or the client not importable)."""
    try:
        from m3_core import doorbell

        return doorbell.endpoint_for(engine_root)
    except ImportError:
        return None


def _doorbell_wait(endpoint: str | None, agent_ids: list[str], timeout_s: float) -> dict | None:
    """Block on the doorbell; None when there is no daemon to block on."""
    if not endpoint:
        return None
    from m3_core import doorbell

    return doorbell.wait(endpoint, agent_ids, timeout_s)


# m3-wait's exit codes, and the one its confirm command uses to wait again.
_M3_WAIT_NO_DAEMON = 3
_KEEP_WAITING = 75


def _baseline_path(engine_root: str, agent_ids: list[str]) -> pathlib.Path:
    """Where --confirm keeps the unread ids it last reported, per reader set (the
    daemon's key too). Per reader set, not per row: one message can reach several
    readers (a bare type and its instance), and each must see it once."""
    import hashlib

    # A file name, not security (outside the FIPS boundary, as dispatch_endpoint's tag).
    key = hashlib.sha256("\n".join(sorted(set(agent_ids))).encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    return pathlib.Path(engine_root) / ".internal" / "waiter" / f"{key}.json"


def _confirm(args) -> int:
    """One wake, confirmed in the store: report unread ids that are new since the
    last report (exit 0, receipt stamped), or exit 75 so m3-wait waits again.
    Run by m3-wait per wake, as a fresh process, so the baseline lives in a file.

    With no file yet (first run, or after it was removed), the baseline is empty:
    mail that arrived while no waiter ran is reported at the first wake, which
    is a resync."""
    try:
        from memory.orchestration import (
            notifications_ack_all_impl,
            notifications_unread_ids_checked,
        )
    except ImportError as e:
        _warn(f"--confirm cannot import m3's store: {e}")
        return 1
    path = _baseline_path(args.engine_root, args.agent_ids)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        saved = {}
    except (OSError, ValueError) as exc:
        _warn(f"unreadable waiter baseline {path} ({exc!r}); reporting all unread mail once")
        saved = {}
    hits, now_all = {}, {}
    for a in args.agent_ids:
        # Not unread_ids(): it answers an empty set on a failed read, which here
        # would be saved as the baseline and re-report the whole inbox later.
        try:
            ids, skipped = notifications_unread_ids_checked(a)
        except Exception as exc:  # noqa: BLE001 -- reported; the baseline is left as it was
            _warn(f"--confirm could not read {a!r}'s inbox: {exc!r}")
            return 1
        gained, nxt = _advance(set(saved.get(a, [])), ({str(x) for x in ids}, bool(skipped)))
        now_all[a] = sorted(nxt or ())
        if gained:
            hits[a] = sorted(gained)
    from m3_core.atomic_io import atomic_write

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(now_all))
    if not hits:
        return _KEEP_WAITING
    received = {a: _stamp_receipt(a) for a in hits}
    acked = {}
    if args.ack:
        for a in hits:
            notifications_ack_all_impl(a)
            acked[a] = hits[a]
    print(json.dumps({"new_notifications": hits, "received": received, "acked": acked}), flush=True)
    return 0


def _pinning_argv() -> list[str]:
    """This process's --config/--set arguments, so a child reads the same settings."""
    out, argv = [], sys.argv[1:]
    for i, a in enumerate(argv):
        if a in ("--config", "--set") and i + 1 < len(argv):
            out += [a, argv[i + 1]]
        elif a.startswith(("--config=", "--set=")):
            out.append(a)
    return out


def _core_binary(base: str) -> str | None:
    try:
        from m3_core.seams import core_bins
    except ImportError:
        return None
    found = core_bins.core_binary(base)
    return str(found) if found else None


def _m3_wait_binary() -> str | None:
    return _core_binary("m3-wait")


def _ensure_daemon(endpoint: str) -> bool:
    """m3-dispatchd answers on ``endpoint``, started here if the core has it.

    The waiter is already a per-user job on every OS, so it keeps the daemon up
    rather than a second registered service: no new unit, task or uninstall
    path. A daemon that died is started again at the next round."""
    from m3_core import doorbell

    if doorbell.alive(endpoint):
        return True
    binary = _core_binary("m3-dispatchd")
    if not binary:
        return False  # this core has no daemon: polling is the normal path
    from m3_core.paths import get_m3_logs_root

    log = os.path.join(get_m3_logs_root(), "m3-dispatchd.log")
    if doorbell.start_daemon(endpoint, binary, log):
        return True
    _warn(
        f"m3-dispatchd did not answer after starting. observed: binary {binary}, endpoint {endpoint}. "
        f"polling instead. inspect: {log}"
    )
    return False


def _wait_via_m3_wait(args, endpoint: str, binary: str) -> int | None:
    """Block in m3-wait (about 1 MB committed) instead of this interpreter, and
    confirm each wake with ``--confirm``. None when it cannot serve (no daemon,
    or m3-wait failed: reported), so the caller polls as before."""
    # --exit-when-stdin-closes: m3-wait ends when this process does, however it
    # ends, so a killed waiter leaves no child running confirm commands.
    cmd = [binary, "--endpoint", endpoint, "--timeout", str(args.timeout), "--exit-when-stdin-closes"]
    for a in args.agent_ids:
        cmd += ["--reader", a]
    cmd += ["--", sys.executable, os.path.abspath(__file__), "--confirm", "--engine-root", args.engine_root]
    for a in args.agent_ids:
        cmd += ["--agent-id", a]
    if args.ack:
        cmd.append("--ack")
    cmd += _pinning_argv()
    try:
        # Popen + wait, not run(): run() closes stdin at once, which is the signal to exit.
        child = subprocess.Popen(cmd, stdin=subprocess.PIPE, **_no_window())  # nosec B603 - argv list, no shell
        rc = child.wait()
    except OSError as exc:
        _warn(f"m3-wait did not start: {exc!r}. observed: binary {binary}, endpoint {endpoint}. polling instead")
        return None
    if rc == _M3_WAIT_NO_DAEMON:
        return None  # the daemon is an accelerator; polling is the normal fallback
    if rc not in (0, 2):
        _warn(
            f"m3-wait exited {rc} (1: an error it printed above, or --confirm failed; other: the confirm "
            f"command's own code). observed: binary {binary}, endpoint {endpoint}, readers {args.agent_ids}. "
            "polling instead. inspect: the stderr above"
        )
        return None
    return rc


def _wait_once(args) -> int:
    stores = notification_stores(args.engine_root)
    # STARTUP SWEEP -- stamp receipt on anything already waiting, BEFORE the
    # baseline is taken.
    #
    # The baseline below is "what is unread right now", and the loop only ever
    # reports ids that appear AFTER it. That is correct for detecting change and
    # wrong for recording RECEIPT: a notification that arrived while this process
    # was down -- a restart, a reboot, a --timeout expiry, the hour this waiter
    # spent blind on 2026-09-12 -- gets baselined away as "pre-existing" and is
    # never stamped. The SLA then under-reports silently, which is the failure
    # this whole feature exists to remove.
    #
    # notifications_mark_received targets `received_at IS NULL`, so this is
    # idempotent (a second run stamps 0 rows and preserves the FIRST receipt
    # time) and it never touches read_at -- a swept message stays UNREAD and
    # still shows up under --unread_only. Sweeping costs one call per agent at
    # startup, once.
    for _a in args.agent_ids:
        _m3_admin("notifications_mark_received", _a)

    # A failed first read leaves that agent unbaselined (None): _advance then
    # takes its first good read as the baseline instead of reporting it all.
    baselines = {a: (None if (r := unread_ids(a)) is None else r[0]) for a in args.agent_ids}
    try:
        fp = change_token(stores)
    except Exception as exc:  # noqa: BLE001 -- reported, then a real failure rc
        _warn(
            f"cannot read the change token for {stores!r}; detection would be "
            f"blind, so stopping. observed: {type(exc).__name__}: {exc}. "
            f"inspect: memory.backends.dialect().store_change_token"
        )
        return 1
    deadline = time.time() + args.timeout
    bell = _doorbell_endpoint(args.engine_root)

    while time.time() < deadline:
        # With m3-dispatchd running, block until a writer rings instead of
        # checking once a second. Every outcome -- mail, resync, timeout --
        # falls through to the SAME confirmation below, so the exit contract is
        # unchanged; the bounded wait is the safety net for a lost ring. No
        # daemon (None): poll the change token as before.
        woke = _doorbell_wait(bell, args.agent_ids, min(_BELL_WAIT_S, max(0.0, deadline - time.time())))
        if woke is None:
            time.sleep(args.interval)
            cur_fp = change_token(stores)
            if cur_fp == fp:
                continue  # nothing written at all
            fp = cur_fp
        hits = {}
        for a in args.agent_ids:
            gained, baselines[a] = _advance(baselines.get(a), unread_ids(a))
            if gained:
                hits[a] = sorted(gained)
        if hits:
            # RECEIPT vs READING -- the distinction this whole waiter turns on.
            #
            # The SLA is "acknowledge RECEIPT within 30 seconds", and receipt
            # does not need an agent turn: a plain subprocess round-trips in
            # ~430ms. So stamping it here makes receipt deterministic and
            # independent of whether any agent is free -- a busy agent delays
            # the WORK, never the RECEIPT.
            #
            # This used to be impossible to do safely. `notifications` carried
            # ONE timestamp, `read_at`, so the only way to record receipt was to
            # ack -- which marks a message read that no agent has read, and the
            # unread flag was the only record that work was still pending. The
            # usual reassurance ("the task state machine covers it") was
            # measured and is false for real traffic: 29 of 30 recent
            # notifications carried no task_id.
            #
            # Migration 045 / pg_054 split the two, so the safe option now
            # exists: `received_at` is written here, `read_at` is left for an
            # agent that has actually read the message. --unread_only still
            # filters on `read_at`, so the inbox remains the record of
            # UNPROCESSED work. Receipt is therefore the DEFAULT.
            #
            # --ack additionally marks messages READ from here. That is still
            # opt-in and still lossy in the same way it always was: use it only
            # where every watched kind is backed by a task whose own state
            # survives the ack.
            received = {a: _m3_admin("notifications_mark_received", a) for a in hits}

            acked = {}
            if args.ack:
                acked = {a: _m3_admin("notifications_ack_all", a) for a in hits}
            print(json.dumps({"new_notifications": hits, "received": received, "acked": acked}))
            return 0  # exit == your runtime's wake signal

    print(json.dumps({"timeout": True, "agent_ids": args.agent_ids}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
