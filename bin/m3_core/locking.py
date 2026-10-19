import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path

from m3_core.paths import get_m3_config_root

logger = logging.getLogger("M3_SDK")


# How long a PermissionError creating a lock file is treated as a release still
# in progress (a delete pending on Windows) before it is reported as denied.
_DENIED_RETRY_S = 2.0


class LockTimeout(RuntimeError):
    """The lock stayed held past the wait. A class, not a message test: the
    native-lock fallback re-raised any RuntimeError whose text said "lock"."""


@contextmanager
def migration_lock():
    """Acquires an exclusive atomic file lock for safe startup migrations.

    If the lock is held by another process, it block-waits (with a timeout of 120s)
    until the lock is released.
    """
    with file_lock(os.path.join(get_m3_config_root(), ".migration.lock"), purpose="migration"):  # noqa: PTH118 — root-derived string: Path would re-separate a forward-slash root
        yield


@contextmanager
def file_lock(lock_path: str, *, purpose: str):
    """THE cross-process exclusive lock: block up to 120 s, reclaim a stale one.

    An O_CREAT|O_EXCL file stamped with its owner so a crashed holder's lock is reclaimed rather than wedging
    every waiter. ``purpose`` names it in errors (``migration``, ``secrets file``).
    """
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)  # noqa: PTH103, PTH120 — a bare lock name raises here as before; Path('x').parent.mkdir() would silently succeed

    # No native path: m3_core_rs exports no lock class (a NativeMigrationLock
    # branch here could never run, and caught the caller's own exceptions).
    fd = None
    start_time = time.time()
    acquired = False
    denied_since: float | None = None

    while time.time() - start_time < 120.0:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            # Stamp ownership so a future waiter can tell whether the holder is
            # still alive (PID liveness) or this is an orphaned lock to reclaim.
            try:
                os.write(fd, _lock_owner_stamp().encode("utf-8"))
            except OSError:
                pass  # stamping is best-effort; the lock itself is what matters
            acquired = True
            break
        except FileExistsError:
            # The lock exists. Before sleeping, check whether it's STALE — a
            # process that died holding it (crash / kill -9 / OOM) leaves the
            # file behind, which would otherwise wedge every migration for the
            # full 120s and then hard-error (the 2026-06-27 incident). Reclaim
            # it only when we can prove the owner is gone.
            if _reclaim_stale_lock(lock_path):
                continue  # reclaimed — retry os.open immediately, no sleep
            time.sleep(0.5)
        except PermissionError as e:
            # Windows refuses to create a file whose deletion is still pending:
            # the previous holder's release, a few milliseconds long. Raised at
            # once, it crashed a concurrent writer (the audit-trail concurrency
            # test, windows-latest). A denial that outlasts the window is a real
            # permission problem and is reported as one.
            now = time.time()
            denied_since = denied_since or now
            if now - denied_since > _DENIED_RETRY_S:
                raise PermissionError(
                    e.errno,
                    f"cannot create the {purpose} lock {lock_path}: permission denied for "
                    f"{_DENIED_RETRY_S:.0f} s. observed: {e.strerror}. inspect: write access to "
                    f"{Path(lock_path).parent}",
                    lock_path,
                ) from e
            time.sleep(0.05)

    if not acquired:
        raise LockTimeout(
            f"Could not acquire {purpose} lock at {lock_path} within 120 seconds. "
            f"Another process holding it may be hung. If you are sure none is running, "
            f"delete the lock file manually: {lock_path}. "
            f"observed: the lock stayed held for 120 s. inspect: the process holding {lock_path}"
        )

    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:  # noqa: BLE001 — releasing a lock fd during cleanup
                pass
            _release_lock_file(lock_path, purpose)


def _release_lock_file(lock_path: str, purpose: str) -> None:
    """Delete the lock file, retrying while a waiter has it open. On Windows a file
    another process has open (a waiter reading the owner stamp) cannot be deleted:
    the unlink raises PermissionError. Swallowed once, it left a lock whose owner
    PID is alive, so it was never reclaimed and the owner's next acquire waited the
    full 120 s on its own lock. A lock that cannot be removed is said, not hidden."""
    deadline = time.time() + 5.0
    while True:
        try:
            Path(lock_path).unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if time.time() >= deadline:
                break
            time.sleep(0.01)
        except OSError:
            break
    logger.warning(
        "observed: the %s lock file %s could not be removed after release; waiters will block until "
        "its owner exits. inspect: the process holding it open; fix: delete the file",
        purpose,
        lock_path,
    )


