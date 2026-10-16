"""The user-only secrets file: where m3 keeps its secrets, and THE list of them.

Every OS stores secrets here; the OS keyring holds
one only when the person asked (`m3 secret set NAME --keyring`), and then the
file names where it is:

* ``secrets.file`` in m3_config.toml, else ``<config root>/m3_secrets.toml``;
  one table, ``[secrets]``, of ``NAME = "value"`` or
  ``NAME = { keyring = "service/key" }``.
* Created user-only (0600 via ``atomic_write``); every read REFUSES a file that
  is group/other-accessible or not owned by the running user, naming the file,
  the mode and the fix (the ssh-key rule). On Windows the same rule is applied
  to the DACL (``m3_core.win_acl``): the file inherits its directory's ACL, so a
  write first checks what a new file there WOULD inherit, before any plaintext
  lands, and a read refuses a file anyone beyond the user, SYSTEM and
  Administrators can open.
* Written only under ``file_lock`` (read-modify-write), never relaxing an
  existing file's mode.

Resolution (``explain_secret``): the file's value -> the keyring entry the file
points at, or ``system/NAME`` when the file does not list the name -> the vault
(never for a bootstrap secret). The environment is NEVER read
for a secret; a name found there only makes a miss
say so, with the command that stores it.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import override

from m3_core.seams import current_uid, is_windows

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
            "m3_config.toml is invalid, so the secrets file cannot be located:\n" + format_config_error(e)
        ) from e
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
            f"{what} can be opened by {', '.join(foreign)} besides you; a secrets file must be user-only. Fix: {fix}"
        )


def _check_private(p: Path) -> None:
    if is_windows():
        from m3_core.win_acl import icacls_user_only

        _check_private_windows(p, what=str(p), fix=icacls_user_only(p))
        return
    st = p.stat()
    mode = st.st_mode & 0o777
    if mode & 0o077:
        raise SecretsFileError(f"{p} is mode {oct(mode)}; a secrets file must be user-only. Fix: chmod 600 {p}")
    uid = current_uid()
    if uid is not None and st.st_uid != uid:
        raise SecretsFileError(
            f"{p} is owned by uid {st.st_uid}, not you ({uid}); refusing to read it. "
            f"Fix: if you created it, `chown $(id -u) {p}` as that owner or root; otherwise remove it."
        )


@dataclass(frozen=True, slots=True)
class KeyringRef:
    """A secrets-file entry naming where the value lives instead of holding it:
    ``NAME = { keyring = "service/key" }``."""

    service: str
    key: str

    @classmethod
    def parse(cls, text: str) -> KeyringRef:
        service, sep, key = text.partition("/")
        if not (sep and service and key):
            raise ValueError(f"{text!r} is not service/key")
        return cls(service, key)

    @override
    def __str__(self) -> str:
        return f"{self.service}/{self.key}"


def default_keyring_ref(name: str) -> KeyringRef:
    """Where a secret the file does not list is looked for in the keyring."""
    return KeyringRef("system", name)


def read_secret_entries() -> dict[str, str | KeyringRef]:
    """Every entry in the file: a value, or a ``KeyringRef``. {} when there is no
    file. Raises SecretsFileError for a file that exists but is unsafe or
    malformed (never silently empty)."""
    p = secrets_file_path()
    if not os.path.isfile(p):
        return {}
    _check_private(p)
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise SecretsFileError(f"{p} is not valid TOML ({e}); fix the line named, or remove the file") from e
    table = data.get("secrets", {})
    if not isinstance(table, dict):
        raise SecretsFileError(f"{p}: 'secrets' must be a [secrets] table, found {type(table).__name__}")
    out: dict[str, str | KeyringRef] = {}
    bad: list[str] = []
    for name, entry in table.items():
        if isinstance(entry, str):
            out[name] = entry
        elif isinstance(entry, dict) and set(entry) == {"keyring"} and isinstance(entry["keyring"], str):
            try:
                out[name] = KeyringRef.parse(entry["keyring"])
            except ValueError:
                bad.append(name)
        else:
            bad.append(name)
    if bad:
        # Names only: a value may be the secret itself.
        raise SecretsFileError(
            f'{p}: [secrets] entries must be NAME = "value" or NAME = {{ keyring = "service/key" }}; '
            f"not one of those: {', '.join(sorted(bad))}"
        )
    _check_names(p, out)
    return out


def read_secrets() -> dict[str, str]:
    """The secrets whose VALUE is in the file (keyring pointers left out)."""
    return {k: v for k, v in read_secret_entries().items() if isinstance(v, str)}


def _known_secret_names() -> set[str]:
    """Registry secrets with their env aliases, plus the names DSNs are stored under."""
    from m3_core.config import REGISTRY, KeyClass

    known = {n for k in REGISTRY if k.klass is KeyClass.SECRET for n in (k.name, *k.env_names)}
    return known | {n for keys in _STORE_KEYS.values() for n in keys} | _store_dsn_secrets()


def _store_dsn_secrets() -> set[str]:
    """The ``dsn_secret`` each named store in m3_config.toml declares: the secrets
    file is where such a DSN is kept, so the name must be one the file accepts."""
    from m3_core.config import ConfigFileError
    from m3_core.config.resolve import ConfigEnvErrors
    from m3_core.config.settings import current

    try:
        stores = current().stores
    except (ConfigFileError, ConfigEnvErrors):  # secrets_file_path reports the invalid file itself
        return set()
    return {s.dsn_secret for s in stores.values() if s.dsn_secret}


def _check_names(p: Path, table: dict) -> None:
    """An unknown name is an error naming the file and key,
    never the value. A name an installed SLM profile declares as its
    api_key_service is known too, so a profile for a
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
        f"api_key_service of an installed SLM profile{note}. Fix the name, or remove the line."
    )


