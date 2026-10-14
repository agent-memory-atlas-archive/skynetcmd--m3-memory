#!/usr/bin/env python3
"""Config parity snapshot: what m3 resolves today, so the config migration can prove it changed nothing.

Before the first setting moves behind the new resolver, capture every setting
as today's code sees it; after each migration step, ``check`` recaptures and
diffs. Only entries on the user-reviewed intended-change list may differ.

Four layers per snapshot:
  env     every constant env name m3 reads (the inventory of
          ``check_env_access``), BEFORE and AFTER M3Context loads ``.env``
  parsed  the constants m3's config modules compute from the env at import:
          the per-key legacy parser, whatever today's code makes of the string
  files   each JSON config file under the config root, flattened
  store   ``active_backend().store_identity()`` (credential-free; never a path)

SECRETS never reach the file. A secret-named key, or any value carrying URL
credentials, is recorded as HMAC-SHA256(value) under a random per-machine key
kept in the OS keyring and never exported, so the snapshot still catches a
corrupted DSN or token, but only on the machine that took it (#P10; a keyed
HMAC, not the unsalted hash CWE-759 warns about). On a machine with NO keyring
backend (a desktop-less Linux box) the key is a user-only file under the
config root instead; a keyring that exists but is
locked is an error, never a fallback.

FAIL LOUD (DESIGN_PHILOSOPHIES §3): ``check`` fails when the baseline or the
HMAC key is missing. It never writes a baseline it did not find; that
"seed and skip" is the defect of the older local parity test.

Usage:
    python bin/config_parity.py init-key                  # once per machine; never overwrites
    python bin/config_parity.py capture --context shell   # writes <config root>/parity/<context>.json
    python bin/config_parity.py check --context shell [--intended FILE]

Exit: 0 = ok / no drift; 1 = drift; 2 = cannot check (missing key or baseline).
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import os
import re
import secrets
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

from m3_core.seams import current_uid

_BIN = Path(__file__).resolve().parent
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

KEYRING_SERVICE = "m3-config-parity"
KEYRING_USER = "hmac-key-v1"
SCHEMA = 1

# Library modules whose import-time constants are captured. Importing a SCRIPT
# could run its side effects, so scripts are listed in the snapshot as
# uncaptured instead (measured 2026-10-08: 38 files read the env at import).
PARSED_MODULES = (
    "memory.config",
    "memory.embed",
    "memory.tokens",
    "memory.chunking",
    "files_memory.config",
    "files_memory.entity_coalesce",
    "files_memory.dedup",
    "files_memory.promotability",
    "files_memory.summarize",
    "m3_core.runtime",
    "m3_core.gpu",
    "m3_core.context",
    "m3_core.governor",
    "chatlog_config",
    "chatlog_redaction",
    "auto_route",
    "llm_failover",
    "crypto_provider",
    "sqlite_pragmas",
)

# The config files m3 folds into m3_config.toml. Other JSON files in the config root are
# runtime state (heartbeats, pass stamps, watchdog state) or another app's, and
# would show as drift every few minutes; they are listed by NAME only, for the
# classification, never compared.
CONFIG_FILES = (
    ".llm_config.json",
    ".cognitive_loop_config.json",
    ".governor_config.json",
    ".log_config.json",
    ".spill_config.json",
    ".embed_config.json",
    ".chatlog_config.json",
    ".migrate_config.json",
    ".tools_config.json",
    ".wiki_admission.json",
    ".wiki_authority.json",
    ".wiki_pii.json",
    ".wiki_self_entities.json",
)

_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSW|SALT|DSN|CREDENTIAL|MASTER|_PG_URL|^PG_URL)", re.IGNORECASE)
_URL_CREDENTIALS = re.compile(r"://[^/@\s:]+:[^/@\s]+@")


class ParityError(Exception):
    """The check cannot run (missing key or baseline): exit 2, never a pass."""


# ── the per-machine HMAC key ──────────────────────────────────────────────────
def _keyring():
    import keyring

    return keyring


def _key_file() -> Path:
    from m3_core.paths import get_m3_config_root

    return Path(get_m3_config_root()) / "parity" / ".hmac_key"


def _keyring_get() -> tuple[bool, str | None]:
    """(keyring available, stored value). ONLY 'no keyring backend at all'
    (keyring's fail.Keyring backend) counts as unavailable. A keyring that
    exists but refuses (a locked macOS login keychain over SSH) is an error, not
    a reason to fall back: falling back would give one machine two keys."""
    import keyring.errors

    try:
        return True, _keyring().get_password(KEYRING_SERVICE, KEYRING_USER)
    except keyring.errors.NoKeyringError:
        return False, None
    except keyring.errors.KeyringError as e:
        raise ParityError(
            f"the keyring exists but refused access ({type(e).__name__}: {e}); on macOS over "
            f"SSH the login keychain is locked, so run this in a logged-in session"
        ) from e


def _read_key_file(p: Path) -> str | None:
    if not os.path.isfile(p):
        return None
    uid = current_uid()
    if uid is not None:  # POSIX ownership; Windows protects the key with ACLs
        mode = p.stat().st_mode & 0o777
        if mode & 0o077 or p.stat().st_uid != uid:
            raise ParityError(
                f"{p} is mode {oct(mode)} (or not yours); the key must be user-only. Fix with: chmod 600 {p}"
            )
    return p.read_text(encoding="utf-8").strip()


def get_key() -> bytes:
    available, raw = _keyring_get()
    if not available:
        raw = _read_key_file(_key_file())
    if not raw:
        where = f"the keyring ({KEYRING_SERVICE}/{KEYRING_USER})" if available else str(_key_file())
        raise ParityError(
            f"no parity HMAC key in {where}. Create it once on this machine with "
            f"`config_parity.py init-key`; a snapshot taken with a different key cannot be compared."
        )
    return bytes.fromhex(raw)


def init_key() -> str | None:
    """Create the key if absent; return where it was stored ('keyring' or the
    file path), or None when a key already exists. Never overwrites."""
    available, existing = _keyring_get()
    if available:
        if existing:
            return None
        import keyring.errors

        try:
            _keyring().set_password(KEYRING_SERVICE, KEYRING_USER, secrets.token_hex(32))
        except keyring.errors.KeyringError as e:
            # A locked macOS keychain over SSH READS fine (None for a missing
            # item) and refuses only the write: -25308 errSecInteractionNotAllowed.
            raise ParityError(
                f"the keyring refused to store the key ({type(e).__name__}: {e}); on macOS "
                f"over SSH the login keychain is locked, so run init-key in a logged-in "
                f"session (a LaunchAgent)"
            ) from e
        return "keyring"
    p = _key_file()
    if _read_key_file(p):
        return None
    from m3_core.atomic_io import atomic_write  # a new file is created 0600 (user-only)

    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(p, secrets.token_hex(32) + "\n")
    return str(p)


# ── value recording ───────────────────────────────────────────────────────────
def _is_secret(name: str, value: object) -> bool:
    return bool(_SECRET_NAME.search(name)) or (isinstance(value, str) and bool(_URL_CREDENTIALS.search(value)))


def _machine_markers() -> list[str]:
    marks = [str(Path.home()), socket.gethostname()]
    for var in ("USERNAME", "USER"):
        if os.environ.get(var):  # env-owner: the parity snapshot records env on purpose
            marks.append(os.environ[var])  # env-owner: the parity snapshot records env on purpose
    return [m for m in marks if m and len(m) > 2]


def _plain(value: object) -> object:
    """JSON-safe, deterministic form of a constant."""
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (set, frozenset)):
        # by JSON form: deterministic, and a set mixing types cannot be sorted directly
        return sorted((_plain(v) for v in value), key=lambda x: json.dumps(x, sort_keys=True))
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return f"<{type(value).__name__}>"


def record(name: str, value: object, key: bytes, markers: list[str]) -> dict:
    """One entry. A secret becomes its HMAC; nothing else of it is kept."""
    if value is None:
        return {"set": False}
    if _is_secret(name, value):
        from crypto_provider import get_hmac_sha256  # the FIPS boundary: a keyed digest of a secret

        digest = get_hmac_sha256(key, json.dumps(_plain(value), sort_keys=True).encode("utf-8"))
        return {"set": True, "secret": True, "hmac": digest}
    plain = _plain(value)
    text = json.dumps(plain, sort_keys=True)
    return {"set": True, "value": plain, "machine_derived": any(m.lower() in text.lower() for m in markers)}


# ── capture ───────────────────────────────────────────────────────────────────
def _env_names() -> tuple[list[str], int]:
    """Constant env names m3 reads: the inventory's direct reads, plus every
    name a config-registry key answers to, plus the count of dynamic-name sites a
    snapshot cannot enumerate.

    The registry half matters more as readers convert: a reader converted from
    ``os.environ.get("M3_X")`` to the registry no longer appears in the direct-read
    inventory, so without it every conversion silently shrank parity's coverage
    (a converted M3_TOOL_TIMEOUT stopped registering as drift, 2026-10-11)."""
    import check_env_access as cea
    from m3_core.config import REGISTRY

    findings, unparsed = cea.scan_repo()
    if unparsed:
        raise ParityError(f"inventory incomplete, files not parsed: {unparsed}")
    names = {f.name for f in findings if f.kind == "read" and f.name not in ("<dynamic>", "*")}
    names |= set(REGISTRY.env_names())
    dynamic = sum(1 for f in findings if f.kind == "read" and f.name == "<dynamic>")
    return sorted(names), dynamic


def _env_derived_constants(module) -> list[str]:
    """Uppercase module-level names whose definition reads the environment."""
    import check_env_access as cea

    path = getattr(module, "__file__", None)
    if not path:
        return []
    src = Path(path).read_text(encoding="utf-8")
    read_lines = {f.line for f in cea.scan_source(src, path) if f.kind == "read"}
    out = []
    for stmt in ast.parse(src).body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            span = range(stmt.lineno, (stmt.end_lineno or stmt.lineno) + 1)
            if any(line in read_lines for line in span):
                out += [t.id for t in targets if isinstance(t, ast.Name) and t.id.isupper()]
    return sorted(set(out))


def _flatten(prefix: str, value: object, out: dict) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}.{k}" if prefix else str(k), v, out)
    else:
        out[prefix] = value


def capture(context: str) -> dict:
    key = get_key()  # first: no key, no snapshot
    from crypto_provider import get_sha256  # the key fingerprint goes through the FIPS boundary

    # A module imported before this point computed its constants from the env
    # as it was THEN (before .env was loaded here); say which ones.
    imported_before = sorted(m for m in PARSED_MODULES if m in sys.modules)
    markers = _machine_markers()
    names, dynamic_sites = _env_names()

    pre = {n: os.environ.get(n) for n in names}  # env-owner: the parity snapshot records env on purpose
    from m3_core.context import M3Context  # loads .env into os.environ

    M3Context.for_db(None)
    post = {n: os.environ.get(n) for n in names}  # env-owner: the parity snapshot records env on purpose
    env = {n: {"pre": record(n, pre[n], key, markers), "post": record(n, post[n], key, markers)} for n in names}

    parsed: dict[str, dict] = {}
    import_errors: dict[str, str] = {}
    for mod_name in PARSED_MODULES:
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:  # recorded, never silently skipped
            import_errors[mod_name] = f"{type(e).__name__}: {e}"
            continue
        for const in _env_derived_constants(mod):
            parsed[f"{mod_name}.{const}"] = record(const, getattr(mod, const, None), key, markers)

    from m3_core.paths import get_m3_config_root

    files: dict[str, dict] = {}
    root = Path(get_m3_config_root())
    present = {p.name for p in list(root.glob("*.json")) + list(root.glob(".*.json"))}
    for p in (root / name for name in CONFIG_FILES if name in present):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            files[p.name] = {"_error": f"{type(e).__name__}: {e}"}
            continue
        flat: dict[str, object] = {}
        _flatten("", data, flat)
        files[p.name] = {k: record(k, v, key, markers) for k, v in sorted(flat.items())}

    from memory.backends import active_backend

    return {
        "schema": SCHEMA,
        "context": context,
        "host": socket.gethostname(),
        "python": sys.version.split()[0],
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "key_fingerprint": get_sha256(b"m3-parity-fp" + key)[:16],
        "env": env,
        "parsed": parsed,
        "files": files,
        "store": {"main": active_backend().store_identity()},
        "coverage": {
            "env_names": len(names),
            "dynamic_name_sites_not_enumerable": dynamic_sites,
            "parsed_modules": list(PARSED_MODULES),
            "parsed_import_errors": import_errors,
            "parsed_modules_imported_before_dotenv": imported_before,
            "json_files_not_in_config_inventory": sorted(present - set(CONFIG_FILES)),
        },
    }


# ── compare ───────────────────────────────────────────────────────────────────
def diff(baseline: dict, current: dict, intended: set[str]) -> list[str]:
    if baseline.get("key_fingerprint") != current.get("key_fingerprint"):
        raise ParityError(
            "the baseline was taken with a different HMAC key (another machine, or the key "
            "was recreated); secrets cannot be compared. Recapture the baseline on this machine."
        )
    out = []
    for layer in ("env", "parsed", "files", "store"):
        b, c = baseline.get(layer, {}), current.get(layer, {})
        for k in sorted(set(b) | set(c)):
            path = f"{layer}:{k}"
            if path in intended:
                continue
            if b.get(k) != c.get(k):
                out.append(f"{path}: {json.dumps(b.get(k), sort_keys=True)} -> {json.dumps(c.get(k), sort_keys=True)}")
    return out


def _default_path(context: str) -> Path:
    from m3_core.paths import get_m3_config_root

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", context):
        raise ParityError(f"context {context!r}: use letters, digits, '.', '_' or '-'")
    return Path(get_m3_config_root()) / "parity" / f"{context}.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-key", help="create this machine's parity HMAC key (never overwrites)")
    for name in ("capture", "check"):
        sp = sub.add_parser(name)
        sp.add_argument("--context", required=True, help="process context label, e.g. shell, scheduled-task")
        sp.add_argument("--path", help="snapshot file (default: <config root>/parity/<context>.json)")
        if name == "check":
            sp.add_argument("--intended", help="JSON list of 'layer:key' entries allowed to differ")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "init-key":
            where = init_key()
            print(f"created in {where}" if where else "a key already exists; left unchanged")
            return 0
        path = Path(args.path) if args.path else _default_path(args.context)
        if args.cmd == "capture":
            from m3_core.atomic_io import atomic_write

            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, json.dumps(capture(args.context), indent=2, sort_keys=True) + "\n")
            print(f"captured {path}")
            return 0
        if not os.path.isfile(path):
            raise ParityError(f"no baseline at {path}; capture one first. A missing baseline is not a pass.")
        baseline = json.loads(path.read_text(encoding="utf-8"))
        intended = set(json.loads(Path(args.intended).read_text(encoding="utf-8"))) if args.intended else set()
        drift = diff(baseline, capture(args.context), intended)
        for d in drift:
            print(d)
        print(f"{len(drift)} difference(s) outside the intended-change list" if drift else "parity: no drift")
        return 1 if drift else 0
    except ParityError as e:
        print(f"config_parity: cannot check: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
