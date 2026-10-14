from __future__ import annotations

import base64
import logging
import os
import threading
from types import FrameType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cryptography.fernet import Fernet

from crypto_provider import provider as crypto
from m3_core.seams import is_windows, os_name

# --- Global Sync ---
_crypto_lock = threading.Lock()
import platform
import queue
import re
import subprocess
import time
import unicodedata
from datetime import UTC, datetime

from _task_runtime import no_window_kwargs
from m3_core.config import settings as _m3cfg
from m3_core.errors import log_swallowed

logger = logging.getLogger(__name__)

# Keyring Circuit Breaker & Concurrency state
_KEYRING_CB_OPEN_UNTIL = 0.0
_KEYRING_LOCK = threading.Lock()


def safe_keyring_get_password(service_name: str, username: str) -> str | None:
    """Wraps keyring.get_password with a single-concurrency lock and 2s timeout.

    If it times out or fails, opens Keyring Circuit Breaker for 300s and returns None.
    A caller that must tell "not stored" from "keyring broken" uses _keyring_read.
    """
    return _keyring_read(service_name, username)[0]


def _keyring_read(service_name: str, username: str) -> tuple[str | None, str | None]:
    """keyring.get_password, bounded, as (value, error). The ONE keyring reader.

    ``error`` is None for a clean answer (found, or not stored) and names the
    failure otherwise, so a miss caused by a broken keyring is never reported as
    "not set" (it used to be swallowed at debug, and the DSN error said only
    that nothing was found).
    """
    global _KEYRING_CB_OPEN_UNTIL

    # 1. Check Circuit Breaker
    if time.time() < _KEYRING_CB_OPEN_UNTIL:
        logger.debug("Keyring circuit breaker is OPEN. Falling back to local vault.")
        left = int(_KEYRING_CB_OPEN_UNTIL - time.time())
        return None, f"keyring: skipped, circuit breaker open for {left}s more after a timeout"

    # 2. Acquire lock to serialize keyring queries
    acquired = _KEYRING_LOCK.acquire(timeout=2.0)
    if not acquired:
        logger.warning(
            "Keyring lock contention detected; circuit breaker will open for 300s. "
            "observed: lock acquisition timed out after 2s. "
            "possible: another thread holds the keyring lock, or keyring itself is hanging. "
            "inspect: verify keyring service availability with `python -c 'import keyring; print(keyring.backends.get_keyring())'`."
        )
        _KEYRING_CB_OPEN_UNTIL = time.time() + 300.0
        return None, "keyring: lock contention (2s); circuit breaker opened for 300s"

    try:
        import keyring

        res_queue: queue.Queue[tuple] = queue.Queue()

        def _target():
            try:
                val = keyring.get_password(service_name, username)
                res_queue.put((True, val))
            except Exception as e:
                res_queue.put((False, e))

        t = threading.Thread(target=_target, daemon=True)
        t.start()

        try:
            success, result = res_queue.get(timeout=2.0)
            if success:
                return result, None
            logger.debug(f"Keyring lookup failed: {result}")
            return None, f"keyring: {type(result).__name__}: {result}"
        except queue.Empty:
            logger.warning(
                "Keyring lookup timed out; circuit breaker will open for 300s. "
                "observed: keyring thread did not return within 2s timeout. "
                "possible: keyring service is busy, unresponsive, or blocked waiting on system resources. "
                "inspect: check system keyring/credential manager for lock contention or resource issues (e.g., macOS Keychain, Windows Credential Manager, Linux secretservice/pass)."
            )
            _KEYRING_CB_OPEN_UNTIL = time.time() + 300.0
            return None, "keyring: lookup timed out (2s); circuit breaker opened for 300s"
    except Exception as e:
        log_swallowed(logger, e, f"Keyring import or setup failed: {e}")
        return None, f"keyring: unavailable ({type(e).__name__}: {e})"
    finally:
        _KEYRING_LOCK.release()


