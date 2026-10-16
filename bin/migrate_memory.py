#!/usr/bin/env python3
"""
Migration runner for the m3-memory SQLite databases.

Supports multiple migration targets:
    - main (agent_memory.db) — always present
    - chatlog — present when the configured chatlog DB path differs from main

Subcommands:
    status                    Show current version and pending migrations
    plan [--to N]             Preview DDL that pending migrations would run (no changes)
    up [--to N] [--dry-run]   Apply pending migrations (prompts for backup + confirmation)
    down --to N --target T [--dry-run]
                              Roll back ONE store to version N (requires .down.sql files)
    backup [--out PATH]       Take a standalone backup
    restore <PATH>            Restore the database from a backup file (refused while m3 runs)

Subcommands accept --target {main,chatlog,dispatch,all} to select which DB(s) to operate
on. Default is "all", except `down` (--target required, never all) and `restore` (main).

Migration file formats (both supported, sorted by numeric prefix):
    NNN_name.sql            legacy, treated as up-only
    NNN_name.up.sql         explicit up
    NNN_name.down.sql       explicit down, paired with the same NNN

The schema_versions table records applied migrations. Each up/down operation
takes a filesystem-level backup of the database (including -wal/-shm) before
running, so operations are reversible at the file level even if the in-DB
transaction already committed.
"""

import argparse
import glob
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from m3_core.paths import configured_main_db, pin_chatlog_db, pin_database  # noqa: E402
from m3_core.seams import is_windows

sys.path.insert(0, os.path.dirname(__file__))

logger = logging.getLogger("migrate_memory")

# Path resolution (new-root-with-legacy-fallback) is the single source of truth
# in m3_core.paths; these local names are thin aliases.
from m3_core.paths import get_m3_backups_root
from m3_core.paths import resolve_config_file as _resolve_config_file
from m3_core.paths import resolve_engine_file as _resolve_engine_file
from sqlite_snapshot import snapshot_sqlite

# Resolved at CALL time (_config_path / _default_db_path), not import: an
# import-time root read binds the importing process's env, before a test sandbox
# or a --config pin can re-point it. A test may still set CONFIG_PATH directly.
CONFIG_PATH: str | None = None


def _config_path() -> str:
    return CONFIG_PATH or _resolve_config_file(".migrate_config.json")


def _default_db_path() -> str:
    return configured_main_db() or _resolve_engine_file("agent_memory.db")


# Migrations inside the repository are still base-dir relative since they are static code assets
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATIONS_DIR = os.path.join(BASE_DIR, "memory", "migrations")


# ── Migration Target ────────────────────────────────────────────────────────


@dataclass(slots=True)
class MigrationTarget:
    """Represents a migration target: name, path to DB, and migrations directory."""

    name: str  # "main" or "chatlog"
    db_path: str
    migrations_dir: str


# Tables that uniquely identify the *main* schema. `agents` has been present
# since v001 of the main migrations stack and is never created by chatlog
# migrations or by memory_core lazy-init. Several other main-only tables
# (memory_history, tasks, gdpr_requests, agent_retention_policies) reinforce
# the signal; we accept any one of them as proof.
#
# The chatlog schema is a deliberate subset of main — same memory_items /
# memory_embeddings / FTS — so there is NO chatlog-only marker we can detect
# positively. Classification is "main if any of these markers exist, chatlog
# if none of them do AND the file has the storage tables". This asymmetry is
# why we cannot just symmetrically check both signatures.
_MAIN_SIGNATURE_TABLES: frozenset[str] = frozenset(
    {
        "agents",
        "memory_history",
        "tasks",
        "gdpr_requests",
        "agent_retention_policies",
        "activity_logs",
    }
)

# Tables present on any post-bootstrap chatlog DB. Used to distinguish
# "chatlog" from "unknown" when the main signatures are absent.
_CHATLOG_STORAGE_TABLES: frozenset[str] = frozenset(
    {
        "memory_items",
        "memory_embeddings",
        "memory_relationships",
    }
)

# Tables that only a DISPATCH store has. Checked BEFORE the chatlog signature
# because a dispatch store has neither main-only nor chatlog storage tables, so
# without its own signature it classifies as "unknown" -- and an unknown store
# is one the lazy initialiser will happily run MAIN migrations against,
# replacing the dispatch schema. Measured while building the split: a
# bootstrapped agent_dispatch.db came back "unknown", was migrated as a main
# store, and its notification_dispatch table vanished.
_DISPATCH_STORAGE_TABLES: frozenset[str] = frozenset(
    {
        "notification_dispatch",
    }
)


def _classify_db(db_path: str) -> str:
    """Identify a SQLite file's schema kind from its tables.

    Returns one of:
        "empty"   — file does not exist OR has no user tables (fresh DB)
        "main"    — has main-only tables (agents / memory_history / tasks)
        "chatlog" — has chatlog storage tables but no main-only tables
        "dispatch"— has the dispatch delivery table (its own ephemeral store)
        "unknown" — has tables but matches neither signature

    Path-equality alone is unreliable: a user can set CHATLOG_DB_PATH to a
    path that happens to also match M3_DATABASE, or the chatlog DB can be
    pointed at by M3_DATABASE accidentally. Looking at actual schema makes
    the runner robust to whatever path-resolution surprises arise.

    Note: a chatlog DB that has been *lazy-touched* by memory_core picks up
    memory_items + memory_embeddings, but never the main-only tables in
    _MAIN_SIGNATURE_TABLES. So presence-of-storage plus absence-of-main-
    signatures is a reliable "this is chatlog".
    """
    if not os.path.exists(db_path):
        return "empty"
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        # Locked/missing/corrupt — be conservative: callers should treat
        # "unknown" as "do not auto-migrate".
        return "unknown"
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        tables = {r[0] for r in rows}
        # Only the version table, never written: a store created and left empty
        # (seen on an install from 2026.6). Refusing it told the user to delete it.
        if tables == {"schema_versions"} and not conn.execute("SELECT 1 FROM schema_versions LIMIT 1").fetchone():
            return "empty"
    except sqlite3.Error:
        return "unknown"
    finally:
        conn.close()
    if not tables:
        return "empty"
    has_main = bool(tables & _MAIN_SIGNATURE_TABLES)
    has_storage = bool(tables & _CHATLOG_STORAGE_TABLES)
    has_dispatch = bool(tables & _DISPATCH_STORAGE_TABLES)
    if has_main:
        return "main"
    # Before the chatlog check: a dispatch store carries none of the chatlog
    # storage tables, but ordering this explicitly keeps the intent readable if
    # a future store shares one.
    if has_dispatch:
        return "dispatch"
    if has_storage:
        return "chatlog"
    return "unknown"


