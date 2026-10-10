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
identical on every OS: text mode would emit CRLF on Windows. The temp file is
created 0600 by ``mkstemp`` regardless of umask; an existing target's mode is
copied onto it, so a write never relaxes or tightens a file's permissions,
while a new file is user-only (CONFIG_DESIGN §10: config holds hostnames, paths
and URLs).

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
        "wb", dir=target.parent, prefix=f".{target.name}.", suffix=".tmp",
        delete=True, delete_on_close=False,
    ) as tmp:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        if mode is not None:
            Path(tmp.name).chmod(mode)
        _replace_with_retry(tmp.name, target)


def _replace_with_retry(src: str, dst: Path) -> None:
    for attempt in range(_REPLACE_RETRIES):
        try:
            Path(src).replace(dst)
            return
        except PermissionError:
            if attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(_REPLACE_DELAY_S * (attempt + 1))