# URI userinfo: scheme://[user]:password@host. The password runs to the LAST '@'
# before whitespace: users paste unencoded '/', ':' and '@' into it, and a match
# stopping at the first '/' left the rest of the password in the line. Hazard of
# the greedy span: a later '@' in the same token masks the host too (safe side).
_DSN_URI_PASSWORD = re.compile(r"(://[^:/@\s]*:)[^\s]+(@)")
# keyword/value form (`password=p`, `password = 'p w'`) and a URI query
# parameter (`?password=p&sslmode=...`): libpq accepts the credential in both.
_DSN_KEYWORD_PASSWORD = re.compile(r"(?i)(?<![A-Za-z0-9_])(password\s*=\s*)('(?:[^'\\]|\\.)*'|[^\s&'\"]+)")


def mask_dsn_passwords(text: str) -> str:
    """``text`` with the password of every DSN in it replaced by ``***``, in
    either libpq form (URI userinfo or query, keyword/value); text with no DSN
    credential is returned as it is. Safe for an error line."""
    text = _DSN_URI_PASSWORD.sub(r"\1***\2", text)
    return _DSN_KEYWORD_PASSWORD.sub(r"\1***", text)


def loggable_url(url: object, *, path: bool = True) -> str:
    """``scheme://host[:port]/path`` of ``url`` for an error line: no userinfo,
    query or fragment (``?key=`` and ``user:pass@`` carry credentials); without
    the path when ``path`` is False. A value that does not parse as a URL is
    returned with any DSN password masked."""
    from urllib.parse import urlsplit

    text = str(url or "")
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return mask_dsn_passwords(text.split("?", 1)[0])
    # urlsplit ends the netloc at the first '/', so a password with an unencoded
    # '/' put its tail and the real host into the path ('u:12/pw@h' parsed as
    # host 'u', port 12, path '/pw@h'). An '@' past the netloc is that ambiguity:
    # return the masked text rather than a host that may be wrong.
    if not parts.scheme or not host or "@" in parts.path:
        return mask_dsn_passwords(text.split("?", 1)[0])
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}{f':{port}' if port else ''}{parts.path if path else ''}"


_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")