def _dispatch_targets(explicit: bool) -> list[MigrationTarget]:
    """The dispatch store as a target. ``explicit`` (``--target dispatch``) migrates
    it even when the file does not exist yet (the first-send bootstrap); under
    ``all`` only an EXISTING store is included, so an upgrade does not create a
    store nobody has used. A file that is not a dispatch store is refused."""
    from m3_core.paths import dispatch_migrations_dir, resolve_dispatch_db

    path = os.path.abspath(resolve_dispatch_db())
    if not explicit and not os.path.exists(path):
        return []
    kind = _classify_db(path)
    if kind not in ("dispatch", "empty"):
        logger.error(
            "Refusing to apply dispatch migrations to %s: schema fingerprint says kind=%s. "
            "inspect: M3_DISPATCH_DB / the dispatch store path",
            path,
            kind,
        )
        return []
    return [MigrationTarget(name="dispatch", db_path=path, migrations_dir=dispatch_migrations_dir())]


def targets(selected: str = "all") -> list[MigrationTarget]:
    """
    Returns a list of MigrationTarget objects based on selection.

    Args:
        selected: "main", "chatlog", or "all"

    Returns:
        List of MigrationTarget objects configured for the selection.
        If chatlog_config import fails, returns only main.

    Hardening: when the resolved "main" path actually points at a chatlog DB
    (M3_DATABASE misconfigured, or a user pointing the runner at the wrong
    file), the main target is *removed* from the result and an error is
    logged. Applying main-stack DDL to a chatlog DB is never the right call
    — schema_versions rows will be wrong, and most main migrations will
    fail with "no such column" / "no such table" errors after partial
    application leaves the chatlog DB in a half-migrated state.
    """
    # Honor M3_DATABASE for the main target so migrations land on the DB the
    # caller is targeting, not always agent_memory.db.
    try:
        from m3_sdk import resolve_db_path as _resolve_main

        main_path = os.path.abspath(_resolve_main(None))
    except ImportError:
        main_path = os.path.abspath(_default_db_path())

    main_kind = _classify_db(main_path)
    main_target_valid = main_kind in ("main", "empty")
    if not main_target_valid:
        # Defensive: refuse to attach main migrations dir to a chatlog DB.
        # When --target main is explicit, this is a hard error (return []).
        # When --target all is in effect, we drop main but still try chatlog.
        if main_kind == "chatlog":
            logger.error(
                "Refusing to apply main migrations to %s — schema fingerprint says "
                "this is a chatlog DB. Pass --database PATH naming your main agent_memory.db, "
                "or use --target chatlog.",
                main_path,
            )
        else:  # unknown
            logger.error(
                "Refusing to apply main migrations to %s — schema fingerprint is "
                "unrecognised (kind=%s). Inspect the file or restore from backup.",
                main_path,
                main_kind,
            )

    main_target = (
        MigrationTarget(name="main", db_path=main_path, migrations_dir=MIGRATIONS_DIR) if main_target_valid else None
    )

    if selected == "main":
        return [main_target] if main_target else []

    if selected == "dispatch":
        return _dispatch_targets(explicit=True)

    if selected == "all" or selected == "chatlog":
        result: list[MigrationTarget] = []
        if main_target:
            result.append(main_target)

        # Chatlog migrations only run when chatlog lives in a different file
        # than the main DB. With path-equality unification, same-file means
        # the chatlog migrations would already have been applied by the main
        # migration run.
        try:
            from chatlog_config import CHATLOG_MIGRATIONS_DIR, chatlog_db_path

            chatlog_path = os.path.abspath(chatlog_db_path())

            # Case-insensitive comparison on Windows for path equality
            is_same_file = False
            if is_windows():
                is_same_file = chatlog_path.lower() == main_path.lower()
            else:
                is_same_file = chatlog_path == main_path

            if not is_same_file:
                chatlog_kind = _classify_db(chatlog_path)
                if chatlog_kind in ("chatlog", "empty"):
                    chatlog_target = MigrationTarget(
                        name="chatlog",
                        db_path=chatlog_path,
                        migrations_dir=CHATLOG_MIGRATIONS_DIR,
                    )
                    if selected == "chatlog":
                        return [chatlog_target]
                    result.append(chatlog_target)
                else:  # main / unknown
                    logger.error(
                        "Refusing to apply chatlog migrations to %s — schema "
                        "fingerprint says kind=%s. "
                        "If this is a fresh install and no chatlog data exists, "
                        "delete the file and re-run (`rm %s`). "
                        "If it contains data you want to keep, pass --chatlog-db "
                        "naming a real chatlog DB (or fix db_path in .chatlog_config.json).",
                        chatlog_path,
                        chatlog_kind,
                        chatlog_path,
                    )
                    if selected == "chatlog":
                        return []
            elif main_kind == "chatlog" and selected in ("chatlog", "all"):
                # Recovery path: M3_DATABASE was misdirected at the chatlog DB,
                # so chatlog_db_path() also resolves to it and main was refused
                # above. Run chatlog migrations against this path so the user's
                # data still gets schema fixes instead of being stranded.
                logger.warning(
                    "Main path resolves to a chatlog DB; routing chatlog migrations "
                    "to %s. Pass --database PATH naming your main agent_memory.db to avoid "
                    "this fallback.",
                    chatlog_path,
                )
                chatlog_target = MigrationTarget(
                    name="chatlog",
                    db_path=chatlog_path,
                    migrations_dir=CHATLOG_MIGRATIONS_DIR,
                )
                if selected == "chatlog":
                    return [chatlog_target]
                result.append(chatlog_target)
            elif selected == "chatlog":
                logger.warning("Chatlog DB path equals main DB path; chatlog migrations run as part of main.")
                return []
        except ImportError:
            logger.warning("chatlog_config module not found. Operating on main DB only.")
            if selected == "chatlog":
                logger.error("Cannot use --target chatlog without chatlog_config module.")
                return []

        if selected == "all":
            result.extend(_dispatch_targets(explicit=False))
        return result

    logger.error(f"Unknown target selection: {selected}")
    return [main_target] if main_target else []