def _keyring_store(service: str, value: str) -> str | None:
    """Write one secret to the OS keyring (service "system", as the reader uses).

    Returns None on success, else what failed, for a caller that reports which
    store it used. Bounded like safe_keyring_get_password: a keyring that hangs
    (a locked keychain waiting on a prompt) must not hang an installer.
    """
    try:
        import keyring
    except ImportError as e:
        return f"keyring: not installed ({e})"
    res: queue.Queue[BaseException | None] = queue.Queue()

    def _target():
        try:
            keyring.set_password("system", service, value)
            res.put(None)
        except BaseException as e:  # noqa: BLE001 -- reported to the caller
            res.put(e)

    threading.Thread(target=_target, daemon=True).start()
    try:
        err = res.get(timeout=5.0)
    except queue.Empty:
        return "keyring: write timed out after 5s (locked keychain or unresponsive backend)"
    return None if err is None else f"keyring: {type(err).__name__}: {err}"


# Dynamically resolve DB path relative to project root.
# DB_PATH is the *default* location kept for legacy callers; vault reads and
# writes below go through _vault_db_path() so that M3_DATABASE / --database
# overrides on the surrounding CLI or MCP tool flow through here too.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "memory", "agent_memory.db")


def _vault_db_path() -> str:
    """Resolve the active DB path for vault reads/writes.

    Lazy import to avoid the auth_utils ↔ m3_sdk circular dependency at
    module-load time. If m3_sdk isn't on sys.path yet (rare — mostly during
    installer bootstraps), fall back to DB_PATH.
    """
    try:
        from m3_sdk import resolve_db_path

        return resolve_db_path(None)
    except ImportError:
        return DB_PATH


def _seam_is_being_built() -> bool:
    """True while this thread is INSIDE backend construction.

    auth_utils is called BY the backend as it is built: PostgresBackend.__init__
    -> _resolve_dsn -> _reject_same_as_warehouse -> context.get_secret ->
    get_api_key. If the vault helpers below then ask the seam for a backend or a
    dialect, that re-enters ``active_backend()`` and blocks on its
    non-reentrant ``_lock`` -- a hard deadlock, hung forever rather than raising.
    (Observed 2026-09-08: the whole suite stalled at 80% with the traceback
    selector.active_backend -> postgres_backend.__init__ -> get_secret ->
    get_api_key -> dialect -> active_backend.)

    ``active_backend()`` reads its cache BEFORE taking the lock, so the hazard
    exists only on FIRST construction. Detect that window by walking the stack
    once; it is cheap next to opening a DB, and it is honest -- the alternative
    (a module-global "in construction" flag) has to be set and cleared by the
    seam, which would push this module's problem into the seam's API.
    """
    import sys as _sys

    f: FrameType | None = _sys._getframe(1)
    while f is not None:
        g = f.f_globals.get("__name__", "")
        if g.startswith("memory.backends"):
            return True
        f = f.f_back
    return False


def _backend():
    """The active storage backend, or a minimal SQLite-only shim.

    auth_utils runs during installer bootstrap, where the seam may not be
    importable yet (it is also the module the seam's own config path imports,
    hence the lazy import in _vault_db_path above). Falling back keeps the
    bootstrap working; the shim implements only open_readonly, and only for
    SQLite, because that is the sole situation in which the seam is absent --
    a PostgreSQL install always has the seam on sys.path by the time secrets
    are read. Only ImportError falls back: a seam that refuses its
    configuration (PostgreSQL with no DSN) propagates, and the vault readers
    report it, rather than reading a local file that is not the live vault.
    """
    # Same re-entrancy hazard as _dialect_param: the backend calls get_secret()
    # while being constructed, so asking for the backend here would deadlock on
    # active_backend()'s lock. Use the SQLite shim for that window -- the vault
    # read that triggers it is resolving a DSN, i.e. the store is not yet usable.
    if not _seam_is_being_built():
        try:
            from memory.backends import active_backend

            return active_backend()
        except ImportError:  # bootstrap: seam not importable yet
            pass

    import sqlite3 as _sq
    from contextlib import contextmanager

    class _SqliteOnlyShim:
        @staticmethod
        @contextmanager
        def open_readonly(db_path: str):
            if not os.path.exists(db_path):
                raise FileNotFoundError(db_path)
            c = _sq.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            try:
                yield c
            finally:
                c.close()

    return _SqliteOnlyShim()


def _dialect_param() -> str:
    """The active backend's bind placeholder ('?' on SQLite, '%s' on PG).

    A literal '?' in feature code is a portability bug (§10a). Falls back to the
    SQLite form when the seam is unavailable (installer bootstrap) or when
    asking would re-enter backend construction -- see _seam_is_being_built.
    """
    if _seam_is_being_built():
        return "?"
    try:
        from memory.backends import dialect

        return dialect().param()
    except ImportError:  # bootstrap: seam not importable yet
        return "?"


