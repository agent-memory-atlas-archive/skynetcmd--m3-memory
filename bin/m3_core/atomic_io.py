"""atomic_write: THE owner of "write a temp file, then os.replace it over the target".

Do not hand-roll the pattern. The copies this replaced shared three defects:

* a FIXED temp name (``<path>.tmp``), so two concurrent writers clobbered each
  other's temp file and one of them published the other's half-write;
* no retry of the Windows sharing violation: ``os.replace`` raises
  ``PermissionError`` while another process holds the target open without
  ``FILE_SHARE_DELETE`` (an editor, an antivirus scan, a reader mid-parse);
* no ``fsync``, so a crash after the replace could leave an empty file.

Built on 3.12's ``NamedTemporaryFile(delete=True, delete_on_close=False)``: a
unique temp per write, closed before the replace (Windows cannot replace an
open file), and removed automatically only when the replace never happened
(measured on Windows, 3.12 and 3.14). Data is written as BYTES, so the file is
identical on every OS: text mode would emit CRLF on Windows. Permissions
differ by OS. POSIX: the temp file is created 0600 by ``mkstemp`` regardless of
umask, and an existing target's mode is copied onto it, so a write never relaxes
or tightens a file's mode and a new file is user-only (CONFIG_DESIGN §10).
Windows: mode bits are only the read-only flag; the replaced file takes its
directory's inherited ACL. A caller that needs a private file there checks the
directory first (``m3_core.win_acl.probe_new_file``, as the config writer does).

Locking a read-modify-write (two ``config set`` calls racing) is the CALLER's
job; this guarantees only that each write is all-or-nothing.
"""

from __future__ import annotations

import os
import stat
import tempfile
import time
from pathlib import Path

# A sharing violation clears when the other handle closes; readers here are
# short-lived, so ~2 s of backoff covers them without hanging a real lock.
_REPLACE_RETRIES = 10
_REPLACE_DELAY_S = 0.05


def atomic_write(path: str | os.PathLike[str], data: str | bytes, *, encoding: str = "utf-8") -> None:
    """Write ``data`` to ``path`` atomically: readers see the old file or the new one, never a mix."""
    target = Path(path)
    payload = data.encode(encoding) if isinstance(data, str) else data
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        mode = None
    with tempfile.NamedTemporaryFile(
        "wb",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=True,
        delete_on_close=False,
    ) as tmp:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        if mode is not None:
            Path(tmp.name).chmod(mode)
        _replace_with_retry(tmp.name, target)


def backup_paths(path: str | os.PathLike[str]) -> tuple[Path, Path]:
    """``(<name>.bak, <name>.backup_history)`` beside ``path``."""
    p = Path(path)
    return p.with_name(f"{p.name}.bak"), p.with_name(f"{p.name}.backup_history")


class GuardedRewrite:
    """One rewrite of a file m3 does not own outright (a client config, a
    crontab), made restorable. THE owner of m3's backups.

    - ``<name>.bak`` is the ACTIVE backup: the content just before this rewrite,
      byte-exact, so restoring is copying it back (a JSON backup stays valid JSON).
    - ``<name>.backup_history`` is append-only, one UTC-stamped section per
      rewrite. BEGIN is written BEFORE the file changes and says how to restore;
      END follows the write, with the diff. A BEGIN with no END means the process
      stopped mid-rewrite: restore from ``.bak`` as that section says.

    Both sit beside the original, so neither is more exposed than the rewritten
    file itself (a client config can hold tokens): on POSIX they keep its mode
    (new: 0600); on Windows they take the directory's inherited ACL, as the
    rewritten file does (see ``atomic_write``). ``begin`` raises ``OSError``: a
    caller that cannot back up must not rewrite.
    """

    def __init__(self, path: str | os.PathLike[str], *, by: str) -> None:
        self.path = Path(path)
        self.by = by
        self.bak, self.history = backup_paths(self.path)
        self._old: bytes | None = None

    def begin(self, old: bytes | None = None) -> None:
        """Save the active backup and open the history section. ``old`` is the
        current content when the target is not a file (a crontab)."""
        from m3_core.runtime import utc_iso  # lazy: this module stays stdlib-only at import

        try:
            mode: int | None = stat.S_IMODE(self.path.stat().st_mode)
        except FileNotFoundError:
            mode = None
        self._old = self.path.read_bytes() if old is None else old
        atomic_write(self.bak, self._old)
        if mode is not None:
            self.bak.chmod(mode)
        self._append(
            f"\n=== {utc_iso()} BEGIN rewrite of {self.path} by {self.by} ===\n"
            f"The content before this rewrite is in {self.bak}. If no END line follows,\n"
            f"the rewrite did not finish: restore by copying {self.bak.name} over {self.path.name}.\n",
            mode,
        )

    def end(self, new: bytes) -> None:
        """Close the section with the change, after the new content is written."""
        import difflib

        from m3_core.runtime import utc_iso

        try:
            before = (self._old or b"").decode("utf-8").splitlines(keepends=True)
            after = new.decode("utf-8").splitlines(keepends=True)
            diff = "".join(difflib.unified_diff(before, after, "before", "after"))
        except UnicodeDecodeError:
            diff = f"(binary content: {len(self._old or b'')} -> {len(new)} bytes)\n"
        self._append(f"{diff or '(no change)'}\n=== {utc_iso()} END rewrite (ok) ===\n", None)

    def abort(self, reason: str) -> None:
        """Close the section for a rewrite the caller undid (it restored ``.bak``),
        so an open BEGIN keeps meaning "interrupted"."""
        from m3_core.runtime import utc_iso

        self._append(
            f"=== {utc_iso()} ROLLED BACK ({reason}); {self.path.name} was restored from {self.bak.name} ===\n", None
        )

    def write(self, data: str | bytes, *, encoding: str = "utf-8") -> None:
        """begin, write ``data`` atomically, end: the whole protocol for a file."""
        payload = data.encode(encoding) if isinstance(data, str) else data
        self.begin()
        atomic_write(self.path, payload)
        self.end(payload)

    def _append(self, text: str, mode: int | None) -> None:
        fd = os.open(self.history, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            data = memoryview(text.encode("utf-8"))
            while data:
                data = data[os.write(fd, data) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        if mode is not None:
            self.history.chmod(mode)


def _replace_with_retry(src: str, dst: Path) -> None:
    for attempt in range(_REPLACE_RETRIES):
        try:
            Path(src).replace(dst)
            return
        except PermissionError:
            if attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(_REPLACE_DELAY_S * (attempt + 1))