# ── Config (backup dir persistence) ─────────────────────────────────────────


def load_config():
    path = _config_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(cfg):
    # atomic_write, not open(path, "w"): that empties the file first, so a failed
    # write lost the user's remembered backup_dir.
    from m3_core.atomic_io import atomic_write

    path = _config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write(path, json.dumps(cfg, indent=2))


def prompt_backup_dir(assume_yes: bool) -> str:
    cfg = load_config()
    saved = cfg.get("backup_dir")
    if saved and os.path.isdir(saved):
        return cast("str", saved)

    default = get_m3_backups_root()

    if assume_yes:
        # Non-interactive: fall back to out-of-repo default under the user's home
        os.makedirs(default, exist_ok=True)
        cfg["backup_dir"] = default
        save_config(cfg)
        logger.info(f"Non-interactive mode: using backup dir {default}")
        return default

    # Same rule as _confirm: never block on a console nobody is watching. Here
    # the safe fallback is the DEFAULT (identical to the --yes path above), not
    # a refusal — there is nothing destructive about choosing the recommended
    # out-of-repo backup directory.
    try:
        _interactive = sys.stdin is not None and sys.stdin.isatty()
    except Exception:  # noqa: BLE001 — a stdin that cannot be probed is not a tty
        _interactive = False
    if not _interactive:
        os.makedirs(default, exist_ok=True)
        cfg["backup_dir"] = default
        save_config(cfg)
        logger.info(f"No interactive console: using backup dir {default}")
        return default

    print("\nBefore applying migrations, the database will be backed up.")
    print("Recommended: choose a directory OUTSIDE this repo to avoid any risk")
    print("of accidentally committing backup files (even though *.db is gitignored).")
    print(f"Default: {default}")
    try:
        choice = input("Backup directory [press Enter for default]: ").strip()
    except (EOFError, KeyboardInterrupt):
        choice = ""
    backup_dir = choice or default
    backup_dir = os.path.abspath(os.path.expanduser(backup_dir))
    os.makedirs(backup_dir, exist_ok=True)

    # Warn if the user picked a path inside the repo
    try:
        if os.path.commonpath([backup_dir, BASE_DIR]) == BASE_DIR:
            print(f"Note: {backup_dir} is inside the repo. *.db files are gitignored,")
            print("      so they won't be committed, but out-of-repo is still cleaner.")
    except ValueError:
        # Different drives on Windows — definitely not in-repo
        pass

    cfg["backup_dir"] = backup_dir
    save_config(cfg)
    return backup_dir


# ── Backup / restore ────────────────────────────────────────────────────────


def take_backup(
    backup_dir: str, version_before: int, tag: str, target: MigrationTarget, src_conn: sqlite3.Connection | None = None
) -> str:
    if not os.path.exists(target.db_path):
        logger.info(f"No existing database to back up: {target.db_path}")
        return ""
    # Create per-target subdirectory under backup_dir
    target_backup_dir = os.path.join(backup_dir, target.name)
    os.makedirs(target_backup_dir, exist_ok=True)
    # Microseconds and the pid: two migrations of a same-named store in one second
    # (two processes starting on a fresh store) collided on a seconds-only name,
    # and the second migration failed on "refusing to overwrite".
    now = datetime.now(UTC)
    ts = f"{now:%Y%m%dT%H%M%S}.{now.microsecond:06d}Z.p{os.getpid()}"
    db_basename = os.path.basename(target.db_path).replace(".db", "")
    basename = f"{db_basename}.v{version_before:03d}.{tag}.{ts}.db"
    dst = os.path.join(target_backup_dir, basename)
    # Verified online-backup snapshot (bin/sqlite_snapshot.py, the one owner).
    # No file-copy fallback: copying a database other connections are writing
    # can capture a torn file, and a migration must not proceed on that. A
    # failure raises here, BEFORE any migration is applied.
    counts = snapshot_sqlite(target.db_path, dst, src_conn=src_conn)
    logger.info(f"Backup written: {dst} (verified: {len(counts)} tables match)")
    return dst


_SQLITE_SIDE_FILES = ("-wal", "-shm", "-journal")


def _live_store_writers() -> list[str]:
    """'role (pid N)' for every running m3 process that holds a store. Raises when
    the probe cannot run: a restore must not proceed on "could not check"."""
    import m3_halt

    return [f"{p.role} (pid {p.pid})" for p in m3_halt.list_blocking_db_writers() if p.pid != os.getpid()]


def _move_back(moved: list[tuple[str, str]]) -> list[str]:
    """Undo a list of (src, dst) moves, newest first; returns the moves that failed."""
    failed = []
    for src, dst in reversed(moved):
        try:
            shutil.move(dst, src)
        except OSError as e:
            failed.append(f"{dst} -> {src} ({e})")
    return failed


