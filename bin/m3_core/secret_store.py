"""The user-only secrets file: m3's secret tier where no keyring works (CONFIG_DESIGN §10, U6 (a)).

A machine with no keyring backend (a desktop-less Linux box: claude-dev has
``fail.Keyring`` in every context, measured 2026-10-08) still needs somewhere
for a DSN or token that is not a world-readable env var, rc line or ``.env``.
This file is that place:

* ``<config root>/m3_secrets.toml`` (until ``m3_config.toml``'s ``secrets.file``
  key exists, Phase 2C), one table, ``[secrets]``, of ``NAME = "value"``.
* Created user-only (0600 via ``atomic_write``); every read REFUSES a file that
  is group/other-accessible or not owned by the running user, naming the file,
  the mode and the fix (the ssh-key rule). On Windows the same rule is applied
  to the DACL (``m3_core.win_acl``): the file inherits its directory's ACL, so a
  write first checks what a new file there WOULD inherit, before any plaintext
  lands, and a read refuses a file anyone beyond the user, SYSTEM and
  Administrators can open.
* Written only under ``file_lock`` (read-modify-write), never relaxing an
  existing file's mode.

Resolution order lives in ``auth_utils.get_api_key``: env -> keyring -> this
file -> vault (the vault never for a bootstrap secret, CONFIG_DESIGN §10).
"""
from __future__ import annotations

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
    """``[secrets].file`` from m3_config.toml (relative paths are relative to the
    config root), else ``<config root>/m3_secrets.toml``. An invalid m3_config.toml
    raises SecretsFileError: secrets are never read from a file guessed past it."""
    from m3_core.config import ConfigFileError, format_config_error
    from m3_core.config.keys import SECRETS_FILE
    from m3_core.config.resolve import ConfigEnvErrors
    from m3_core.config.settings import current

    try:
        settings = current()
    except (ConfigFileError, ConfigEnvErrors) as e:
        raise SecretsFileError(
            "m3_config.toml is invalid, so the secrets file cannot be located:\n"
            + format_config_error(e)) from e
    configured = settings[SECRETS_FILE]
    if configured is None:
        return settings.location.root / SECRETS_FILE_NAME
    p = Path(configured)
    return p if p.is_absolute() else settings.location.root / p


def _check_private_windows(p: Path, *, what: str, fix: str) -> None:
    """Refuse a file whose DACL admits anyone beyond the user, SYSTEM and
    Administrators. An unreadable DACL is refused too: privacy not checked is
    not privacy."""
    from m3_core.win_acl import AclError, file_foreign_principals

    try:
        foreign = file_foreign_principals(str(p))
    except AclError as e:
        raise SecretsFileError(f"cannot verify who can read {what} ({e}); refusing to use it") from e
    if foreign:
        raise SecretsFileError(
            f"{what} can be opened by {', '.join(foreign)} besides you; a secrets file must be "
            f"user-only. Fix: {fix}")


def _check_private(p: Path) -> None:
    if sys.platform == "win32":
        _check_private_windows(p, what=str(p), fix=f"icacls \"{p}\" /inheritance:r /grant:r \"%USERNAME%:F\"")
        return
    st = p.stat()
    mode = st.st_mode & 0o777
    if mode & 0o077:
        raise SecretsFileError(f"{p} is mode {oct(mode)}; a secrets file must be user-only. Fix: chmod 600 {p}")
    if st.st_uid != os.getuid():
        raise SecretsFileError(
            f"{p} is owned by uid {st.st_uid}, not you ({os.getuid()}); refusing to read it. "
            f"Fix: if you created it, `chown $(id -u) {p}` as that owner or root; otherwise remove it."
        )


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
        raise SecretsFileError(f"{p} is not valid TOML ({e}); fix the line named, or remove the file") from e
    table = data.get("secrets", {})
    if not isinstance(table, dict):
        raise SecretsFileError(f"{p}: 'secrets' must be a [secrets] table, found {type(table).__name__}")
    bad = sorted(k for k, v in table.items() if not isinstance(v, str))
    if bad:
        # Names only: a value may be the secret itself.
        raise SecretsFileError(
            f"{p}: [secrets] entries must be NAME = \"string\"; not a string: {', '.join(bad)}"
        )
    _check_names(p, table)
    return dict(table)