def get_master_key() -> str | None:
    """AGENT_OS_MASTER_KEY from the secrets file or the OS keyring (the one
    chain, m3_core.secret_store; never the environment, never the vault it
    unlocks)."""
    from m3_core.secret_store import explain_named_secret

    return explain_named_secret("AGENT_OS_MASTER_KEY").value or None


class SaltMissingError(RuntimeError):
    """Raised when the device salt file is gone but the vault already holds
    encrypted secrets — regenerating a fresh salt here would silently orphan
    every existing secret (they'd never decrypt again). Fail loud instead of
    destroying them. (2026-07-03: the Homecoming root relocation lost the salt
    at the old path and a silent regen orphaned the whole vault — this guard
    exists so that can't recur.)"""


def _vault_has_secrets() -> bool:
    """True iff the vault DB exists and has at least one synchronized_secrets
    row. Used to distinguish a genuine fresh install (safe to mint a new salt)
    from an existing install whose salt went missing (must NOT regenerate).
    Any error → False (treat as 'no vault yet', i.e. let a fresh salt mint)."""
    try:
        vault_path = _vault_db_path()
        # synchronized_secrets lives in the MAIN store, so this must follow the
        # store wherever it is. `os.path.exists` + sqlite3.connect asserted a
        # local file, which on PostgreSQL is a stale-or-absent SQLite path: the
        # probe returned False and a caller then treated an EXISTING install as
        # fresh -- the exact "must NOT regenerate" case this function exists to
        # prevent. open_readonly() is backend-blind (SQLite honors db_path, PG
        # ignores it and yields a pooled connection).
        with _backend().open_readonly(vault_path) as conn:
            row = conn.execute("SELECT 1 FROM synchronized_secrets LIMIT 1").fetchone()
            return row is not None
    except Exception:  # noqa: BLE001 — missing table / locked / unreadable → treat as empty
        return False


def device_salt_path() -> str:
    """Where the device salt file is: the config root's ``.agent_os_salt``, or
    the legacy root's when only that one exists. THE owner: the salt reader and
    M3Context's salt-cohesion check both ask here (the check imported a
    ``get_salt_path`` that never existed, so it never ran)."""
    from m3_core.paths import get_m3_config_root, get_m3_legacy_root

    salt_path = os.path.join(get_m3_config_root(), ".agent_os_salt")
    if not os.path.exists(salt_path):
        legacy_path = os.path.join(get_m3_legacy_root(), ".agent_os_salt")
        if os.path.exists(legacy_path):
            return legacy_path
    return salt_path