def restore_backup(backup_path: str, target: MigrationTarget):
    if not os.path.exists(backup_path):
        logger.error(f"Backup not found: {backup_path}")
        sys.exit(1)
    # A live writer keeps its handle on the file moved aside and its WAL; restoring
    # under it splits the store. Refuse until every writer is stopped.
    try:
        writers = _live_store_writers()
    except Exception as e:  # noqa: BLE001 — reported; the restore is refused
        logger.error(
            f"Restore refused: could not check for running m3 writers ({type(e).__name__}: {e}). "
            f"store: {target.db_path} (unchanged). fix: stop m3 (`m3 stop`), then re-run the restore"
        )
        sys.exit(1)
    if writers:
        logger.error(
            f"Restore refused: {len(writers)} m3 process(es) hold the store: {', '.join(writers)}. "
            f"store: {target.db_path} (unchanged). fix: `m3 stop`, then re-run the restore"
        )
        sys.exit(1)

    # Move the current DB aside under a unique name, so a failed copy can be undone
    # and a second restore never overwrites the first one's sidecar.
    now = datetime.now(UTC)
    sidecar = f"{target.db_path}.pre-restore.{now:%Y%m%dT%H%M%S}.{now.microsecond:06d}Z.p{os.getpid()}"
    moved: list[tuple[str, str]] = []
    try:
        for suffix in ("", *_SQLITE_SIDE_FILES):
            if os.path.exists(target.db_path + suffix):
                if os.path.exists(sidecar + suffix):
                    raise FileExistsError(f"sidecar already exists: {sidecar + suffix}")
                shutil.move(target.db_path + suffix, sidecar + suffix)
                moved.append((target.db_path + suffix, sidecar + suffix))
    except OSError as e:
        failed = _move_back(moved)
        logger.error(
            f"Restore failed before copying: could not move the current store aside ({type(e).__name__}: {e}). "
            + (f"MOVE-BACK FAILED, recover by hand: {failed}" if failed else f"store: {target.db_path} (unchanged)")
        )
        sys.exit(1)
    try:
        shutil.copy2(backup_path, target.db_path)
        for suffix in _SQLITE_SIDE_FILES:
            src = backup_path + suffix
            if os.path.exists(src):
                shutil.copy2(src, target.db_path + suffix)
        verify = sqlite3.connect(target.db_path)
        try:
            result = verify.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError(f"integrity_check failed: {result}")
        finally:
            verify.close()
    except Exception as e:
        # Put the original back: drop the partial copy, then undo the moves.
        for suffix in ("", *_SQLITE_SIDE_FILES):
            try:
                if os.path.exists(target.db_path + suffix):
                    os.remove(target.db_path + suffix)
            except OSError:
                pass
        failed = _move_back(moved)
        if failed:
            logger.error(
                f"Restore failed ({type(e).__name__}: {e}) and the original store could NOT be moved back: "
                f"{failed}. The original is at {sidecar}; move it to {target.db_path} by hand"
            )
        else:
            logger.error(
                f"Restore failed ({type(e).__name__}: {e}); backup: {backup_path}. "
                f"The original store was moved back to {target.db_path} (unchanged)"
            )
        sys.exit(1)
    kept = f"; previous store kept at {sidecar}" if moved else ""
    logger.info(f"Restored {target.db_path} from {backup_path} (integrity_check ok){kept}")


# ── Migration discovery ─────────────────────────────────────────────────────

_FNAME_RE = re.compile(r"^(\d+)_(.+?)(?:\.(up|down))?\.sql$")


def discover_migrations(migrations_dir: str):
    """
    Returns a dict: { version: { 'name': str, 'up': path|None, 'down': path|None } }.
    Legacy NNN_name.sql files map to 'up' with down=None.

    Filesystems do not guarantee `glob` ordering, so we sort filenames before
    iterating — this makes version conflicts (e.g. two files with the same
    NNN prefix) deterministic, with later sort-order winning and a warning
    emitted.

    Args:
        migrations_dir: Directory to scan for migration files.
    """
    out: dict[int, dict[str, str | None]] = {}
    seen_paths: dict[tuple[int, str], str] = {}
    for filepath in sorted(glob.glob(os.path.join(migrations_dir, "*.sql"))):
        fname = os.path.basename(filepath)
        m = _FNAME_RE.match(fname)
        if not m:
            logger.warning(f"Skipping malformed migration file: {fname}")
            continue
        version = int(m.group(1))
        name = m.group(2)
        direction = m.group(3) or "up"  # 'up', 'down', or legacy (treated as 'up')
        key = (version, direction)
        if key in seen_paths:
            logger.warning(
                f"Duplicate migration for v{version:03d} {direction}: "
                f"{os.path.basename(seen_paths[key])} vs {fname} — using {fname}."
            )
        seen_paths[key] = filepath
        entry = out.setdefault(version, {"name": name, "up": None, "down": None})
        if direction == "down":
            entry["down"] = filepath
        else:
            entry["up"] = filepath
            entry["name"] = name
    return out


def init_migrations_table(conn):
    # FK enforcement matches the runtime pool settings in m3_sdk so that
    # migrations which rely on ON DELETE CASCADE (e.g. 002) behave identically
    # when run here vs. when the app is running.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_versions (
            version INTEGER PRIMARY KEY,
            filename TEXT NOT NULL,
            applied_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()


def get_applied_versions(conn):
    """Return integer-valued schema version markers from `schema_versions`.

    Skips non-numeric markers — some bench / scratch DBs store custom string
    labels in the version column. Without this coercion, `max(applied)`
    could return a string and downstream `f"{cur:03d}"` formatting would
    raise ValueError on a freshly-bootstrapped DB.
    """
    rows = conn.execute("SELECT version FROM schema_versions ORDER BY version").fetchall()
    out = []
    for (v,) in rows:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue  # skip non-numeric markers (bench DBs may use TEXT labels)
    return out


def current_version(conn) -> int:
    applied = get_applied_versions(conn)
    return max(applied) if applied else 0


def read_applied_versions(db_path: str) -> "set[int] | None":
    """The versions applied to a store, read-only: None when the file does not
    exist, an empty set when it has no schema_versions table. Never creates the
    file or the table (a read-only command must not leave a store behind)."""
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    try:
        has_table = (
            conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_versions'").fetchone()
            is not None
        )
        return set(get_applied_versions(conn)) if has_table else set()
    finally:
        conn.close()


def pending_migrations(selected: str = "all") -> "dict[str, list[int]]":
    """{target name: unapplied versions} for every configured target, read-only.

    The one place that answers "is a migration due?" — `m3 doctor --fix` asks
    this before running `up`. Raises when a migrations directory has no files
    or a store cannot be read, so a caller never mistakes "could not check"
    for "nothing pending".
    """
    out: dict[str, list[int]] = {}
    for target in targets(selected):
        migs = discover_migrations(target.migrations_dir)
        if not migs:
            raise FileNotFoundError(f"no migrations found in {target.migrations_dir}")
        applied = read_applied_versions(target.db_path) or set()
        out[target.name] = sorted(v for v in migs if v not in applied)
    return out