def _known_secret_names() -> set[str]:
    """Registry secrets with their env aliases, plus the names DSNs are stored under."""
    from m3_core.config import REGISTRY, KeyClass

    known = {n for k in REGISTRY if k.klass is KeyClass.SECRET for n in (k.name, *k.env_names)}
    return known | {n for keys in _STORE_KEYS.values() for n in keys}


def _check_names(p: Path, table: dict) -> None:
    """CONFIG_DESIGN §10: an unknown name is an error naming the file and key,
    never the value. A name an installed SLM profile declares as its
    api_key_service is known too (user decision 2026-10-09), so a profile for a
    new provider works without a code change."""
    unknown = sorted(set(table) - _known_secret_names())
    if not unknown:
        return
    unreadable: list[str] = []
    try:
        from slm_intent import profile_secret_names

        declared, unreadable = profile_secret_names()
    except ImportError as e:
        declared, unreadable = set(), [f"profiles not readable ({e})"]
    unknown = [n for n in unknown if n not in declared]
    if not unknown:
        return
    note = f"; SLM profiles that could not be read: {'; '.join(unreadable)}" if unreadable else ""
    raise SecretsFileError(
        f"{p}: unknown secret name(s): {', '.join(unknown)}. A name must be an m3 secret or the "
        f"api_key_service of an installed SLM profile{note}. Fix the name, or remove the line.")


def get_secret(name: str) -> str | None:
    return read_secrets().get(name)


def _render(table: dict[str, str]) -> str:
    from m3_core.config.toml_render import render_table

    return render_table("secrets", table,
                        header="m3 secrets: user-only (0600). Managed by m3; values are plaintext.")


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


@dataclass(frozen=True, slots=True, kw_only=True)
class DsnResolution:
    dsn: str                 # "" when nothing resolved
    source: str | None       # e.g. "env:M3_PRIMARY_PG_URL", "keyring:M3_PRIMARY_PG_URL"
    errors: tuple[str, ...]  # what each tier HIT on the way, for an honest "not found"


class VaultPolicy(StrEnum):
    ALLOWED = "allowed"
    DEPRECATED = "deprecated"  # still read, warns every time (D1)
    NEVER = "never"            # a bootstrap secret (CONFIG_DESIGN §10)


@dataclass(frozen=True, slots=True, kw_only=True)
class SecretResolution:
    value: str               # "" when nothing resolved
    source: str | None       # "env:NAME", "keyring:NAME", "secrets-file:NAME", "vault:NAME"
    errors: tuple[str, ...]  # what each tier HIT on the way, for an honest "not found"


# D1 (user, 2026-10-08): the primary DSN's vault copy keeps working, loudly, until N+3.
_VAULT_DEPRECATED = frozenset({"M3_PRIMARY_PG_URL"})


def vault_policy(name: str) -> VaultPolicy:
    """Whether a secret may come from the vault: never for a registry
    ``bootstrap`` key (the registry is the one list), deprecated for D1's."""
    if name in _VAULT_DEPRECATED:
        return VaultPolicy.DEPRECATED
    from m3_core.config.keys import REGISTRY

    key = REGISTRY.get(name)
    return VaultPolicy.NEVER if key is not None and key.bootstrap else VaultPolicy.ALLOWED


