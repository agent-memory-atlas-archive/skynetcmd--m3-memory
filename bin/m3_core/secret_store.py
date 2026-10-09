"""The user-only secrets file: m3's secret tier where no keyring works (CONFIG_DESIGN §10, U6 (a)).

A machine with no keyring backend (a desktop-less Linux box: claude-dev has
``fail.Keyring`` in every context, measured 2026-10-08) still needs somewhere
for a DSN or token that is not a world-readable env var, rc line or ``.env``.
This file is that place:

* ``<config root>/m3_secrets.toml`` (until ``m3_config.toml``'s ``secrets.file``
  key exists, Phase 2C), one table, ``[secrets]``, of ``NAME = "value"``.
* Created user-only (0600 via ``atomic_write``); every read REFUSES a file that
  is group/other-accessible or not owned by the running user, naming the file,
  the mode and the fix (the ssh-key rule). On Windows it inherits the profile
  ACL (measured: the user, SYSTEM, Administrators); the ACL check comes with the
  loader in 2C.1.
* Written only under ``file_lock`` (read-modify-write), never relaxing an
  existing file's mode.

Resolution order lives in ``auth_utils.get_api_key``: env -> keyring -> this
file -> vault (the vault never for a bootstrap secret, CONFIG_DESIGN §10).
"""
from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from pathlib import Path

SECRETS_FILE_NAME = "m3_secrets.toml"
_NAME = re.compile(r"[A-Za-z0-9_-]+")


class SecretsFileError(Exception):
    """The secrets file exists but must not be used (permissions, ownership, syntax)."""


def secrets_file_path() -> Path:
    from m3_core.paths import get_m3_config_root
    return Path(get_m3_config_root()) / SECRETS_FILE_NAME


def _check_private(p: Path) -> None:
    if sys.platform == "win32":  # profile ACL; the ACL check lands with the 2C.1 loader
        return
    st = p.stat()
    mode = st.st_mode & 0o777
    if mode & 0o077:
        raise SecretsFileError(f"{p} is mode {oct(mode)}; a secrets file must be user-only. Fix: chmod 600 {p}")
    if st.st_uid != os.getuid():
        raise SecretsFileError(f"{p} is owned by uid {st.st_uid}, not you ({os.getuid()}); refusing to read it")


def read_secrets() -> dict[str, str]:
    """All secrets in the file; {} when there is no file. Raises SecretsFileError
    for a file that exists but is unsafe or malformed (never silently empty)."""
    p = secrets_file_path()
    if not p.is_file():
        return {}
    _check_private(p)
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise SecretsFileError(f"{p} is not valid TOML: {e}") from e
    table = data.get("secrets", {})
    if not isinstance(table, dict) or not all(isinstance(v, str) for v in table.values()):
        raise SecretsFileError(f"{p}: [secrets] must hold NAME = \"string\" entries only")
    return dict(table)


def get_secret(name: str) -> str | None:
    return read_secrets().get(name)


def _render(table: dict[str, str]) -> str:
    # JSON string escapes (\" \\ \n \uXXXX ...) are all valid TOML basic-string
    # escapes, so json.dumps renders a correct TOML string for any value.
    lines = ["# m3 secrets: user-only (0600). Managed by m3; values are plaintext.", "[secrets]"]
    lines += [f"{k} = {json.dumps(v)}" for k, v in sorted(table.items())]
    return "\n".join(lines) + "\n"


def set_secret(name: str, value: str) -> Path:
    """Store one secret (create or replace) under the file lock. Returns the path."""
    if not _NAME.fullmatch(name):
        raise ValueError(f"secret name {name!r}: use letters, digits, '_' or '-'")
    from m3_core.atomic_io import atomic_write
    from m3_core.locking import file_lock

    p = secrets_file_path()
    with file_lock(str(p) + ".lock", purpose="secrets file"):
        table = read_secrets()
        table[name] = value
        atomic_write(p, _render(table))
    return p