# ── Core apply / revert ─────────────────────────────────────────────────────


def _run_sql_transaction(conn, filepath: str):
    """Run a migration script atomically.

    sqlite3's `executescript` issues an implicit COMMIT before the script and
    does not itself run in a transaction, so a plain `BEGIN` wrapper is
    discarded. We instead wrap the script in a SAVEPOINT — SAVEPOINT semantics
    survive executescript's implicit commit and give us all-or-nothing
    behaviour. Migration files must NOT contain their own COMMIT/ROLLBACK
    statements (the repo convention; enforced by _validate_migration_sql).
    """
    with open(filepath, encoding="utf-8") as f:
        sql_script = f.read()
    _validate_migration_sql(filepath, sql_script)
    wrapped = f"SAVEPOINT mig_apply;\n{sql_script}\nRELEASE SAVEPOINT mig_apply;\n"
    try:
        conn.executescript(wrapped)
    except sqlite3.Error:
        # Best-effort rollback of the savepoint; swallow if the script never
        # got far enough to open it.
        try:
            conn.execute("ROLLBACK TO SAVEPOINT mig_apply")
        except sqlite3.Error:
            pass
        try:
            conn.execute("RELEASE SAVEPOINT mig_apply")
        except sqlite3.Error:
            pass
        raise


_ADD_COLUMN_RE = re.compile(r"(?is)^\s*(?:--[^\n]*\n\s*)*ALTER\s+TABLE\s+\S+\s+ADD\s+")
_CREATE_RE = re.compile(r"(?is)^\s*(?:--[^\n]*\n\s*)*CREATE\s+")


def _sql_statements(sql: str) -> list[str]:
    """Split a migration script into complete statements (sqlite3 decides where a
    statement ends, so trigger bodies with inner semicolons stay whole). A trailing
    fragment of only comments/whitespace is dropped."""
    out, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf)
            buf = ""
    if re.sub(r"--[^\n]*", "", buf).strip():
        out.append(buf)  # incomplete: let execute() report it
    return out


def _run_sql_tolerating_existing(conn, filepath: str) -> list[str]:
    """Apply a migration statement by statement, skipping ONLY an ``ADD COLUMN``
    whose column exists or a ``CREATE`` whose object exists, so every other
    statement still runs. Returns the skipped statements' first lines. Any other
    error rolls the whole file back and raises.

    Why: the file-level path rolls back EVERYTHING on such an error. Marking the
    version applied after that silently dropped the file's other statements (038's
    index on stores a runtime fallback had already given the column).
    """
    with open(filepath, encoding="utf-8") as f:
        sql_script = f.read()
    _validate_migration_sql(filepath, sql_script)
    skipped: list[str] = []
    conn.execute("SAVEPOINT mig_apply")
    try:
        for stmt in _sql_statements(sql_script):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                err = str(e).lower()
                if ("duplicate column name" in err and _ADD_COLUMN_RE.match(stmt)) or (
                    "already exists" in err and _CREATE_RE.match(stmt)
                ):
                    skipped.append(
                        next(ln for ln in stmt.splitlines() if ln.strip() and not ln.lstrip().startswith("--")).strip()
                    )
                    continue
                raise
        conn.execute("RELEASE SAVEPOINT mig_apply")
    except sqlite3.Error:
        conn.execute("ROLLBACK TO SAVEPOINT mig_apply")
        conn.execute("RELEASE SAVEPOINT mig_apply")
        raise
    return skipped


_FORBIDDEN_STATEMENTS_RE = re.compile(r"(?im)^\s*(COMMIT|ROLLBACK|BEGIN|END\s+TRANSACTION)\b")


def _validate_migration_sql(filepath: str, sql: str) -> None:
    """Reject migration files that issue their own COMMIT / ROLLBACK / BEGIN.

    Those statements break our SAVEPOINT-based atomicity wrapper. Strips
    `-- ...` and `/* ... */` comments before checking so commentary using
    those words doesn't trip the detector.
    """
    stripped = re.sub(r"--[^\n]*", "", sql)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
    m = _FORBIDDEN_STATEMENTS_RE.search(stripped)
    if m:
        raise ValueError(
            f"Migration {os.path.basename(filepath)} contains a top-level "
            f"{m.group(1).upper()} statement, which conflicts with the "
            f"SAVEPOINT wrapper used by migrate_memory.py. Remove it — "
            f"migrations run inside an implicit transaction."
        )


def apply_pending(conn, migrations_dir: str) -> list[int]:
    """Apply every pending migration in ``migrations_dir`` to an open connection,
    with no prompt and no backup: for a store being CREATED (the dispatch store's
    first send), where there is nothing to back up and no one to ask. The same
    discovery and apply_migration as ``up``, so it is the one runner, not another.
    Returns the versions applied."""
    init_migrations_table(conn)
    applied = set(get_applied_versions(conn))
    done: list[int] = []
    for v, entry in sorted(discover_migrations(migrations_dir).items()):
        if v in applied:
            continue
        if not entry["up"]:
            # Same rule as `up`: never apply later versions over a gap.
            raise FileNotFoundError(
                f"v{v:03d} ({entry['name']}) has no up file in {migrations_dir}; applied before it: "
                f"{done or 'none'}. fix: reinstall m3 so the migration set is complete"
            )
        apply_migration(conn, v, entry["name"], entry["up"])
        done.append(v)
    return done


def apply_migration(conn, version: int, name: str, up_path: str):
    logger.info(f"Applying migration {version:03d}: {os.path.basename(up_path)}")
    try:
        _run_sql_transaction(conn, up_path)
        conn.execute(
            "INSERT INTO schema_versions (version, filename) VALUES (?, ?)",
            (version, os.path.basename(up_path)),
        )
        conn.commit()
        logger.info(f"  -> applied v{version:03d}")
    except sqlite3.OperationalError as e:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        err = str(e).lower()
        if "duplicate column name" in err or "already exists" in err:
            # The store already has part of what this migration adds (a runtime
            # fallback, or an interrupted earlier run). Apply the rest statement by
            # statement; only the already-present ADD COLUMN / CREATE is skipped.
            skipped = _run_sql_tolerating_existing(conn, up_path)
            conn.execute(
                "INSERT OR IGNORE INTO schema_versions (version, filename) VALUES (?, ?)",
                (version, os.path.basename(up_path)),
            )
            conn.commit()
            # INFO, not WARNING: skipping an object that already exists is the
            # designed outcome (e.g. a repair migration on a store that never
            # needed it); a warning here would fire on healthy installs.
            logger.info(f"v{version:03d}: already present, skipped: {skipped}; every other statement applied.")
        else:
            logger.error(f"Failed to apply v{version:03d}: {e}")
            raise


