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
import logging
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from enum import StrEnum
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


# ── PostgreSQL DSNs: the one owner (plan 2B.1, review S5) ─────────────────────
class PgRole(StrEnum):
    PRIMARY = "primary"      # the live store (M3_DB_BACKEND=postgres)
    WAREHOUSE = "warehouse"  # the pg_sync fan-in mirror (CDW)


# Names a DSN is stored under in the keyring / secrets file / vault, per role.
# The warehouse keeps PG_URL for continuity: every existing vault holds it there.
_STORE_KEYS: dict[PgRole, tuple[str, ...]] = {
    PgRole.PRIMARY: ("M3_PRIMARY_PG_URL",),
    PgRole.WAREHOUSE: ("M3_CDW_PG_URL", "PG_URL"),
}


@dataclass(frozen=True)
class DsnResolution:
    dsn: str                 # "" when nothing resolved
    source: str | None       # e.g. "env:M3_PRIMARY_PG_URL", "keyring:M3_PRIMARY_PG_URL"
    errors: tuple[str, ...]  # what each tier HIT on the way, for an honest "not found"


def explain_pg_dsn(role: PgRole) -> DsnResolution:
    """Resolve a role's DSN and say where it came from, or why nothing did.

    Order: env (the role's own variables, via m3_core.paths, which owns their
    precedence and the legacy-name warning) -> OS keyring -> user-only secrets
    file -> vault. For PRIMARY the vault is a DEPRECATED fallback (user decision
    D1, 2026-10-08): CONFIG_DESIGN §10 says a bootstrap secret never comes from
    the vault, so using it warns every time until it is retired at N+3. Never
    reads the other role's variables, so a warehouse DSN cannot become the
    primary store through resolution.
    """
    from m3_core.paths import resolve_cdw_pg_dsn, resolve_primary_pg_dsn

    errors: list[str] = []
    env_reader = resolve_primary_pg_dsn if role is PgRole.PRIMARY else resolve_cdw_pg_dsn
    env = (env_reader("") or "").strip()
    if env:
        names = ("M3_PRIMARY_PG_URL", "M3_PG_URL") if role is PgRole.PRIMARY else ("M3_CDW_PG_URL", "PG_URL")
        name = next((n for n in names if (os.environ.get(n) or "").strip() == env), names[0])
        return DsnResolution(env, f"env:{name}", ())
    try:
        import auth_utils
    except ImportError as e:  # installer bootstrap: env is all there is
        return DsnResolution("", None, (f"secret tiers unavailable: {e}",))

    tiers = (("keyring", auth_utils._keyring_tier), ("secrets-file", auth_utils._file_tier))
    for tier, read in tiers:
        for key in _STORE_KEYS[role]:
            value, err = read(key)
            if err:
                errors.append(err)
            if value and value.strip():
                return DsnResolution(value.strip(), f"{tier}:{key}", tuple(errors))
    for key in _STORE_KEYS[role]:
        value, err = auth_utils._vault_tier(key)
        if err:
            errors.append(err)
        if value and value.strip():
            if role is PgRole.PRIMARY:
                logging.getLogger(__name__).warning(
                    "The primary PostgreSQL DSN came from the encrypted vault, which is deprecated "
                    "for it (a bootstrap secret; the vault lives in the store it unlocks). Store it "
                    "in the keyring or the secrets file instead (`m3 doctor` names how); the vault "
                    "fallback is removed at N+3.")
            return DsnResolution(value.strip(), f"vault:{key}", tuple(errors))
    return DsnResolution("", None, tuple(errors))


def resolve_pg_dsn(role: PgRole) -> str:
    """The role's DSN, stripped, or "" when none resolves (see explain_pg_dsn)."""
    return explain_pg_dsn(role).dsn


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