def explain_secret(store_keys: tuple[str, ...], *, env_hit: tuple[str, str] | None,
                   vault: VaultPolicy) -> SecretResolution:
    """THE secret chain: env -> OS keyring -> user-only secrets file -> vault.

    ``env_hit`` is (name, value) from the caller's env reader, which owns that
    secret's env names and their legacy warnings. ``store_keys`` are the names
    the value is stored under in the keyring, the file and the vault. Every
    tier's failure is collected, so a miss is never reported as "not set" when a
    tier was broken (m3 2f4bad6c).
    """
    if env_hit is not None and env_hit[1].strip():
        return SecretResolution(value=env_hit[1].strip(), source=f"env:{env_hit[0]}", errors=())
    try:
        import auth_utils
    except ImportError as e:  # installer bootstrap: env is all there is
        return SecretResolution(value="", source=None, errors=(f"secret tiers unavailable: {e}",))

    errors: list[str] = []
    tiers = (("keyring", auth_utils._keyring_tier), ("secrets-file", auth_utils._file_tier))
    for tier, read in tiers:
        for key in store_keys:
            value, err = read(key)
            if err:
                errors.append(err)
            if value and value.strip():
                return SecretResolution(value=value.strip(), source=f"{tier}:{key}", errors=tuple(errors))
    if vault is VaultPolicy.NEVER:
        errors.append(f"vault: not consulted for {', '.join(store_keys)} (a bootstrap secret)")
        return SecretResolution(value="", source=None, errors=tuple(errors))
    for key in store_keys:
        value, err = auth_utils._vault_tier(key)
        if err:
            errors.append(err)
        if value and value.strip():
            if vault is VaultPolicy.DEPRECATED:
                logging.getLogger(__name__).warning(
                    "observed: %s came from the encrypted vault, which is deprecated for it: it is "
                    "a bootstrap secret, and the vault lives in the store it unlocks. It still "
                    "works; the vault fallback is removed at N+3. fix: `python -m keyring set "
                    "system %s` (prompts for the value, so it stays out of shell history), or set "
                    "%s in the environment of every m3 process.", key, key, key)
            return SecretResolution(value=value.strip(), source=f"vault:{key}", errors=tuple(errors))
    return SecretResolution(value="", source=None, errors=tuple(errors))


def _env_first(names: tuple[str, ...]) -> tuple[str, str] | None:
    """The first of ``names`` set to a non-blank value in the environment."""
    for n in names:
        v = os.environ.get(n)
        if v is not None and v.strip():
            return n, v
    return None


# A registry secret that is a role DSN resolves through its role, which owns the
# role's env names and the PG_URL deprecation warning.
_DSN_ROLE = {"M3_PRIMARY_PG_URL": PgRole.PRIMARY, "M3_CDW_PG_URL": PgRole.WAREHOUSE}


def explain_named_secret(name: str) -> SecretResolution:
    """Resolve one secret by name: a registry key with its env names, or a
    dynamic ``get_secret(service)`` name standing for itself."""
    role = _DSN_ROLE.get(name)
    if role is not None:
        d = explain_pg_dsn(role)
        return SecretResolution(value=d.dsn, source=d.source, errors=d.errors)
    from m3_core.config.keys import REGISTRY

    key = REGISTRY.get(name)
    env_names = key.env_names if key is not None and key.env_names else (name,)
    return explain_secret((name,), env_hit=_env_first(env_names), vault=vault_policy(name))


def explain_pg_dsn(role: PgRole) -> DsnResolution:
    """Resolve a role's DSN and say where it came from, or why nothing did.

    Env comes from the role's own variables via m3_core.paths, which owns their
    precedence and the legacy-name warning; the rest is ``explain_secret``. For
    PRIMARY the vault is a DEPRECATED fallback (D1). Never reads the other
    role's variables, so a warehouse DSN cannot become the primary store.
    """
    from m3_core.paths import resolve_cdw_pg_dsn, resolve_primary_pg_dsn

    env_reader = resolve_primary_pg_dsn if role is PgRole.PRIMARY else resolve_cdw_pg_dsn
    env = (env_reader("") or "").strip()
    env_hit = None
    if env:
        names = ("M3_PRIMARY_PG_URL", "M3_PG_URL") if role is PgRole.PRIMARY else ("M3_CDW_PG_URL", "PG_URL")
        env_hit = (next((n for n in names if (os.environ.get(n) or "").strip() == env), names[0]), env)
    r = explain_secret(_STORE_KEYS[role], env_hit=env_hit,
                       vault=vault_policy(_STORE_KEYS[role][0]))
    return DsnResolution(dsn=r.value, source=r.source, errors=r.errors)