def revert_migration(conn, version: int, down_path: str):
    logger.info(f"Reverting migration {version:03d}: {os.path.basename(down_path)}")
    try:
        _run_sql_transaction(conn, down_path)
        conn.execute("DELETE FROM schema_versions WHERE version = ?", (version,))
        conn.commit()
        logger.info(f"  -> reverted v{version:03d}")
    except sqlite3.OperationalError as e:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        logger.error(f"Failed to revert v{version:03d}: {e}")
        raise


# ── Subcommands ─────────────────────────────────────────────────────────────


def cmd_status(args):
    target_list = targets(args.target)
    if not target_list:
        logger.error(f"No targets matched: {args.target}")
        sys.exit(1)

    for target in target_list:
        if len(target_list) > 1:
            print(f"=== {target.name} ===")

        # Read-only: status must not create the store or its schema_versions table.
        found = read_applied_versions(target.db_path)
        applied = found or set()
        cur = max(applied) if applied else 0
        migs = discover_migrations(target.migrations_dir)
        all_versions = sorted(migs.keys())
        latest = max(all_versions) if all_versions else 0

        print(f"Database:         {target.db_path}{'  (does not exist yet)' if found is None else ''}")
        print(f"Current version:  {cur}")
        print(f"Latest available: {latest}")
        print(f"Status:           {'up-to-date' if cur == latest else 'behind'}")
        print()
        print("Migrations:")
        for v in all_versions:
            entry = migs[v]
            mark = "x" if v in applied else " "
            down = "yes" if entry["down"] else "no "
            up_name = os.path.basename(entry["up"]) if entry["up"] else "(missing up!)"
            print(f"  [{mark}] v{v:03d}  down={down}  {up_name}")

        pending = [v for v in all_versions if v not in applied]
        if pending:
            print()
            print(f"Pending: {pending}")

        if len(target_list) > 1:
            print()


def _confirm(prompt: str, assume_yes: bool) -> bool:
    """Ask for consent — but only where a human can actually answer.

    A bare input() blocks forever when nobody is at the console. That is not a
    hypothetical for this script: the installer runs it as a SUBPROCESS, and on
    Windows schema work can land in an elevated window opened by
    `Start-Process -Verb RunAs`. Such a window has a REAL console with nobody
    typing into it, so input() does not raise EOFError — it simply waits, and
    the user is left with an admin window that does nothing it can explain.
    (Exactly the shape that hung `m3 governor migrate`; see
    tests/test_governor_migrate_never_hangs.py.)

    Callers pass --yes for unattended runs. When they forget, refuse rather
    than hang: declining a destructive migration costs a re-run, hanging costs
    trust in a privileged process (§3 fail loud, never block).
    """
    if assume_yes:
        return True
    try:
        interactive = sys.stdin is not None and sys.stdin.isatty()
    except Exception:  # noqa: BLE001 — a stdin that cannot be probed is not a tty
        interactive = False
    if not interactive:
        print(f"{prompt} [y/N]: (no interactive console — declining; pass --yes to confirm non-interactively)")
        return False
    try:
        ans = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes")


def _print_pending_plan(pending: list[int], migs: dict, direction: str) -> None:
    """Pretty-print the SQL that would run for each pending/to-revert version."""
    print("\n--- Migration plan ---")
    for v in pending:
        entry = migs[v]
        path = entry["up"] if direction == "up" else entry["down"]
        if not path:
            print(f"\n# v{v:03d} ({direction}): MISSING")
            continue
        print(f"\n# v{v:03d} ({direction}): {os.path.basename(path)}")
        try:
            with open(path, encoding="utf-8") as f:
                print(f.read().rstrip())
        except OSError as e:
            print(f"# <error reading {path}: {e}>")
    print("\n--- end of plan ---\n")


_FAMILY = {"main": "core memories", "chatlog": "chat log", "dispatch": "agent dispatch"}