def _get_device_salt() -> bytes:
    """
    Retrieves a persistent 16-byte salt for PBKDF2 from a local file.

    Resolution: the M3_AGENT_OS_SALT_HEX secret (32 hex chars, recovery/override) >
    config-root file > legacy-root file. If none is found, a NEW salt is
    minted ONLY when the vault has no encrypted secrets yet (a genuine fresh
    install). If the salt is missing but the vault already holds secrets,
    raise SaltMissingError rather than silently regenerating — a fresh salt
    changes the derived key and orphans every existing secret. See
    SaltMissingError for the incident that motivated this guard.
    """
    # Recovery / explicit override: lets an operator restore a lost salt
    # (e.g. from a backup) without putting the file back, or pin the salt in
    # a container: `m3 secret set M3_AGENT_OS_SALT_HEX`. The secrets chain, never
    # the environment. Must be exactly 16 bytes (32 hex chars).
    from m3_core.secret_store import explain_named_secret

    salt_hex = explain_named_secret("M3_AGENT_OS_SALT_HEX").value
    if salt_hex:
        try:
            raw = bytes.fromhex(salt_hex)
            if len(raw) == 16:
                return raw
            logger.warning(
                f"M3_AGENT_OS_SALT_HEX wrong length; ignoring and will check file fallback. "
                f"observed: hex string decoded to {len(raw)} bytes (not 16). "
                "possible: M3_AGENT_OS_SALT_HEX was truncated or copied incompletely. "
                "inspect: verify M3_AGENT_OS_SALT_HEX is exactly 32 hex characters."
            )
        except ValueError:
            logger.warning(
                "M3_AGENT_OS_SALT_HEX is not valid hex; will check file fallback. "
                "observed: the stored value could not be decoded as a 32-character hex string. "
                "possible: typo or truncation in the M3_AGENT_OS_SALT_HEX value. "
                "inspect: verify the value matches /^[0-9a-fA-F]{32}$/ (exactly 32 hex chars = 16 bytes)."
            )

    salt_path = device_salt_path()

    if os.path.exists(salt_path):
        try:
            with open(salt_path, "rb") as f:
                salt = f.read(16)
                if len(salt) == 16:
                    return salt
        except Exception:
            pass

    # No usable salt file. Minting a new one is SAFE only if nothing was ever
    # encrypted with the old one. If the vault already has secrets, a fresh
    # salt would orphan them — fail loud with an actionable message instead.
    if _vault_has_secrets():
        from m3_core.paths import resolve_backend_name, store_is_a_file

        # On a server backend the vault is a table in that database; a file path here sends the reader to a file that is not the vault.
        vault = _vault_db_path() if store_is_a_file() else f"(the {resolve_backend_name()} store)"
        raise SaltMissingError(
            f"Device salt is missing but the vault already holds encrypted secrets; refusing to mint a new salt. "
            f"observed: salt file not found at {salt_path}, no M3_AGENT_OS_SALT_HEX secret stored, and vault {vault} contains synchronized_secrets rows. "
            "possible: the salt file was deleted, the config/engine root was relocated without preserving .agent_os_salt, a backup restore was incomplete, or the file system is corrupted. "
            "inspect: (1) check if .agent_os_salt exists in the config root or legacy root (get_m3_legacy_root() / get_m3_config_root()); (2) restore from backup if available; (3) if salt is unrecoverable and secrets can be re-entered, clear synchronized_secrets with `DELETE FROM synchronized_secrets` and retry — a fresh salt will mint automatically."
        )

    # Genuine fresh install (empty/absent vault): mint a new salt.
    salt = os.urandom(16)
    # Ensure config directory exists
    os.makedirs(os.path.dirname(salt_path), exist_ok=True)
    try:
        with open(salt_path, "wb") as f:
            f.write(salt)
        # Set restrictive permissions (user read/write only).
        # Use os.name, not platform.system(): on Python 3.14 / Windows the
        # latter triggers a WMI query (platform._wmi_query) that can hang
        # indefinitely. os.name == "nt" is a constant — no WMI, no stall —
        # and chmod is a POSIX concern anyway.
        if not is_windows():
            os.chmod(salt_path, 0o600)
    except Exception as e:
        logger.warning(
            f"Could not write device salt to file; will use in-memory salt for this process only. "
            f"observed: write to {salt_path} failed with {type(e).__name__}. "
            "possible: parent directory does not exist, insufficient permissions, or file system read-only. "
            f"inspect: verify parent directory exists (`ls -d {os.path.dirname(salt_path)}`), has write permission (mode 700 preferred), and is not on a read-only mount."
        )
    return salt


_PBKDF2_ITERATIONS = 600_000
_PBKDF2_LEGACY_ITERATIONS = 100_000


def _derive_raw_key(master_key: str, iterations: int = _PBKDF2_ITERATIONS) -> bytes:
    """
    Derives a raw 32-byte key from the master key using PBKDF2-HMAC-SHA256.
    Uses a per-device salt for protection against rainbow tables.

    Routed through crypto_provider so key derivation honors the FIPS boundary
    (M3_FIPS_MODE -> wolfCrypt; fatal on any miss). Output is byte-identical to
    the prior direct PBKDF2HMAC path on the DEFAULT backend, so existing
    encrypted secrets decrypt unchanged.
    """
    from crypto_provider import provider

    salt = _get_device_salt()
    return provider.pbkdf2_sha256(master_key.encode("utf-8"), salt, iterations, 32)


def _get_fernet(master_key: str, iterations: int = _PBKDF2_ITERATIONS) -> Fernet:
    """
    Derives a Fernet encryption object from the master key (legacy decrypt path).
    Uses a per-device salt for protection against rainbow tables.

    The KDF is routed through crypto_provider (FIPS boundary); Fernet itself is
    the non-FIPS legacy cipher and is reached only for decrypting pre-migration
    secrets — never for new writes (those use AES-256-GCM). Under M3_FIPS_STRICT
    this path is refused entirely (see FIPS_MODULE_BOUNDARY.md).
    """
    from crypto_provider import provider
    from cryptography.fernet import Fernet

    salt = _get_device_salt()
    raw = provider.pbkdf2_sha256(master_key.encode("utf-8"), salt, iterations, 32)
    key = base64.urlsafe_b64encode(raw)
    return Fernet(key)