# Hard ceiling: no legitimate startup migration runs longer than this. A lock
# older than this from an UNKNOWN/cross-host owner (whose PID we can't probe) is
# treated as stale. Generous so we never reclaim a genuinely-slow migration.
_MIGRATION_LOCK_MAX_AGE_S = 600.0


def _lock_owner_stamp() -> str:
    """Owner metadata written into the lock file: 'pid host epoch'."""
    import socket

    return f"{os.getpid()} {socket.gethostname()} {int(time.time())}"


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID exists; single owner `m3_core.process`.

    Conservative by that owner's contract: anything that cannot disprove
    existence reads as alive, so a live lock is never stolen.
    """
    from m3_core.process import pid_is_alive

    return pid_is_alive(pid)


def _reclaim_stale_lock(lock_path: str) -> bool:
    """Remove the lock file IFF its owner is provably gone. Returns True if a
    stale lock was reclaimed (caller should retry acquisition immediately).

    Decision rules (all fail SAFE — when unsure, leave the lock alone):
      - Same host + owner PID not alive  -> stale, reclaim.
      - Cross host (can't probe PID) + file older than the max-age ceiling
        -> stale, reclaim.
      - Unparseable/empty stamp + file older than the ceiling -> stale.
      - Otherwise -> not stale (return False; caller keeps waiting).
    """
    import socket

    try:
        raw = ""
        with Path(lock_path).open(encoding="utf-8") as f:
            raw = f.read().strip()
        try:
            mtime = Path(lock_path).stat().st_mtime
        except OSError:
            mtime = 0.0
    except FileNotFoundError:
        return True  # vanished out from under us — effectively reclaimable
    except OSError:
        return False  # can't read -> don't touch it

    age = max(0.0, time.time() - mtime)
    parts = raw.split()
    pid: int = 0
    host = ""
    if len(parts) >= 2:
        try:
            pid = int(parts[0])
        except ValueError:
            pid = 0
        host = parts[1]

    this_host = socket.gethostname()
    stale = False
    if pid and host == this_host:
        # Same machine: authoritative liveness check.
        stale = not _pid_alive(pid)
    else:
        # Cross-host or unparseable: we cannot probe the PID, so fall back to a
        # generous age ceiling. Only reclaim something clearly abandoned.
        stale = age > _MIGRATION_LOCK_MAX_AGE_S

    if not stale:
        return False
    # Rename, re-read, then delete — never a bare unlink. Two waiters can judge
    # the same lock stale; with unlink the second deleted the FRESH lock the
    # first had just created, and both proceeded as holders. The rename is
    # atomic, and the renamed file is checked to be the one judged stale.
    import uuid

    aside = f"{lock_path}.stale-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        Path(lock_path).replace(aside)
    except FileNotFoundError:
        return True  # someone else reclaimed it first — retry
    except OSError:
        return False  # held open (Windows) or not ours to move: keep waiting
    try:
        moved = Path(aside).read_text(encoding="utf-8").strip()
        moved_mtime = Path(aside).stat().st_mtime
    except OSError:
        moved, moved_mtime = None, None
    if moved != raw or moved_mtime != mtime:
        # We moved a lock created after our read. Put it back only if nobody has
        # created another meanwhile (link fails if the name exists).
        try:
            os.link(aside, lock_path)
            Path(aside).unlink()
        except OSError as e:
            logger.error(
                "lock %s: moved a lock created after the stale check and could not restore it "
                "(%s: %s). observed: its owner may now run alongside another holder. "
                "kept at: %s. inspect: the processes using %s",
                lock_path,
                type(e).__name__,
                e,
                aside,
                lock_path,
            )
        return False
    try:
        Path(aside).unlink()
    except OSError:
        pass  # the lock name is free already; a leftover aside file holds nothing
    logger.warning(
        "Reclaimed stale lock %s (owner pid=%s host=%s age=%.0fs)",
        lock_path,
        pid or "?",
        host or "?",
        age,
    )
    return True