def redacted_snippet(body: object, limit: int = 200) -> str:
    """At most ``limit`` characters of a response body or error text for a
    message: whitespace collapsed, any bearer token replaced. THE redaction for
    echoing what a server said; a body can quote the request's Authorization."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body or "")
    text = " ".join(_BEARER_RE.sub("Bearer [REDACTED]", text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def mask_secret(value: str) -> str:
    """A value safe to show a person choosing between copies: a DSN keeps all
    but its password
    (``postgresql://u:***@host/db``); anything else keeps at most its first 4
    and last 2 characters, and nothing when it is too short for that to hide it."""
    if "://" in value:
        return mask_dsn_passwords(value)
    return f"{value[:4]}***{value[-2:]} ({len(value)} chars)" if len(value) >= 16 else f"*** ({len(value)} chars)"


def get_secret(name: str) -> str | None:
    return read_secrets().get(name)


_HEADER = (
    'm3 secrets: user-only (0600). Values are plaintext; NAME = { keyring = "service/key" } '
    "means the value is in the OS keyring under that entry. A secret not listed here is read "
    'from the keyring (service "system", key NAME).'
)


def _rewrite_file(p: Path, change) -> None:
    """Apply ``change(table)`` to the [secrets] table and write the file back,
    keeping comments, other entries and pointers (tomlkit round-trip). Call under
    the file lock, after ``read_secret_entries()`` has checked the existing file."""
    import tomlkit

    from m3_core.atomic_io import atomic_write

    if os.path.exists(p):
        doc = tomlkit.parse(p.read_text(encoding="utf-8"))
    else:
        if is_windows():
            _check_new_file_would_be_private(p.parent)
        doc = tomlkit.document()
        doc.add(tomlkit.comment(_HEADER))
    table = doc.get("secrets")
    if table is None:
        table = tomlkit.table()
        doc["secrets"] = table
    change(table)
    atomic_write(p, tomlkit.dumps(doc))


# ── PostgreSQL DSNs: the one owner ─────────────────────
class PgRole(StrEnum):
    PRIMARY = "primary"  # the live store (db.backend = "postgres")
    WAREHOUSE = "warehouse"  # the pg_sync fan-in mirror (CDW)


# Names a DSN is stored under in the keyring / secrets file / vault, per role.
# The warehouse keeps PG_URL for continuity: every existing vault holds it there.
_STORE_KEYS: dict[PgRole, tuple[str, ...]] = {
    PgRole.PRIMARY: ("M3_PRIMARY_PG_URL",),
    PgRole.WAREHOUSE: ("M3_CDW_PG_URL", "PG_URL"),
}


@dataclass(frozen=True, slots=True, kw_only=True)
class DsnResolution:
    dsn: str  # "" when nothing resolved
    source: str | None  # e.g. "secrets-file:M3_PRIMARY_PG_URL", "keyring:system/M3_PRIMARY_PG_URL"
    errors: tuple[str, ...]  # what each tier HIT on the way, for an honest "not found"


class VaultPolicy(StrEnum):
    ALLOWED = "allowed"
    DEPRECATED = "deprecated"  # still read, warns every time (D1)
    NEVER = "never"  # a bootstrap secret


@dataclass(frozen=True, slots=True, kw_only=True)
class SecretResolution:
    value: str  # "" when nothing resolved
    source: str | None  # "secrets-file:NAME", "keyring:service/key", "vault:NAME"
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


def explain_secret(
    store_keys: tuple[str, ...], *, vault: VaultPolicy, env_names: tuple[str, ...] = ()
) -> SecretResolution:
    """THE secret chain: the file's value -> the OS keyring -> the vault.

    ``store_keys`` are the names the value is stored under. The keyring entry is
    the one the file points at (``NAME = { keyring = "service/key" }``), or
    ``system/NAME`` for a name the file does not list. The environment is never
    read for the value; ``env_names`` are only looked up so that a
    miss can say "set in the environment, which m3 does not read" and name the
    command that stores it. Every tier's failure is collected, so a miss is never
    reported as "not set" when a tier was broken.
    """
    errors: list[str] = []
    try:
        entries = read_secret_entries()
    except SecretsFileError as e:
        logging.getLogger(__name__).error(
            "observed: the secrets file was refused while resolving %s: %s. Not read; trying the keyring and vault.",
            ", ".join(store_keys),
            e,
        )
        errors.append(f"secrets file: {e}")
        entries = {}
    for key in store_keys:
        entry = entries.get(key)
        if isinstance(entry, str) and entry.strip():
            return SecretResolution(value=entry.strip(), source=f"secrets-file:{key}", errors=tuple(errors))

    try:
        import auth_utils
    except ImportError as e:  # installer bootstrap: the file is all there is
        errors.append(f"keyring and vault unavailable: {e}")
        return _miss(store_keys, env_names, errors)

    for key in store_keys:
        entry = entries.get(key)
        ref = entry if isinstance(entry, KeyringRef) else default_keyring_ref(key)
        value, err = auth_utils._keyring_tier(ref.key, service=ref.service)
        if err:
            errors.append(err)
        if value and value.strip():
            return SecretResolution(value=value.strip(), source=f"keyring:{ref}", errors=tuple(errors))
        if isinstance(entry, KeyringRef):
            errors.append(f"secrets file: {key} points at keyring {ref}, which holds no value")
    if vault is VaultPolicy.NEVER:
        errors.append(f"vault: not consulted for {', '.join(store_keys)} (a bootstrap secret)")
        return _miss(store_keys, env_names, errors)
    for key in store_keys:
        value, err = auth_utils._vault_tier(key)
        if err:
            errors.append(err)
        if value and value.strip():
            if vault is VaultPolicy.DEPRECATED:
                logging.getLogger(__name__).warning(
                    "observed: %s came from the encrypted vault, which is deprecated for it: it is "
                    "a bootstrap secret, and the vault lives in the store it unlocks. It still "
                    "works; the vault fallback is removed at N+3. fix: `m3 secret set %s` "
                    "(prompts for the value, so it stays out of shell history).",
                    key,
                    key,
                )
            return SecretResolution(value=value.strip(), source=f"vault:{key}", errors=tuple(errors))
    return _miss(store_keys, env_names, errors)


def _miss(store_keys: tuple[str, ...], env_names: tuple[str, ...], errors: list[str]) -> SecretResolution:
    """Nothing resolved. A name set in the environment is named (never its
    value): m3 no longer reads it there, and that is the likeliest reason."""
    # env-owner: names a secret's env name still set (never its value or a read of it)
    in_env = [n for n in env_names if (os.environ.get(n) or "").strip()]
    if in_env:
        errors.append(
            f"{', '.join(in_env)} is set in the environment, which m3 does not read for secrets; "
            f"store it: `m3 secret set {store_keys[0]}` (prompts), or `m3 config import-env` "
            "(moves it from your rc files, .env, MCP config and registry)"
        )
    return SecretResolution(value="", source=None, errors=tuple(errors))


# A registry secret that is a role DSN resolves through its role.
_DSN_ROLE = {"M3_PRIMARY_PG_URL": PgRole.PRIMARY, "M3_CDW_PG_URL": PgRole.WAREHOUSE}
# The env names that once held each role's DSN: read for the miss message only.
_DSN_ENV_NAMES: dict[PgRole, tuple[str, ...]] = {
    PgRole.PRIMARY: ("M3_PRIMARY_PG_URL", "M3_PG_URL"),
    PgRole.WAREHOUSE: ("M3_CDW_PG_URL", "PG_URL"),
}


def store_keys_for(name: str) -> tuple[str, ...]:
    """The names a secret's value may be stored under, its own name first (the
    warehouse DSN may sit under the older PG_URL)."""
    role = _DSN_ROLE.get(name)
    return _STORE_KEYS[role] if role is not None else (name,)


def keyring_value(ref: KeyringRef) -> tuple[str | None, str | None]:
    """(value, error) of one keyring entry: the keyring tier, for a caller that
    is not resolving a secret but asking where one lives (import-env, doctor)."""
    import auth_utils

    return auth_utils._keyring_tier(ref.key, service=ref.service)


def explain_named_secret(name: str) -> SecretResolution:
    """Resolve one secret by name: a registry key, or a dynamic
    ``get_api_key(service)`` name standing for itself."""
    role = _DSN_ROLE.get(name)
    if role is not None:
        d = explain_pg_dsn(role)
        return SecretResolution(value=d.dsn, source=d.source, errors=d.errors)
    from m3_core.config.keys import REGISTRY

    key = REGISTRY.get(name)
    env_names = key.env_names if key is not None and key.env_names else (name,)
    return explain_secret((name,), vault=vault_policy(name), env_names=env_names)


def explain_pg_dsn(role: PgRole) -> DsnResolution:
    """Resolve a role's DSN and say where it came from, or why nothing did.
    For PRIMARY the vault is a DEPRECATED fallback (D1). Only the role's own
    names are read, so a warehouse DSN cannot become the primary store."""
    r = explain_secret(_STORE_KEYS[role], vault=vault_policy(_STORE_KEYS[role][0]), env_names=_DSN_ENV_NAMES[role])
    return DsnResolution(dsn=r.value, source=r.source, errors=r.errors)


def resolve_pg_dsn(role: PgRole) -> str:
    """The role's DSN, stripped, or "" when none resolves (see explain_pg_dsn)."""
    return explain_pg_dsn(role).dsn