def cmd_up(args):
    target_list = targets(args.target)
    if not target_list:
        logger.error(f"No targets matched: {args.target}")
        sys.exit(1)

    backup_dir = prompt_backup_dir(args.yes)
    incomplete: list[str] = []

    for target in target_list:
        if len(target_list) > 1:
            logger.info(f"=== Processing {target.name} ===")

        os.makedirs(os.path.dirname(target.db_path), exist_ok=True)
        conn = sqlite3.connect(target.db_path)
        try:
            conn.execute("PRAGMA busy_timeout = 30000;")
            # Do NOT set locking_mode = EXCLUSIVE here. The m3 MCP memory server
            # keeps agent_memory.db open continuously in WAL mode, so an EXCLUSIVE
            # lock can never be granted — the migration fails with "database is
            # locked" regardless of busy_timeout (and db._ensure_sync_tables then
            # re-spawns it every sync, the lock-loop documented in 033). Normal WAL
            # locking + a generous busy_timeout lets online migrations coexist with
            # the live server.
        except sqlite3.Error as e:
            logger.warning(f"Could not set lock/timeout pragmas: {e}")
        try:
            init_migrations_table(conn)
            applied = set(get_applied_versions(conn))
            cur = current_version(conn)
            migs = discover_migrations(target.migrations_dir)
            all_versions = sorted(migs.keys())
            target_ver = args.to if args.to is not None else (max(all_versions) if all_versions else 0)

            pending = [v for v in all_versions if v not in applied and v <= target_ver]
            if not pending:
                logger.info(f"{target.name}: Database is up to date. No pending migrations.")
                continue

            print(f"\nCurrent version: v{cur:03d}")
            print(f"Target version:  v{target_ver:03d}")
            print("Will apply:")
            for v in pending:
                entry = migs[v]
                down = "has down" if entry["down"] else "NO down (legacy, irreversible)"
                print(f"  + v{v:03d}  {entry['name']}  [{down}]")

            if getattr(args, "dry_run", False):
                _print_pending_plan(pending, migs, "up")
                logger.info(f"{target.name}: Dry run — no changes made.")
                continue

            # `target.name` is "main" or "chatlog" — the memory FAMILY this
            # migration touches (core memories vs chatlog memories). It is NOT
            # a git branch. The actual file path was set by --database (or
            # M3_DATABASE / default) earlier in this run. Print both so the
            # user knows exactly which DB file is about to be modified.
            if not _confirm(
                f"\nApply {len(pending)} migration(s) to '{target.name}' "
                f"(family: {_FAMILY.get(target.name, target.name)}) DB at {target.db_path}?"
                if target.name == "main"
                else f"\nApply {len(pending)} migration(s) to '{target.name}' "
                f"(family: chatlog memories) DB at {target.db_path}?",
                args.yes,
            ):
                logger.info("Aborted by user.")
                continue

            take_backup(backup_dir, cur, "pre-up", target, conn)

            done: list[int] = []
            for v in pending:
                entry = migs[v]
                if not entry["up"]:
                    # Stop: applying later versions over a gap leaves a schema no
                    # chain ever produced, and "Now at vN" would hide the gap.
                    rest = pending[pending.index(v) :]
                    logger.error(
                        f"{target.name}: v{v:03d} ({entry['name']}) has only a down file, no up file, in "
                        f"{target.migrations_dir}; stopped before it. applied: {done or 'none'}; NOT applied: "
                        f"{rest} (expected through v{target_ver:03d}). fix: restore the missing "
                        f"{v:03d}_{entry['name']}.up.sql (reinstall m3), then re-run `up`"
                    )
                    incomplete.append(target.name)
                    break
                apply_migration(conn, v, entry["name"], entry["up"])
                done.append(v)
            else:
                logger.info(f"{target.name}: Done. Applied {done}; now at v{current_version(conn):03d}.")
        finally:
            conn.close()
    if incomplete:
        sys.exit(1)


def cmd_down(args):
    if args.to is None:
        logger.error("down requires --to N (the version to roll back TO)")
        sys.exit(1)
    if getattr(args, "target", None) not in ("main", "chatlog", "dispatch"):
        logger.error(
            f"down requires --target main|chatlog|dispatch (got {getattr(args, 'target', None)!r}): each store "
            "has its own version chain, so a rollback names exactly one"
        )
        sys.exit(1)

    target_list = targets(args.target)
    if not target_list:
        logger.error(f"No targets matched: {args.target}")
        sys.exit(1)

    backup_dir = prompt_backup_dir(args.yes)

    for target in target_list:
        if len(target_list) > 1:
            logger.info(f"=== Processing {target.name} ===")

        if not os.path.exists(target.db_path):
            logger.info(f"{target.name}: no store at {target.db_path}; nothing to roll back")
            continue
        conn = sqlite3.connect(target.db_path)
        try:
            conn.execute("PRAGMA busy_timeout = 30000;")
            # Do NOT set locking_mode = EXCLUSIVE here. The m3 MCP memory server
            # keeps agent_memory.db open continuously in WAL mode, so an EXCLUSIVE
            # lock can never be granted — the migration fails with "database is
            # locked" regardless of busy_timeout (and db._ensure_sync_tables then
            # re-spawns it every sync, the lock-loop documented in 033). Normal WAL
            # locking + a generous busy_timeout lets online migrations coexist with
            # the live server.
        except sqlite3.Error as e:
            logger.warning(f"Could not set lock/timeout pragmas: {e}")
        try:
            init_migrations_table(conn)
            applied = get_applied_versions(conn)
            cur = current_version(conn)
            migs = discover_migrations(target.migrations_dir)

            if args.to >= cur:
                logger.info(f"{target.name}: Already at or below v{args.to:03d} (current: v{cur:03d}). Nothing to do.")
                continue

            # Versions to revert, in reverse order
            to_revert = [v for v in reversed(applied) if v > args.to]

            # Pre-flight: every version being reverted needs a down file
            missing = [v for v in to_revert if not migs.get(v, {}).get("down")]
            if missing:
                logger.error(
                    f"{target.name}: Cannot roll back: no down migration available for version(s) {missing}. "
                    f"Legacy migrations (NNN_name.sql) are irreversible. "
                    f"Lowest reversible target above them is v{max(missing):03d}."
                )
                continue

            print(f"\nCurrent version: v{cur:03d}")
            print(f"Target version:  v{args.to:03d}")
            print("Will revert (in order):")
            for v in to_revert:
                print(f"  - v{v:03d}  {migs[v]['name']}")

            if getattr(args, "dry_run", False):
                _print_pending_plan(to_revert, migs, "down")
                logger.info(f"{target.name}: Dry run — no changes made.")
                continue

            if not _confirm(f"\nRevert {len(to_revert)} migration(s) on {target.name}?", args.yes):
                logger.info("Aborted by user.")
                continue

            take_backup(backup_dir, cur, "pre-down", target, conn)

            for v in to_revert:
                revert_migration(conn, v, migs[v]["down"])

            logger.info(f"{target.name}: Done. Now at v{current_version(conn):03d}.")
        finally:
            conn.close()


def cmd_backup(args):
    target_list = targets(args.target)
    if not target_list:
        logger.error(f"No targets matched: {args.target}")
        sys.exit(1)

    backup_dir = args.out or prompt_backup_dir(args.yes)

    for target in target_list:
        applied = read_applied_versions(target.db_path)  # read-only; None = no file
        if applied is None:
            logger.info(f"{target.name}: no store at {target.db_path}; nothing to back up")
            continue
        take_backup(backup_dir, max(applied) if applied else 0, "manual", target)


