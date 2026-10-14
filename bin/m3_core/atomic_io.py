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
from dataclasses import dataclass
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

    The history keeps EVERY previous version in full, secrets in a client config
    included; no cap, no redaction. After
    rotating a leaked key, delete the history.
    """

    def __init__(self, path: str | os.PathLike[str], *, by: str) -> None:
        self.path = Path(path).absolute()  # the history and the index name it the same way
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
        # The index first: a failure there leaves no open BEGIN claiming a
        # rewrite that never started.
        _record_in_index(self.path)
        atomic_write(self.bak, self._old)
        if mode is not None:
            self.bak.chmod(mode)
        stamp = utc_iso("microseconds")  # unique per rewrite: --at names one
        self._append(
            f"\n=== {stamp} BEGIN rewrite of {self.path} by {self.by} ===\n"
            f"This is a log: do NOT copy this file over {self.path.name}.\n"
            f"To undo this change: copy {self.bak.name} over {self.path.name},\n"
            f"or run: m3 restore {self.path} --at {stamp}\n"
            f"If no END line follows, the rewrite did not finish: undo it the same way.\n" + _quoted(self._old),
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
        self._append(f"{_CHANGE}\n{diff or '(no change)'}\n=== {utc_iso()} END rewrite (ok) ===\n", None)

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


# A section's copy of the previous content. Every line is prefixed, so the
# history can never be pasted or copied as a config (user design 2026-10-12).
# UTF-8 text with one line ending throughout (LF, or CRLF as Windows editors
# write) is kept readable, its ending named in the marker; anything else (mixed
# endings, binary) is base64. Every version restores byte for byte.
_QUOTE = "| "
_TEXT_HEADS = {
    (eol, final): f"--- previous content (text, {label} line endings, each line prefixed '| ', {tail}) ---"
    for eol, label in (("\n", "LF"), ("\r\n", "CRLF"))
    for final, tail in ((True, "ends with a newline"), (False, "no final newline"))
}
_B64 = "--- previous content (base64, each line prefixed '| ') ---"
_CONTENT_HEADS = frozenset({*_TEXT_HEADS.values(), _B64})
_CHANGE = "--- change ---"


def _quoted(content: bytes) -> str:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    eol = "\r\n" if text is not None and "\r\n" in text else "\n"
    # Readable only when splitting on the one ending leaves no stray CR.
    if text is None or "\r" in text.replace("\r\n", "") or (eol == "\n" and "\r" in text):
        return _quoted_b64(content)
    if eol == "\r\n" and "\n" in text.replace("\r\n", ""):  # a lone LF among CRLF: mixed
        return _quoted_b64(content)
    final = text.endswith(eol) or not text
    lines = text[: -len(eol)].split(eol) if text.endswith(eol) else text.split(eol)
    return _TEXT_HEADS[(eol, final)] + "\n" + ("".join(f"{_QUOTE}{line}\n" for line in lines) if text else "")


def _quoted_b64(content: bytes) -> str:
    import base64

    b64 = base64.b64encode(content).decode("ascii")
    return _B64 + "\n" + "".join(f"{_QUOTE}{b64[i : i + 76]}\n" for i in range(0, len(b64), 76))


def _unquoted(head: str, lines: list[str]) -> bytes:
    body = [line[len(_QUOTE) :] if line.startswith(_QUOTE) else line[1:] for line in lines]
    if head == _B64:
        import base64

        return base64.b64decode("".join(body))
    (eol, final) = next(k for k, v in _TEXT_HEADS.items() if v == head)
    text = eol.join(body)
    return (text + eol).encode("utf-8") if final and body else text.encode("utf-8")


@dataclass(frozen=True, slots=True)
class Section:
    """One rewrite recorded in a .backup_history."""

    began: str  # UTC time of BEGIN
    by: str
    state: str  # "ok" | "rolled back" | "open" (began, never finished)
    previous: bytes | None  # the file before the rewrite; None in a diff-only section


def sections(path: str | os.PathLike[str]) -> list[Section]:
    """Every rewrite in ``path``'s history, oldest first. THE reader of the
    history format; versions() and unfinished_rewrites() are views of it."""
    target = Path(path).absolute()
    _bak, history = backup_paths(target)
    try:
        lines = history.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    begin = f"BEGIN rewrite of {target} by "
    out: list[Section] = []
    cur: dict | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line in _CONTENT_HEADS and cur is not None and cur["previous"] is None:
            body: list[str] = []
            while i < len(lines) and lines[i].startswith("|"):
                body.append(lines[i])
                i += 1
            cur["previous"] = _unquoted(line, body)
            continue
        if not (line.startswith("=== ") and line.endswith(" ===")):
            continue
        stamp, _, rest = line[4:-4].partition(" ")
        if rest.startswith(begin):
            if cur is not None:
                out.append(Section(**cur))
            cur = {"began": stamp, "by": rest[len(begin) :], "state": "open", "previous": None}
        elif cur is not None and rest.startswith("END rewrite"):
            cur["state"] = "ok"
        elif cur is not None and rest.startswith("ROLLED BACK"):
            cur["state"] = "rolled back"
    if cur is not None:
        out.append(Section(**cur))
    return out


def versions(path: str | os.PathLike[str]) -> list[tuple[str, str, bytes]]:
    """``(began_at, by, content)`` for every rewrite in ``path``'s history that
    saved the previous content, oldest first: ``content`` is the file as it was
    BEFORE that rewrite."""
    return [(x.began, x.by, x.previous) for x in sections(path) if x.previous is not None]


def restore(path: str | os.PathLike[str], at: str, *, by: str = "m3 restore") -> Path:
    """Write back ``path`` as it was before the rewrite that began at ``at``,
    through the same protocol (so the restore can be undone too). Returns the
    path; raises ``KeyError`` naming the versions when ``at`` is not one."""
    all_versions = versions(path)
    found = {stamp: content for stamp, _by, content in all_versions}
    if at not in found:
        raise KeyError(f"no version at {at}; versions: {', '.join(found) or 'none'}")
    if sum(stamp == at for stamp, _by, _c in all_versions) > 1:
        raise KeyError(f"{at} names more than one version; restore from {backup_paths(path)[0]} by hand")
    guard = GuardedRewrite(path, by=f"{by} (to the version before {at})")
    if os.path.exists(guard.path):  # os.path: Path.exists raises on 3.12/3.13 under an untraversable parent
        guard.write(found[at])
    else:
        guard.begin(b"")
        atomic_write(guard.path, found[at])
        guard.end(found[at])
    return guard.path


def _index_path() -> Path:
    """``<config_root>/.backup_index``: every file a GuardedRewrite has touched,
    one path per line, so doctor finds each history without a second list."""
    from m3_core.paths import get_m3_config_root  # lazy: stdlib-only at import

    return Path(get_m3_config_root()) / ".backup_index"


def _record_in_index(path: Path) -> None:
    """Best effort: the backup itself is the .bak and the history beside the
    file; the index only lets doctor find them. A missing config root is never
    created here (a root that does not exist is not this writer's to make), and a
    failure is logged, never a reason to refuse the rewrite."""
    import logging

    try:
        index = _index_path()
        entry = str(path)
        if not os.path.isdir(index.parent):  # os.path: Path.is_dir raises on 3.12/3.13
            logging.getLogger(__name__).warning(
                "observed: config root %s does not exist; %s is backed up beside itself but "
                "`m3 doctor` will not check it for an interrupted rewrite",
                index.parent,
                path,
            )
            return
        if entry in _index_lines(index):
            return
        fd = os.open(index, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, f"{entry}\n".encode())
        finally:
            os.close(fd)
    except OSError as e:
        logging.getLogger(__name__).warning(
            "observed: could not record %s in the backup index (%s); it is backed up beside itself "
            "but `m3 doctor` will not check it for an interrupted rewrite",
            path,
            e,
        )


def _index_lines(index: Path) -> list[str]:
    try:
        return [line for line in index.read_text(encoding="utf-8").splitlines() if line.strip()]
    except FileNotFoundError:
        return []


def rewritten_files() -> list[Path]:
    """Every file a GuardedRewrite has backed up, in the order first seen."""
    return [Path(p) for p in _index_lines(_index_path())]


def unfinished_rewrites() -> list[tuple[Path, str, str]]:
    """``(file, by, began_at)`` for each file whose LAST recorded rewrite began
    and never finished (no END, no ROLLED BACK): the process stopped mid-rewrite,
    and the file may hold neither the old content nor the new."""
    out: list[tuple[Path, str, str]] = []
    for path in rewritten_files():
        recorded = sections(path)
        if recorded and recorded[-1].state == "open":
            out.append((path, recorded[-1].by, recorded[-1].began))
    return out


def _replace_with_retry(src: str, dst: Path) -> None:
    for attempt in range(_REPLACE_RETRIES):
        try:
            Path(src).replace(dst)
            return
        except PermissionError:
            if attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(_REPLACE_DELAY_S * (attempt + 1))