@dataclass(frozen=True, slots=True, kw_only=True)
class DsnStored:
    location: str  # "secrets-file:<path>"


def store_pg_dsn(role: PgRole, dsn: str) -> DsnStored:
    """Store a role's DSN in the user-only secrets file, verified by read-back.
    The file on every OS: m3 writes the keyring
    only when asked (`m3 secret set NAME --keyring`). Raises SecretsFileError
    naming what failed."""
    key = _STORE_KEYS[role][0]
    dsn = dsn.strip()
    try:
        p = set_secret(key, dsn)
        if get_secret(key) != dsn:
            raise SecretsFileError(f"{p}: wrote {key} but the read-back returned a different value")
    except (OSError, SecretsFileError) as e:
        raise SecretsFileError(
            f"could not store the {role.value} PostgreSQL DSN ({key}) in the secrets file: {type(e).__name__}: {e}"
        ) from e
    return DsnStored(location=f"secrets-file:{p}")


def is_known_secret(name: str) -> bool:
    """A registry secret (or env alias), a DSN store name, or an SLM profile's
    api_key_service: the names the secrets file accepts."""
    if name in _known_secret_names():
        return True
    try:
        from slm_intent import profile_secret_names

        declared, _unreadable = profile_secret_names()
    except ImportError:
        return False
    return name in declared