def _decrypt_token(token_str: str, master_key: str, iterations: int = _PBKDF2_ITERATIONS) -> str | None:
    """Decrypts a token string using AES-GCM or Fernet (legacy). Protected by global sync."""
    if token_str.startswith("gAAAA"):
        # Legacy Fernet (pre-91fdea1e): the stored value IS the url-safe base64
        # Fernet token. Base64-decoding it first (the AES path below) mangled it,
        # so these rows could not be read at all.
        try:
            return _get_fernet(master_key, iterations).decrypt(token_str.encode("ascii")).decode("utf-8")
        except Exception:
            return None
    try:
        raw_token = base64.b64decode(token_str)
        key = _derive_raw_key(master_key, iterations)

        with _crypto_lock:
            # Try modern AES-GCM first
            try:
                crypto.unlock_key()
                return crypto.decrypt(raw_token, key).decode("utf-8")
            except Exception:
                # Try legacy Fernet
                from cryptography.fernet import Fernet

                f_key = base64.urlsafe_b64encode(key)
                return Fernet(f_key).decrypt(raw_token).decode("utf-8")
            finally:
                crypto.lock_key()
    except Exception:
        return None


def _encrypt_value(value: str, master_key: str) -> str:
    """Encrypts a value using the modern AES-GCM provider. Protected by global sync."""
    key = _derive_raw_key(master_key)
    with _crypto_lock:
        try:
            crypto.unlock_key()
            encrypted = crypto.encrypt(value.encode("utf-8"), key)
            return base64.b64encode(encrypted).decode("utf-8")
        finally:
            crypto.lock_key()


def _sanitize_service(service: str) -> str:
    """
    Removes any characters that could be used for shell or PowerShell injection.
    Uses NFKC normalization to prevent bypass via similar-looking Unicode chars (H10).
    """
    normalized = unicodedata.normalize("NFKC", service)
    return re.sub(r"[^a-zA-Z0-9_\-]", "", normalized)


def get_api_key(service: str | None) -> str | None:
    """
    Resolves an API key by service name on every OS, through the one chain
    (m3_core.secret_store.explain_secret):
    1. The user-only secrets file's value
    2. The OS keyring entry the file points at, else system/<service>
       (python keyring; on macOS also the security CLI)
    3. Encrypted vault (synchronized_secrets), never for a bootstrap secret
    The environment is never read for a secret.

    No service (an SLM profile's api_key_service is optional) means no key, not
    an error: callers pass the profile field straight through.
    """
    if not service:
        return None
    service = _sanitize_service(service)
    # The one chain owner (the vault never for a bootstrap secret).
    from m3_core.secret_store import explain_named_secret

    return explain_named_secret(service).value or None