def cmd_restore(args):
    # Restore always requires explicit target specification; default to "main"
    # Warn if user picks "all"
    if args.target == "all":
        logger.warning(
            "Restore with --target all is ambiguous (which backup file for each target?). "
            "Defaulting to --target main. Use --target chatlog for the chat log DB."
        )
        target_list = targets("main")
    else:
        target_list = targets(args.target)

    if not target_list:
        logger.error(f"No targets matched: {args.target}")
        sys.exit(1)

    target = target_list[0]  # Restore operates on exactly one target
    if not _confirm(f"Restore {target.name} database from {args.path}? This will overwrite the current DB.", args.yes):
        logger.info("Aborted by user.")
        return
    restore_backup(args.path, target)


def cmd_plan(args):
    """Dry-run preview: print DDL for every pending migration without touching the DB."""
    target_list = targets(args.target)
    if not target_list:
        logger.error(f"No targets matched: {args.target}")
        sys.exit(1)

    for target in target_list:
        if len(target_list) > 1:
            print(f"=== {target.name} ===")

        applied = read_applied_versions(target.db_path) or set()  # read-only
        migs = discover_migrations(target.migrations_dir)
        all_versions = sorted(migs.keys())
        target_ver = args.to if args.to is not None else (max(all_versions) if all_versions else 0)
        pending = [v for v in all_versions if v not in applied and v <= target_ver]
        if not pending:
            logger.info(f"{target.name}: No pending migrations.")
            continue
        _print_pending_plan(pending, migs, "up")


# ── Entry point ─────────────────────────────────────────────────────────────


_TARGET_HELP = (
    "Memory FAMILY to migrate (NOT a git branch): "
    "'main' = core memories DB (default agent_memory.db, overridable via --database); "
    "'chatlog' = chatlog memories DB (default agent_chatlog.db, overridable via --chatlog-db "
    "or db_path in .chatlog_config.json); "
    "'dispatch' = the agent dispatch store; "
    "'all' = main, chatlog and an existing dispatch store. Default: all configured."
)


def build_parser():
    p = argparse.ArgumentParser(description="m3-memory database migration runner")
    # Top-level --database pins the main database for the run so targets()
    # picks up the override. Subcommand --target still selects which
    # DB family (main / chatlog) to touch.
    p.add_argument(
        "--database",
        default=None,
        metavar="PATH",
        help="Override the file path for the 'main' (core memories) target. "
        "Pins it for this run; targets() resolves the main DB from that "
        "pin. The 'chatlog' target is unaffected (see --chatlog-db). "
        "Default main DB: agent_memory.db under the engine root.",
    )
    p.add_argument(
        "--chatlog-db",
        default=None,
        metavar="PATH",
        help="Override the file path for the 'chatlog' target for this run. "
        "Default: db_path in .chatlog_config.json, else agent_chatlog.db under the engine root.",
    )
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("status", help="Show current version and pending migrations")
    sp.add_argument(
        "--target",
        choices=["main", "chatlog", "dispatch", "all"],
        default="all",
        help=_TARGET_HELP,
    )
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("up", help="Apply pending migrations")
    sp.add_argument("--to", type=int, default=None, help="Apply up to this version (default: latest)")
    sp.add_argument(
        "--target",
        choices=["main", "chatlog", "dispatch", "all"],
        default="all",
        help=_TARGET_HELP,
    )
    sp.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts")
    sp.add_argument("--dry-run", action="store_true", help="Print the plan + DDL without applying anything")
    sp.set_defaults(func=cmd_up)

    sp = sub.add_parser("down", help="Roll back migrations")
    sp.add_argument("--to", type=int, required=True, help="Roll back to this version")
    # Required, and never "all": each store has its own version chain, so one
    # --to N reverting every store at once reverts stores nobody named.
    sp.add_argument(
        "--target",
        choices=["main", "chatlog", "dispatch"],
        required=True,
        help="The ONE store to roll back (required; no 'all': each store has its own version numbers). "
        "'main' = core memories DB, 'chatlog' = chatlog memories DB, 'dispatch' = the agent dispatch store.",
    )
    sp.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts")
    sp.add_argument("--dry-run", action="store_true", help="Print the plan + DDL without reverting anything")
    sp.set_defaults(func=cmd_down)

    sp = sub.add_parser("plan", help="Preview DDL that pending migrations would run (no changes)")
    sp.add_argument("--to", type=int, default=None, help="Plan up to this version (default: latest)")
    sp.add_argument(
        "--target",
        choices=["main", "chatlog", "dispatch", "all"],
        default="all",
        help=_TARGET_HELP,
    )
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("backup", help="Take a standalone backup of the database")
    sp.add_argument("--out", type=str, default=None, help="Backup directory (overrides saved default)")
    sp.add_argument(
        "--target",
        choices=["main", "chatlog", "dispatch", "all"],
        default="all",
        help=_TARGET_HELP,
    )
    sp.add_argument("-y", "--yes", action="store_true", help="Skip interactive prompts")
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("restore", help="Restore the database from a backup file")
    sp.add_argument("path", help="Path to the backup .db file")
    sp.add_argument(
        "--target",
        choices=["main", "chatlog", "dispatch", "all"],
        default="main",
        help="Which DB target to restore (default: main; use chatlog for chat log DB)",
    )
    sp.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")
    sp.set_defaults(func=cmd_restore)

    return p


def main():
    # Configured here, not at import: other m3 code imports this module, and an
    # import-time basicConfig turned on INFO logging for the whole process
    # (every `m3 memory` command printed each HTTP request).
    logging.basicConfig(level=logging.INFO, format="%(name)s: [%(levelname)s] %(message)s")
    from m3_core.paths import require_sqlite_backend

    require_sqlite_backend("migrate_memory")  # opens SQLite files directly; refuse on PostgreSQL
    if not os.path.exists(MIGRATIONS_DIR):
        logger.error(f"Migrations directory not found: {MIGRATIONS_DIR}")
        sys.exit(1)

    parser = build_parser()
    args = parser.parse_args()

    if args.database:
        pin_database(args.database)
    if args.chatlog_db:
        pin_chatlog_db(args.chatlog_db)

    # Back-compat: `python migrate_memory.py` with no args == `up` (the old behavior),
    # but now with the new prompts. Scripts that relied on the old no-arg invocation
    # will still work, just interactively.
    if args.command is None:
        args = parser.parse_args(["up"])

    args.func(args)


if __name__ == "__main__":
    main()