def store_in_keyring(name: str, value: str) -> str:
    """Put ``name`` in the OS keyring (service "system"), verified by read-back,
    and make the secrets file point at it (replacing any value there: a value in
    the file would win over the keyring). Only on the user's explicit request
    (`m3 secret set NAME --keyring`). Returns where the value went; raises
    SecretsFileError naming what failed, never the value."""
    import auth_utils

    ref = default_keyring_ref(name)
    err = auth_utils._keyring_store(name, value)
    if err is not None:
        raise SecretsFileError(f"could not store {name} in the OS keyring: {err}")
    back, read_err = auth_utils._keyring_tier(ref.key, service=ref.service)
    if back != value:
        raise SecretsFileError(
            f"stored {name} in the OS keyring but the read-back "
            + (f"failed ({read_err})" if read_err else "returned a different value")
        )
    set_secret(name, ref)  # bumps the generation: running processes drop their cached copy
    return f"keyring:{ref}"


def set_secret(name: str, value: str | KeyringRef) -> Path:
    """Store one secret, or a pointer to where it lives, under the file lock
    (create or replace). Returns the path. Comments, other entries and pointers
    survive the write."""
    if not _NAME.fullmatch(name):
        raise ValueError(f"secret name {name!r}: use letters, digits, '_' or '-'")
    from m3_core.locking import file_lock

    p = secrets_file_path()
    _check_names(p, {name: value})  # refuse before writing a file that could not be read back
    with file_lock(str(p) + ".lock", purpose="secrets file"):
        read_secret_entries()  # an existing file is checked here (permissions, syntax, names)

        def put(table) -> None:
            if isinstance(value, KeyringRef):
                import tomlkit

                ref = tomlkit.inline_table()
                ref["keyring"] = str(value)
                table[name] = ref
            else:
                table[name] = value

        _rewrite_file(p, put)
    bump_secret_generation()  # running processes drop their cached copy
    return p