def _macos_keychain_read(name: str) -> str | None:
    """The macOS Keychain generic password named ``name``, via the security CLI;
    None when absent or unreadable. Its own function so the test sandbox can
    keep every test away from the real keychain (conftest)."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", name, "-w"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
            **no_window_kwargs(),
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return result.stdout.strip() or None


def _keyring_tier(key: str, *, service: str = "system") -> tuple[str | None, str | None]:
    """The OS keyring entry ``service``/``key`` (python keyring, then for the
    "system" service the macOS security CLI). Returns (value, error): the error
    is what this tier hit, for a caller that must explain a miss instead of
    reporting "not set"."""
    # Try python keyring if available.
    # Derive the OS name from sys.platform, not platform.system(): the latter
    # can hang on a WMI query on Python 3.14 / Windows. This runs on every
    # secret read, so it must not stall.
    system = os_name()
    val, error = _keyring_read(service, key)
    if val:
        return val, None
    if service != "system":  # the CLI fallbacks below know only m3's own entries
        return None, error
    service = key

    # Platform-specific native credential store fallbacks
    if system == "Darwin":
        found = _macos_keychain_read(service)
        if found:
            return found, None

    # Windows has no native fallback: the keyring module (a hard dependency) IS the
    # Credential Manager reader, and cmdkey cannot return a password. A cmdkey
    # probe here once ran on every miss and reported "exists" for ANY name,
    # because `cmdkey /list:NAME` exits 0 and echoes NAME even when nothing is
    # stored.

    elif system == "Linux":
        # Check if D-Bus Secret Service is available before silent failures
        try:
            result = subprocess.run(
                [
                    "dbus-send",
                    "--session",
                    "--dest=org.freedesktop.DBus",
                    "--type=method_call",
                    "--print-reply",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus.ListNames",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                **no_window_kwargs(),
            )
            if "org.freedesktop.secrets" not in result.stdout:
                logger.debug("Linux Secret Service (D-Bus) not running — skipping keyring")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.debug("D-Bus not available on this Linux system")

    return None, error


def _vault_tier(service: str) -> tuple[str | None, str | None]:
    """The encrypted vault (synchronized_secrets in the main store). Returns
    (value, error). Never for a bootstrap secret (CONFIG_DESIGN §10): the
    caller decides; get_api_key consults it for everything."""
    error = None
    # Fallback to the synchronized encrypted vault
    vault_path = _vault_db_path()
    # No os.path.exists gate: on PostgreSQL there is no vault FILE, and skipping
    # the lookup here means every secret silently resolves to None -- the vault
    # tier would be dead on PG while appearing merely "empty".
    conn = None
    _cm: Any = None
    try:
        _p = _dialect_param()
        _cm = _backend().open_readonly(vault_path)
        conn = _cm.__enter__()
        cur = conn.cursor()
        cur.execute(
            f"SELECT encrypted_value FROM synchronized_secrets WHERE service_name = {_p}",
            (service,),
        )
        row = cur.fetchone()

        if row and row[0]:
            master_key = get_master_key()
            if not master_key:
                logger.warning(
                    f"Encrypted secret exists for {service}, but master key is unavailable; returning None. "
                    f"observed: synchronized_secrets row found for service={service}, but AGENT_OS_MASTER_KEY is in neither the secrets file nor the keyring. "
                    "possible: AGENT_OS_MASTER_KEY was cleared from the keyring, it was only ever set in the environment (m3 no longer reads secrets there), or the keyring is inaccessible. "
                    "inspect: `m3 doctor --verbose` (the secrets checks); store it with `m3 secret set AGENT_OS_MASTER_KEY`."
                )
                return None, "vault: a row exists but AGENT_OS_MASTER_KEY is unavailable"

            # Try current iteration count first, fall back to legacy
            decrypted = _decrypt_token(row[0], master_key)
            is_legacy = row[0].startswith("gAAAA")
            needs_migration = is_legacy

            if decrypted is None:
                # Try legacy PBKDF2 iterations
                decrypted = _decrypt_token(row[0], master_key, iterations=_PBKDF2_LEGACY_ITERATIONS)
                if decrypted:
                    needs_migration = True
                else:
                    logger.debug(f"Failed to decrypt vault secret for {service}")
                    return None, "vault: the row does not decrypt with this master key"

            # Re-encrypt a legacy row (Fernet, or the old PBKDF2 iteration
            # count) in the current format, through a WRITE connection: this
            # one is read-only (the UPDATE used to run here
            # and always failed, swallowed at debug).
            if decrypted and needs_migration:
                _reencrypt_vault_row(service, decrypted, master_key)

            return decrypted, None
    except SaltMissingError as exc:
        # Loud, not swallowed at debug: the salt is gone but secrets exist,
        # so EVERY vault read is now failing. Surface it once, prominently,
        # then return None so the caller falls through (env/keyring) rather
        # than crashing the server on a secret it can't decrypt anyway.
        logger.error(
            "Vault is unreadable and will remain so until the salt is restored. "
            f"observed: SaltMissingError raised during vault access — {exc}. "
            "cause: device salt file missing or inaccessible, preventing decryption of any secret in the vault. "
            "inspect: review the SaltMissingError message above for recovery steps (restore salt file, set M3_AGENT_OS_SALT_HEX, or clear secrets and start fresh)."
        )
        error = f"vault: {type(exc).__name__}: {exc}"
    except Exception as exc:
        log_swallowed(logger, exc, f"Failed to read from encrypted vault: {type(exc).__name__}")
        error = f"vault: {type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            try:
                _cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 — already closing
                pass

    return None, error


def _reencrypt_vault_row(service: str, plaintext: str, master_key: str) -> None:
    """Rewrite one legacy vault row in the current AES-GCM / PBKDF2 format.

    Only encrypted_value changes; version and origin are left alone, since the
    secret itself did not change. A failure is LOGGED at warning with its cause
    (it was swallowed at debug, so a row that could never migrate warned on
    every read without ever saying why); the caller still gets the secret.
    """
    backend = _backend()
    still = "It is still readable in the legacy format; the next read retries."
    if not hasattr(backend, "connection"):  # installer-bootstrap shim: read-only
        logger.warning(
            f"Legacy vault row '{service}' not re-encrypted: no writable store yet (installer bootstrap). {still}"
        )
        return
    store = type(backend).__name__
    conn = None
    _wcm: Any = None
    try:
        new_encrypted = _encrypt_value(plaintext, master_key)
        _p = _dialect_param()
        _wcm = backend.connection()
        conn = _wcm.__enter__()
        cur = conn.cursor()
        cur.execute(
            f"UPDATE synchronized_secrets SET encrypted_value = {_p} WHERE service_name = {_p}",
            (new_encrypted, service),
        )
        conn.commit()
        if cur.rowcount == 1:
            logger.info(f"Re-encrypted legacy vault row '{service}' in the current format.")
        else:
            logger.warning(
                f"Legacy vault row '{service}' not re-encrypted: the UPDATE on {store} "
                f"matched {cur.rowcount} rows, expected 1 (the row was read from a "
                f"different store?). {still}"
            )
    except Exception as e:  # noqa: BLE001 -- the read already succeeded; report, do not raise
        logger.warning(
            f"Legacy vault row '{service}' could not be re-encrypted on {store}: {type(e).__name__}: {e}. {still}"
        )
    finally:
        if conn is not None:
            try:
                _wcm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 -- already closing
                pass


def set_api_key(service: str, value: str):
    """
    Encrypts and saves an API key to the synchronized_secrets vault in SQLite.
    Requires AGENT_OS_MASTER_KEY in the native keyring.
    """
    master_key = get_master_key()
    if not master_key:
        raise ValueError(
            "AGENT_OS_MASTER_KEY not found. Cannot encrypt secret. "
            "observed: no AGENT_OS_MASTER_KEY in the secrets file or the OS keyring. "
            "fix: `m3 secret set AGENT_OS_MASTER_KEY --keyring` (prompts), or m3 setup"
        )

    encrypted_value = _encrypt_value(value, master_key)
    origin_device = (
        _m3cfg.setting("identity.origin_device")  # M3_ORIGIN_DEVICE / ORIGIN_DEVICE
        or os.environ.get("COMPUTERNAME")
        or os.environ.get("HOSTNAME")
        or platform.node()
    )
    # ISO-8601 UTC timestamp
    now = datetime.now(UTC).isoformat()

    conn = None
    try:
        # WRITE path: the pooled seam connection, with the same commit/rollback
        # discipline as every other writer. No os.path.exists gate above it --
        # on PG the vault has no file and secrets would silently never persist.
        _p = _dialect_param()
        _wcm = _backend().connection()
        conn = _wcm.__enter__()
        cur = conn.cursor()

        # Get current version, increment by 1
        cur.execute(
            f"SELECT version FROM synchronized_secrets WHERE service_name = {_p}",
            (service,),
        )
        row = cur.fetchone()
        version = (row[0] + 1) if row else 1

        # ON CONFLICT ... DO UPDATE with excluded.* is spelled identically on
        # SQLite and PostgreSQL, so only the placeholders need the dialect.
        cur.execute(
            f"""
            INSERT INTO synchronized_secrets (service_name, encrypted_value, version, origin_device, updated_at)
            VALUES ({_p}, {_p}, {_p}, {_p}, {_p})
            ON CONFLICT(service_name) DO UPDATE SET
                encrypted_value = excluded.encrypted_value,
                version = excluded.version,
                origin_device = excluded.origin_device,
                updated_at = excluded.updated_at
        """,
            (service, encrypted_value, version, origin_device, now),
        )

        conn.commit()
        logger.info(f"Successfully saved encrypted {service} to the synchronized vault (version {version}).")
        from m3_core.secret_store import bump_secret_generation

        bump_secret_generation()  # running processes drop their cached copy
    finally:
        if conn is not None:
            try:
                _wcm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 — already closing
                pass