def resolve_pg_dsn(role: PgRole) -> str:
    """The role's DSN, stripped, or "" when none resolves (see explain_pg_dsn)."""
    return explain_pg_dsn(role).dsn


@dataclass(frozen=True, slots=True, kw_only=True)
class DsnStored:
    location: str            # "keyring:<key>" or "secrets-file:<path>"
    skipped: tuple[str, ...]  # why an earlier store was not used


def _keyring_serves_daemons() -> bool:
    """Whether a keyring write made now is readable by m3's headless jobs.

    Windows Credential Manager and the macOS login keychain are (the AgentOS_*
    tasks run with Interactive logon; launchd user agents share the keychain).
    On Linux, cron and plain SSH have no session bus and a systemd --user unit
    sees the login keyring only after a PAM login (CONFIG_DESIGN §10, measured
    2026-10-08), so a read-back from the installer's own session proves nothing
    for them: the secrets file is the store there.
    """
    return sys.platform in ("win32", "darwin")


def store_pg_dsn(role: PgRole, dsn: str) -> DsnStored:
    """Store a role's DSN where m3's headless jobs can read it, verified by read-back.

    The keyring where it serves daemons, else the user-only secrets file (never
    the vault: a bootstrap secret, CONFIG_DESIGN §10). Raises SecretsFileError
    naming every store's failure when none took the value.
    """
    import auth_utils

    key = _STORE_KEYS[role][0]
    dsn = dsn.strip()
    skipped: list[str] = []
    if _keyring_serves_daemons():
        err = auth_utils._keyring_store(key, dsn)
        if err is None:
            back, read_err = auth_utils._keyring_tier(key)
            if back == dsn:
                return DsnStored(location=f"keyring:{key}", skipped=())
            err = f"keyring: wrote {key} but the read-back " + (
                f"failed ({read_err})" if read_err else "returned a different value")
        skipped.append(err)
    else:
        skipped.append(f"keyring: not used on {sys.platform} (cron and SSH jobs cannot read it)")
    try:
        p = set_secret(key, dsn)
        if get_secret(key) != dsn:
            raise SecretsFileError(f"{p}: wrote {key} but the read-back returned a different value")
    except (OSError, SecretsFileError) as e:
        raise SecretsFileError(
            f"could not store the {role.value} PostgreSQL DSN ({key}) anywhere: "
            + " | ".join([*skipped, f"secrets file: {type(e).__name__}: {e}"])) from e
    return DsnStored(location=f"secrets-file:{p}", skipped=tuple(skipped))


def set_secret(name: str, value: str) -> Path:
    """Store one secret (create or replace) under the file lock. Returns the path."""
    if not _NAME.fullmatch(name):
        raise ValueError(f"secret name {name!r}: use letters, digits, '_' or '-'")
    from m3_core.atomic_io import atomic_write
    from m3_core.locking import file_lock

    p = secrets_file_path()
    _check_names(p, {name: value})  # refuse before writing a file that could not be read back
    with file_lock(str(p) + ".lock", purpose="secrets file"):
        table = read_secrets()  # an existing file is checked here
        if sys.platform == "win32" and not p.exists():
            _check_new_file_would_be_private(p.parent)
        table[name] = value
        atomic_write(p, _render(table))
    return p


def _check_new_file_would_be_private(directory: Path) -> None:
    """Windows: a new file takes its directory's inheritable ACL, and atomic_write's
    0600 means nothing there. So learn what a file in ``directory`` inherits from an
    EMPTY probe, and refuse before the plaintext is written."""
    import tempfile

    # 3.12 delete_on_close=False: closed before the ACL is read, removed on exit
    # even when the check raises (the same idiom as atomic_write).
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".m3_acl_probe.",
                                     delete=True, delete_on_close=False) as probe:
        probe.close()
        _check_private_windows(
            Path(probe.name), what=f"a new file in {directory} (the secrets file was NOT written)",
            fix=(f"icacls \"{directory}\" /inheritance:r /grant:r \"%USERNAME%:(OI)(CI)F\" "
                 "/grant:r \"SYSTEM:(OI)(CI)F\", or point M3_CONFIG_ROOT at a user-only directory"))
