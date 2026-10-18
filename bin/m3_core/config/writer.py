"""The writer of m3_config.toml. tomlkit edits; tomllib
(``loader.validate_text``) remains the only parser that decides what the file
means, and it validates the NEW text before the old file is replaced.

Every write is a read-modify-write under an advisory lock (two writers cannot
lose each other's update), comments and layout survive (tomlkit round-trips
them), and the replace is atomic (``atomic_write``: a reader sees the old file
or the new one, never a mix; a new file is created user-only, an existing
file's mode is kept). Secrets are refused: they never live in this file (§10).
Writers are the installer, the wizard and (2F) ``m3 config set``; daemons never
write it (§3).

A new file is the shipped default (``default_file``: every key commented at its
default), hash-verified first. Setting a key the default lists turns its
commented line into the value in place; unsetting it comments the line again.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path

import tomlkit

from m3_core.seams import is_windows

from . import default_file
from .keys import REGISTRY
from .loader import _HEADER as _TABLE_HEADER
from .loader import Location, show_key, validate_text
from .registry import ConfigValue, Key, KeyClass, Registry


class ConfigWriteError(ValueError):
    """A write that was refused; the file is unchanged."""


def _toml_native(value: ConfigValue) -> object:
    if isinstance(value, Path):
        # Forward slashes: str() on Windows re-separated `/srv/m3` into `\srv\m3`,
        # rewriting the user's spelling; Windows reads either separator.
        return value.as_posix()
    if isinstance(value, list):
        return list(value)
    return value


def set_values(location: Location, values: Mapping[Key, ConfigValue | None], *, registry: Registry = REGISTRY) -> Path:
    """Set (or, with None, remove) each key in the file at ``location``.

    Refuses, leaving the file untouched: a secret or state key, a key not in the
    registry, a value its validator rejects, and new content that would not load.
    """
    from m3_core.atomic_io import atomic_write
    from m3_core.locking import file_lock

    for key in values:
        if registry.get(key.name) is not key:
            raise ConfigWriteError(f"{key.name} is not a registry key")
        if key.klass is not KeyClass.CONFIG:
            raise ConfigWriteError(
                f"{key.name} is a {key.klass} key and is never written to m3_config.toml"
                + ("; store it with m3_core.secret_store" if key.klass is KeyClass.SECRET else "")
            )
        if values[key] is not None:
            key.check(values[key])

    path = location.file
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(str(path) + ".lock", purpose="m3_config.toml"):
        if is_windows() and not os.path.exists(path):
            _check_new_file_would_be_private(path.parent)
        old = path.read_text(encoding="utf-8") if os.path.isfile(path) else ""
        doc = tomlkit.parse(_in_place(old or _new_text(), values))
        for key, value in values.items():
            _apply(doc, key, value)
        new = tomlkit.dumps(doc)
        validate_text(path, new, registry)  # refuse BEFORE replacing
        if new != old:
            atomic_write(path, new)
    from .settings import _CACHE  # lazy: settings sits above the writer

    _CACHE.mark_stale()
    return path


def _check_new_file_would_be_private(directory: Path) -> None:
    """Windows: m3 creates m3_config.toml user-only (it holds hostnames, paths and
    URLs), but a new file takes its directory's ACL; atomic_write's 0600 applies on
    POSIX only. Probe what a new file here would inherit, and refuse before writing."""
    from m3_core.win_acl import AclError, file_foreign_principals, probe_new_file

    try:
        with probe_new_file(directory) as probe:
            foreign = file_foreign_principals(probe)
    except AclError as e:
        raise ConfigWriteError(
            f"observed: cannot verify who could read a new file in {directory} ({e}); m3_config.toml was NOT written"
        ) from e
    if foreign:
        from m3_core.win_acl import icacls_user_only

        raise ConfigWriteError(
            f"observed: a new file in {directory} could be opened by {', '.join(foreign)} besides you; "
            "m3_config.toml was NOT written. inspect: make the directory user-only "
            f"({icacls_user_only(directory, directory=True)}), or point M3_CONFIG_ROOT at a user-only directory"
        )


def _new_text() -> str:
    """A new file starts as the shipped default (every key commented at its
    default), verified against its generated hash: a file that is not the one
    this m3 shipped is refused, and nothing is written."""
    try:
        return default_file.verified_text()
    except default_file.DefaultFileError as e:
        raise ConfigWriteError(str(e)) from e


def create_from_default(location: Location, *, registry: Registry = REGISTRY) -> bool:
    """THE copy step (`m3 setup`, `m3 upgrade` and `m3 config import-env` reach it
    through import_env): when no file exists at ``location``, write the verified
    shipped default there, user-only (POSIX 0600 by atomic_write; on Windows the
    directory's inherited ACL is probed first), under the writer's lock. True when
    it wrote the file; False, touching nothing, when a file is already there:
    an existing m3_config.toml is never overwritten. The hash is checked before
    anything is created, so a refused copy leaves no file and no directory."""
    from m3_core.atomic_io import atomic_write
    from m3_core.locking import file_lock

    path = location.file
    if os.path.lexists(path):
        return False
    text = _new_text()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(str(path) + ".lock", purpose="m3_config.toml"):
        if os.path.lexists(path):  # another writer created it while we waited
            return False
        if is_windows():
            _check_new_file_would_be_private(path.parent)
        validate_text(path, text, registry)
        atomic_write(path, text)
    return True


def _key_lines(lines: list[str], table: str) -> range:
    """The line indexes of ``[table]``'s body (up to the next header); empty when absent."""
    start = None
    for i, line in enumerate(lines):
        m = _TABLE_HEADER.match(line)
        if m is None:
            continue
        if start is not None:
            return range(start, i)
        if m.group(1).replace(" ", "") == table:
            start = i + 1
    return range(start, len(lines)) if start is not None else range(0)


def _is_set(parsed: dict | None, key: Key) -> bool:
    node: object = parsed
    for part in key.name.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _in_place(text: str, values: Mapping[Key, ConfigValue | None]) -> str:
    """Set and unset keys where the default file lists them, so a key's value
    stays beside its doc instead of being appended at the end of its table with
    the stale commented default left behind (what tomlkit alone does).

    Set: a key not yet in the file whose ``# leaf = ...`` line follows its block's
    ``# type:`` line in its table becomes ``leaf = value`` on that line. Unset: a
    single-line ``leaf = ...`` in that position becomes the commented default again.
    Anything else (a hand-written file, a line the user commented) is left to tomlkit."""
    try:
        parsed: dict | None = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return text  # validate_text names the problem; editing a broken file is not this step's job
    lines = text.split("\n")
    for key, value in values.items():
        span = _key_lines(lines, key.table)
        if value is not None and not _is_set(parsed, key):
            prefix = default_file.commented_prefix(key.leaf)
            for i in span:
                if lines[i].startswith(prefix) and i > 0 and lines[i - 1].startswith(default_file.TYPE_MARK):
                    lines[i] = f"{key.leaf} = {tomlkit.item(_toml_native(value)).as_string()}"
                    break
        elif value is None and _is_set(parsed, key):
            for i in span:
                line = lines[i]
                if line.lstrip().startswith("#") or line.split("=", 1)[0].strip() != key.leaf:
                    continue
                if i > 0 and lines[i - 1].startswith(default_file.TYPE_MARK) and _one_line(line, key.leaf):
                    lines[i] = default_file.commented_line(key)
                break
    return "\n".join(lines)


def _one_line(line: str, leaf: str) -> bool:
    """The assignment is complete on this line (not a multi-line array or string)."""
    try:
        return leaf in tomllib.loads(line)
    except tomllib.TOMLDecodeError:
        return False


def _apply(doc: tomlkit.TOMLDocument, key: Key, value: ConfigValue | None) -> None:
    parts = key.name.split(".")
    node = doc
    for part in parts[:-1]:
        if part not in node:
            if value is None:
                return  # nothing to remove
            node.add(part, tomlkit.table(is_super_table=False))
        child = node[part]
        if not isinstance(child, dict):
            raise ConfigWriteError(f"{show_key(key.name)}: [{part}] is not a table in this file")
        node = child  # type: ignore[assignment]
    leaf = parts[-1]
    if value is None:
        if leaf in node:
            del node[leaf]
    else:
        node[leaf] = _toml_native(value)