def delete_secret(name: str) -> bool:
    """Remove one entry (a value or a keyring pointer) under the file lock;
    other entries and comments survive. True when it was there. A keyring entry
    the pointer named is left in the keyring."""
    from m3_core.locking import file_lock

    p = secrets_file_path()
    if not os.path.isfile(p):
        return False
    with file_lock(str(p) + ".lock", purpose="secrets file"):
        if name not in read_secret_entries():  # checks the file (permissions, syntax, names)
            return False

        def drop(table) -> None:
            del table[name]

        _rewrite_file(p, drop)
    bump_secret_generation()  # running processes drop their cached copy
    return True


def _check_new_file_would_be_private(directory: Path) -> None:
    """Windows: a new file takes its directory's inheritable ACL, and atomic_write's
    0600 means nothing there. So learn what a file in ``directory`` inherits from an
    EMPTY probe, and refuse before the plaintext is written."""
    from m3_core.win_acl import icacls_user_only, probe_new_file

    with probe_new_file(directory) as probe:
        _check_private_windows(
            Path(probe),
            what=f"a new file in {directory} (the secrets file was NOT written)",
            fix=f"{icacls_user_only(directory, directory=True)}, or point M3_CONFIG_ROOT at a user-only directory",
        )


# ── Per-process secret cache with a refresh signal ──
# A hot path (the cloud embed token, read per embed call) must not touch the
# keyring every time: a macOS keychain read can prompt. So a secret is resolved
# once per process and cached. A changed key still reaches long-running processes:
# every m3 write of a secret (secrets file, vault, PG DSN) and `m3 secret refresh`
# rewrite a GENERATION marker; a cached process checks its mtime at most every
# _GEN_TTL_S seconds and drops its cache when it moved. A marker file, not a
# signal: SIGHUP has no Windows equivalent.
_GEN_TTL_S = 5.0
_cache: dict[str, str | None] = {}
_gen_seen: int | None = None
_gen_checked_at = 0.0


def secret_generation_path() -> Path:
    """``<config root>/.secrets_generation``: the refresh signal's one location."""
    from m3_core.seams import roots

    return Path(roots.config_root()) / ".secrets_generation"


def _generation() -> int:
    try:
        return secret_generation_path().stat().st_mtime_ns
    except OSError:
        return 0


def bump_secret_generation() -> Path:
    """Signal every m3 process that cached secrets are stale, and drop this one's."""
    import time

    from m3_core.atomic_io import atomic_write

    p = secret_generation_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(p, f"{time.time_ns()}\n")
    clear_secret_cache()
    return p


def clear_secret_cache() -> None:
    global _gen_seen, _gen_checked_at
    _cache.clear()
    _gen_seen, _gen_checked_at = None, 0.0


def cached_secret(name: str, resolve) -> str | None:
    """``resolve(name)``, once per process until the generation marker moves."""
    import time

    global _gen_seen, _gen_checked_at
    now = time.monotonic()
    if now - _gen_checked_at >= _GEN_TTL_S:
        _gen_checked_at = now
        gen = _generation()
        if gen != _gen_seen:
            _cache.clear()
            _gen_seen = gen
    if name not in _cache:
        _cache[name] = resolve(name)
    return _cache[name]
