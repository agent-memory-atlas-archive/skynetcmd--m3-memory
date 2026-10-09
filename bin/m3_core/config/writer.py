"""The writer of m3_config.toml (CONFIG_DESIGN §12). tomlkit edits; tomllib
(``loader.validate_text``) remains the only parser that decides what the file
means, and it validates the NEW text before the old file is replaced.

Every write is a read-modify-write under an advisory lock (two writers cannot
lose each other's update), comments and layout survive (tomlkit round-trips
them), and the replace is atomic (``atomic_write``: a reader sees the old file
or the new one, never a mix; a new file is created user-only, an existing
file's mode is kept). Secrets are refused: they never live in this file (§10).
Writers are the installer, the wizard and (2F) ``m3 config set``; daemons never
write it (§3).
"""
from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

import tomlkit

from .keys import REGISTRY
from .loader import SCHEMA_VERSION, Location, show_key, validate_text
from .registry import ConfigValue, Key, KeyClass, Registry


class ConfigWriteError(ValueError):
    """A write that was refused; the file is unchanged."""


def _toml_native(value: ConfigValue) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return list(value)
    return value


def set_values(location: Location, values: Mapping[Key, ConfigValue | None], *,
               registry: Registry = REGISTRY) -> Path:
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
                + ("; store it with m3_core.secret_store" if key.klass is KeyClass.SECRET else ""))
        if values[key] is not None:
            key.check(values[key])

    path = location.file
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(str(path) + ".lock", purpose="m3_config.toml"):
        if sys.platform == "win32" and not path.exists():
            _check_new_file_would_be_private(path.parent)
        old = path.read_text(encoding="utf-8") if path.is_file() else ""
        doc = tomlkit.parse(old) if old else _new_document()
        for key, value in values.items():
            _apply(doc, key, value)
        new = tomlkit.dumps(doc)
        validate_text(path, new, registry)  # refuse BEFORE replacing (§12, #D26)
        if new != old:
            atomic_write(path, new)
    return path


def _check_new_file_would_be_private(directory: Path) -> None:
    """Windows: §12 creates m3_config.toml user-only (it holds hostnames, paths and
    URLs), but a new file takes its directory's ACL; atomic_write's 0600 applies on
    POSIX only. Probe what a new file here would inherit, and refuse before writing."""
    from m3_core.win_acl import AclError, file_foreign_principals, probe_new_file

    try:
        with probe_new_file(directory) as probe:
            foreign = file_foreign_principals(probe)
    except AclError as e:
        raise ConfigWriteError(f"observed: cannot verify who could read a new file in {directory} "
                               f"({e}); m3_config.toml was NOT written") from e
    if foreign:
        raise ConfigWriteError(
            f"observed: a new file in {directory} could be opened by {', '.join(foreign)} besides you; "
            "m3_config.toml was NOT written. inspect: make the directory user-only "
            f"(icacls \"{directory}\" /inheritance:r /grant:r \"%USERNAME%:(OI)(CI)F\" "
            "/grant:r \"SYSTEM:(OI)(CI)F\"), or point M3_CONFIG_ROOT at a user-only directory")


def _new_document() -> tomlkit.TOMLDocument:
    doc = tomlkit.document()
    doc.add(tomlkit.comment("m3 configuration. Every key: docs/CONFIG_REFERENCE.md. "
                            "Edit by hand or with `m3 config set`."))
    doc.add("schema_version", SCHEMA_VERSION)
    return doc


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
