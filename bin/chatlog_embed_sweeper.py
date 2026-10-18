#!/usr/bin/env python3
"""
chatlog_embed_sweeper.py — lazy embed chat log rows missing embeddings.

Runs on a schedule (default every 30 min via install_schedules.py). Picks up
rows written with embed=False, embeds in batches using memory_core._embed_many,
and drains any spill-to-disk files from the async write queue.
"""

from __future__ import annotations

from m3_core.paths import apply_early_argv as _apply_early_argv
from m3_core.paths import pin_database  # noqa: E402

# --config FILE (the file this job was registered with) is pinned, and every
# --set KEY=VALUE applied, before any import below binds a root or reads a
# setting at import time (chatlog_config, log dirs).
_apply_early_argv()

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from datetime import UTC, datetime
from glob import glob

# Setup path so we can import bin/ modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chatlog_config
from m3_core.config import settings as _m3cfg
from m3_core.runtime import utc_iso

logger = logging.getLogger("chatlog_embed_sweeper")

# --- undrainable-spill quarantine (added 2026-08-29) -------------------------
# A spill file whose target can never exist (a deleted pytest tmpdir, a %TEMP%
# scratch DB) is refused on every pass — correctly, since discarding a turn is
# worse than keeping it (§3). But with no escape hatch the sweeper retries it
# forever: measured at 312,684 passes / ~1,230 MB/s / a 720 MB log, which
# stalled the desktop UI. Count consecutive failures per file and, past the
# budget, move it aside. MOVE, never delete — the turns must survive (§3).
_QUARANTINE_DIRNAME = chatlog_config.QUARANTINE_DIRNAME
_SPILL_CFG_TTL = 5.0
_spill_cfg_cache: dict = {"ts": 0.0, "mtime": None, "max_attempts": None}


def _spill_config_path() -> str:
    from m3_core.paths import get_m3_config_root

    return os.path.join(get_m3_config_root(), ".spill_config.json")


def _spill_max_attempts(now: float | None = None) -> int:
    """Consecutive-failure budget before a spill file is quarantined.

    Precedence: config file > env var > default (§3). A config FILE, not an env
    var alone: the Windows scheduled task / launchd agent / systemd --user unit
    that runs the sweeper inherits a bare environment, so an env-only knob is
    absent in exactly the process that needs it. Re-read only when the file's
    mtime changes, stat throttled to _SPILL_CFG_TTL (§4). Never raises.
    """
    now = now if now is not None else time.time()
    if now - _spill_cfg_cache["ts"] >= _SPILL_CFG_TTL:
        _spill_cfg_cache["ts"] = now
        try:
            mtime = os.stat(_spill_config_path()).st_mtime
        except OSError:
            mtime = None  # absent/unreadable -> env + default
        if mtime != _spill_cfg_cache["mtime"]:
            _spill_cfg_cache["mtime"] = mtime
            cfg: dict = {}
            if mtime is not None:
                try:
                    with open(_spill_config_path(), encoding="utf-8") as fh:
                        cfg = json.load(fh) or {}
                except Exception as e:  # noqa: BLE001 — any parse failure
                    # §3 never silent: a malformed file would otherwise revert to
                    # defaults invisibly, so the operator's tuning is not live.
                    logger.warning(
                        "Spill config %s is unreadable/malformed (%s) — falling back to env var + default until fixed.",
                        _spill_config_path(),
                        e,
                    )
                    cfg = {}
            _spill_cfg_cache["max_attempts"] = cfg.get("max_drain_attempts")

    val = _spill_cfg_cache["max_attempts"]
    if val is not None:
        try:
            return max(1, int(val))
        except (TypeError, ValueError):
            pass
    try:
        # os.environ.get, NOT getenv_compat: that helper maps a DEPRECATED
        # legacy name to a new one, and this knob is new — it has no legacy
        # spelling to be compatible with (test_env_rename_map_drift enforces
        # that every getenv_compat call site is a real rename).
        return max(1, _m3cfg.setting("chatlog.spill.max_drain_attempts"))
    except (TypeError, ValueError):
        return 5


def _attempts_path(spill_path: str) -> str:
    return spill_path + ".attempts"


def _read_attempts_doc(spill_path: str) -> dict:
    """The attempts sidecar for this spill file ({} if none/unreadable)."""
    try:
        with open(_attempts_path(spill_path), encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except Exception:  # noqa: BLE001 — absent or corrupt both mean "start over"
        return {}


def _read_attempts(spill_path: str) -> int:
    """Consecutive failures recorded for this spill file (0 if none/unreadable)."""
    try:
        return int(_read_attempts_doc(spill_path).get("consecutive_failures", 0))
    except (TypeError, ValueError):
        return 0


def _record_attempt(spill_path: str, ok: bool, reason: str = "", *, counted: bool = True) -> int:
    """Bump (or clear) the consecutive-failure count. Returns the new count.

    ``counted=False`` records a TEMPORARY failure (the store was busy or
    unreachable, ``Dialect.is_transient_error``): the error and when temporary
    failures began are kept, but the count toward quarantine does not move, so
    an outage longer than the retry budget does not park healthy turns."""
    if ok:
        try:
            os.remove(_attempts_path(spill_path))
        except OSError:
            pass
        return 0
    prior = _read_attempts_doc(spill_path)
    n = _read_attempts(spill_path) + (1 if counted else 0)
    doc = {"consecutive_failures": n, "last_error": reason[:500], "last_attempt": utc_iso()}
    if not counted:
        doc["transient_since"] = prior.get("transient_since") or doc["last_attempt"]
    try:
        with open(_attempts_path(spill_path), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
    except OSError as e:
        # Best-effort: a counter we cannot persist just means we retry longer.
        logger.warning("Could not record drain attempt for %s: %s", spill_path, e)
    return n


def _quarantine_spill(spill_path: str, reason: str, attempts: int) -> bool:
    """Move an undrainable spill file out of the hot path. True if moved.

    The turns are NOT lost (§3): they sit in <spill_dir>/quarantine/ and drain
    again if moved back once the target store exists. The reason is kept in a
    ``<file>.reason.json`` sidecar so status can say why without the log.
    """
    try:
        qdir = os.path.join(os.path.dirname(spill_path), _QUARANTINE_DIRNAME)
        os.makedirs(qdir, exist_ok=True)
        dest = os.path.join(qdir, os.path.basename(spill_path))
        if os.path.exists(dest):  # keep both rather than clobber
            stem, ext = os.path.splitext(dest)
            dest = f"{stem}.{datetime.now(UTC):%Y%m%dT%H%M%S}{ext}"
        os.replace(spill_path, dest)
        try:
            os.remove(_attempts_path(spill_path))
        except OSError:
            pass
        try:
            from m3_core.atomic_io import atomic_write

            atomic_write(
                dest + chatlog_config.QUARANTINE_REASON_SUFFIX,
                json.dumps(
                    {"source": spill_path, "reason": reason[:500], "attempts": attempts, "quarantined_at": utc_iso()}
                ),
            )
        except OSError as e:
            logger.warning("Could not write the quarantine reason for %s: %s", dest, e)
        logger.error(
            "QUARANTINED undrainable spill %s -> %s after %d consecutive failures "
            "(%s). Turns are PRESERVED, not deleted — move the file back into %s once "
            "the cause is fixed; the next drain pass retries it. Raise "
            "max_drain_attempts in %s to retry longer.",
            spill_path,
            dest,
            attempts,
            reason,
            os.path.dirname(spill_path),
            _spill_config_path(),
        )
        return True
    except OSError as e:
        logger.warning("Could not quarantine %s: %s", spill_path, e)
        return False


def _store_dialect():
    """The active backend's dialect, which classifies a failed write
    (``Dialect.is_transient_error``)."""
    from memory.backends import dialect

    return dialect()


# How often quarantined spill files get another try. Quarantine is for files
# that failed max_drain_attempts times in a row; once a day each gets ONE more
# attempt, so turns parked by a cause since fixed (a store migrated, a lock
# released) reach the store without anyone moving files.
QUARANTINE_RETRY_S = 24 * 3600
_RETRY_STAMP = ".last_retry"


def _retry_quarantined(spill_dir: str, max_attempts: int, now: float | None = None) -> int:
    """Move quarantined spill files back for one drain attempt, at most once per
    QUARANTINE_RETRY_S. Returns how many were moved back.

    Only whole files quarantined by ``_quarantine_spill`` (those with a reason
    sidecar) return: malformed lines (``.bad``) never parse, and foreign lines
    were parked on purpose. Each returns with ``max_attempts - 1`` failures
    recorded, so one failure re-quarantines it with a fresh reason and a
    success removes it. Never raises: the drain proceeds either way."""
    qdir = os.path.join(spill_dir, _QUARANTINE_DIRNAME)
    if not os.path.isdir(qdir):
        return 0
    now = time.time() if now is None else now
    stamp = os.path.join(qdir, _RETRY_STAMP)
    try:
        if now - os.stat(stamp).st_mtime < QUARANTINE_RETRY_S:
            return 0
    except OSError:
        pass  # never retried: due now
    moved = 0
    suffix = chatlog_config.QUARANTINE_REASON_SUFFIX
    try:
        for reason in [f for f in os.listdir(qdir) if f.endswith(suffix)]:
            name = reason[: -len(suffix)]
            src = os.path.join(qdir, name)
            dest = os.path.join(spill_dir, name)
            if not os.path.isfile(src) or os.path.exists(dest):
                continue
            os.replace(src, dest)
            with open(_attempts_path(dest), "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "consecutive_failures": max(0, max_attempts - 1),
                        "last_error": "daily retry from quarantine",
                        "last_attempt": utc_iso(),
                    },
                    fh,
                )
            try:
                os.remove(os.path.join(qdir, reason))
            except OSError:
                pass
            moved += 1
        with open(stamp, "w", encoding="utf-8") as fh:
            fh.write(utc_iso())
    except OSError as e:
        logger.warning("Daily retry of quarantined spill files in %s did not complete: %s", qdir, e)
    if moved:
        logger.warning("Retrying %d quarantined spill file(s) from %s (daily retry)", moved, qdir)
    return moved


def _claim_spill_file(spill_path: str) -> str | None:
    """Rename a live day file to a draining name the writer never appends to.

    The writer appends to ``YYYYMMDD.jsonl`` under the spill lock; renaming under
    the same lock means every line in the claimed file was written before the
    claim, so reading then deleting it cannot drop a late append. A file that is
    already a draining file (a previous pass failed or died) is returned as is.
    None when the file cannot be claimed this pass (gone, or held open).
    """
    base = os.path.basename(spill_path)
    if chatlog_config.DRAINING_MARKER in base:
        return spill_path
    stem = base[: -len(".jsonl")]
    tag = f"{os.getpid()}-{datetime.now(UTC):%Y%m%dT%H%M%S%f}"
    dest = os.path.join(os.path.dirname(spill_path), f"{stem}{chatlog_config.DRAINING_MARKER}{tag}.jsonl")
    try:
        with chatlog_config.locked(chatlog_config.spill_lock_path(), purpose="chatlog spill"):
            os.replace(spill_path, dest)
    except FileNotFoundError:
        return None  # another drainer claimed it first
    except OSError as e:
        logger.warning("Could not claim spill file %s for draining (%s); retrying next pass", spill_path, e)
        return None
    if os.path.exists(_attempts_path(spill_path)):
        try:
            os.replace(_attempts_path(spill_path), _attempts_path(dest))
        except OSError:
            pass
    return dest


def _is_under(path: str, root: str) -> bool:
    try:
        p = os.path.normcase(os.path.realpath(path))
        r = os.path.normcase(os.path.realpath(root))
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different drives on Windows
        return False


def _foreign_target_reason(db_path: str | None, backend_name: str) -> str | None:
    """Why a spilled line's captured SQLite target is not a store to drain into.

    Foreign: its directory no longer exists (a reaped test tree, an old engine
    root — draining would create an orphan store nobody reads), or it lies under
    the temp dir and is neither this install's chatlog store nor under its engine
    root (a test/scratch store). None means "drain it". SQLite only: on a pooled
    backend ``_db_path`` is a label, not a location (§10).
    """
    if not db_path or backend_name != "sqlite":
        return None
    import tempfile

    from m3_core.paths import get_m3_engine_root

    target = os.path.abspath(db_path)
    own = os.path.abspath(chatlog_config.chatlog_db_path())
    if os.path.normcase(target) == os.path.normcase(own):
        return None
    if not os.path.isdir(os.path.dirname(target)):
        return "target directory no longer exists"
    if _is_under(target, get_m3_engine_root()):
        return None
    if _is_under(target, tempfile.gettempdir()):
        return "target is a temp-dir store, not this install's chatlog store"
    return None


def _park_lines(name: str, raw_lines: list[str], spill_dir: str) -> str:
    """Append raw spill lines to <spill_dir>/quarantine/<name>; return the path.
    Raises OSError if they did not reach disk (the caller then keeps them)."""
    qdir = os.path.join(spill_dir, _QUARANTINE_DIRNAME)
    os.makedirs(qdir, exist_ok=True)
    dest = os.path.join(qdir, name)
    with open(dest, "a", encoding="utf-8", newline="\n") as fh:
        fh.writelines(line if line.endswith("\n") else line + "\n" for line in raw_lines)
        fh.flush()
        os.fsync(fh.fileno())
    return dest


def _normalise_spilled(doc: dict) -> dict:
    """Default the private keys of a legacy/hand-written spill line.

    A spill line is the QUEUED item verbatim (`{**item, "_id", "_title",
    "_content", "_metadata_json", "_created_at", "_db_path"}`), so every field
    the live write path uses is already there.
    """
    host_agent = doc.get("host_agent", "unknown")
    doc.setdefault("_id", str(uuid.uuid4()))
    doc.setdefault("_content", doc.get("content", ""))
    doc.setdefault("_title", "")
    doc.setdefault("_created_at", doc.get("timestamp") or utc_iso())
    doc.setdefault("conversation_id", "")
    doc.setdefault("model_id", doc.get("model_id", "unknown"))
    if not doc.get("agent_id"):
        doc["agent_id"] = f"{host_agent}:spill"
    if not doc.get("_metadata_json"):
        doc["_metadata_json"] = json.dumps(
            {
                "role": doc.get("role", "unknown"),
                "provider": doc.get("provider", "unknown"),
                "model_id": doc.get("model_id", "unknown"),
                "host_agent": host_agent,
                "spill_source": True,
            }
        )
    return doc


async def _drain_one(spill_path: str, backend_name: str, max_attempts: int) -> int:
    """Drain one CLAIMED spill file. Returns rows landed.

    Every line ends in exactly one place: the store, quarantine/ (malformed →
    ``<file>.bad``, foreign target → ``foreign-<file>``), or the rewritten
    spill file (its target failed; retried next pass). The file is rewritten
    WITHOUT the groups that landed, so a retry does not re-insert them.
    """
    from m3_core.paths import _db_is_populated

    spill_dir = os.path.dirname(spill_path)
    base = os.path.basename(spill_path)
    per_db: dict[str | None, list[tuple[str, dict]]] = {}
    bad_lines: list[str] = []
    total_lines = 0
    with open(spill_path, encoding="utf-8") as f:  # noqa: ASYNC230 — callers in a live loop run this on a worker thread (m3_cognitive_loop)
        for raw in f:
            if not raw.strip():
                continue
            total_lines += 1
            try:
                doc = json.loads(raw)
                if not isinstance(doc, dict):
                    raise ValueError(f"line is a JSON {type(doc).__name__}, not an object")
            except (json.JSONDecodeError, ValueError):
                bad_lines.append(raw)
                continue
            doc = _normalise_spilled(doc)
            per_db.setdefault(doc.get("_db_path"), []).append((raw, doc))

    landed = 0
    keep: list[str] = []  # lines whose target failed this pass
    last_error = ""
    permanent = False  # any failure this pass that retrying as is cannot fix
    foreign: dict[str, tuple[str, list[str]]] = {}
    for db_path, entries in per_db.items():
        raws = [r for r, _ in entries]
        rows = [d for _, d in entries]
        why = _foreign_target_reason(db_path, backend_name)
        if why:
            foreign[str(db_path)] = (why, raws)
            continue
        # A 0-table SQLite stub passes the directory check but would die in the
        # writer with "no such table" — the same orphan-store hazard. Keep and
        # retry: a not-yet-migrated engine root can become valid.
        if db_path and backend_name == "sqlite" and not _db_is_populated(db_path):
            logger.error(
                "Spill target %s exists but carries no chatlog schema — refusing to "
                "write %d line(s) into it. Keeping them in %s; run `m3 doctor --fix` "
                "if it is this install's store.",
                db_path,
                len(rows),
                spill_path,
            )
            keep.extend(raws)
            last_error = f"target {db_path} has no chatlog schema"
            permanent = True
            continue
        # The CANONICAL writer (§10a), with conflicts ignored: a crash between
        # commit and rewrite must not make the next pass fail on the primary key.
        try:
            from chatlog_core import _executemany_insert

            written = await asyncio.to_thread(_executemany_insert, rows, ignore_conflicts=True)
            landed += written
            logger.info("Drained %d rows from %s -> %s", written, spill_path, db_path or "<live resolver>")
        except Exception as e:  # noqa: BLE001 — per-target isolation; sqlite3/psycopg errors are unrelated types
            logger.error(
                "Failed to insert %d drained row(s) from %s into %s: %s: %s",
                len(rows),
                spill_path,
                db_path or "<live resolver>",
                type(e).__name__,
                e,
            )
            keep.extend(raws)
            last_error = f"{type(e).__name__}: {e}"
            permanent = permanent or not _store_dialect().is_transient_error(e)

    # Park malformed and foreign lines BEFORE the source is rewritten or removed;
    # if parking fails they stay in the source file.
    if bad_lines:
        try:
            dest = _park_lines(f"{base}.bad", bad_lines, spill_dir)
            logger.warning(
                "Moved %d malformed line(s) from %s to %s (not JSON objects; kept for inspection)",
                len(bad_lines),
                spill_path,
                dest,
            )
        except OSError as e:
            logger.error("Could not park %d malformed line(s) from %s: %s; keeping them", len(bad_lines), spill_path, e)
            keep.extend(bad_lines)
            last_error = last_error or f"cannot park malformed lines: {e}"
            permanent = True
    for target, (why, raws) in foreign.items():
        try:
            dest = _park_lines(f"foreign-{base}", raws, spill_dir)
            logger.warning(
                "Moved %d spilled line(s) for foreign target %s (%s) from %s to %s. "
                "Turns are preserved; move them back into %s only if that store should receive them.",
                len(raws),
                target,
                why,
                spill_path,
                dest,
                spill_dir,
            )
        except OSError as e:
            logger.error(
                "Could not park %d foreign line(s) for %s from %s: %s; keeping them", len(raws), target, spill_path, e
            )
            keep.extend(raws)
            last_error = last_error or f"cannot park foreign lines: {e}"
            permanent = True

    if not keep:
        _record_attempt(spill_path, ok=True)
        try:
            os.remove(spill_path)
            logger.info("Deleted spill file: %s", spill_path)
        except FileNotFoundError:
            pass  # a concurrent drainer finished it; its rows were insert-or-ignored
        except OSError as e:
            # Every line is accounted for; a retry re-inserts nothing (ignore).
            logger.warning("Failed to delete drained spill file %s: %s", spill_path, e)
        return landed

    if len(keep) < total_lines:
        from m3_core.atomic_io import atomic_write

        try:
            atomic_write(spill_path, "".join(line if line.endswith("\n") else line + "\n" for line in keep))
        except OSError as e:
            # Landed groups stay in the file; insert-or-ignore makes the retry safe.
            logger.warning("Could not rewrite %s without its landed lines: %s", spill_path, e)
    n = _record_attempt(spill_path, ok=False, reason=last_error, counted=permanent)
    if not permanent:
        logger.warning(
            "Spill %s did not drain: the store is temporarily unavailable (%s). %d of %d line(s) kept; "
            "retried every pass, not counted toward quarantine (temporary failures since %s).",
            spill_path,
            last_error or "unknown",
            len(keep),
            total_lines,
            _read_attempts_doc(spill_path).get("transient_since", "now"),
        )
    elif n >= max_attempts:
        _quarantine_spill(spill_path, last_error or "repeated drain failure", n)
    else:
        logger.warning(
            "Spill %s did not fully drain (attempt %d/%d): %d of %d line(s) kept; %s",
            spill_path,
            n,
            max_attempts,
            len(keep),
            total_lines,
            last_error or "unknown",
        )
    return landed


async def drain_spill(conn=None) -> int:
    """
    Drain spill JSONL files back into the chat log store(s). An async coroutine:
    run it with ``asyncio.run(drain_spill())`` on a thread with no running loop.

    Each item carries the target it was enqueued against (``_db_path``) and lands
    there, so turns from a session pointed at a dedicated DB don't land in the
    live store; items without it use the live resolver. Reinsertion goes through
    ``chatlog_core._executemany_insert`` — the writer the live path uses (§10a)
    — so all 22 columns (tenancy, content_hash, origin_device) survive on either
    backend.

    Spill is NOT a SQLite-only concern: ``chatlog_core._spill_batch`` fires on
    ANY flush exception and on queue-full backpressure, and a PG outage is
    precisely when spill accumulates.

    Concurrent drainers (the sweeper task and the cognitive loop) are safe: a
    day file is claimed by rename, and reinsertion ignores ids already present.

    ``conn`` is accepted for backwards compatibility and ignored.

    Returns count of rows inserted across all target stores.
    """
    from memory.backends import active_backend

    spill_dir = chatlog_config.spill_dir()
    if not os.path.isdir(spill_dir):
        return 0
    _max_attempts = _spill_max_attempts()
    _retry_quarantined(spill_dir, _max_attempts)
    spill_files = sorted(glob(os.path.join(spill_dir, "*.jsonl")))
    if not spill_files:
        return 0

    backend_name = active_backend().name
    total_drained = 0
    for spill_path in spill_files:
        claimed = _claim_spill_file(spill_path)
        if claimed is None:
            continue
        # §4: a file that has already spent its retry budget is quarantined
        # WITHOUT reopening/reparsing it.
        _prior = _read_attempts(claimed)
        if _prior >= _max_attempts:
            _quarantine_spill(claimed, "exceeded retry budget", _prior)
            continue
        try:
            total_drained += await _drain_one(claimed, backend_name, _max_attempts)
        except Exception as e:  # noqa: BLE001 — one file must not stop the rest
            transient = _store_dialect().is_transient_error(e)
            n = _record_attempt(claimed, ok=False, reason=f"{type(e).__name__}: {e}", counted=not transient)
            logger.error(
                "Error processing spill file %s (attempt %d/%d; file kept): %s: %s",
                claimed,
                n,
                _max_attempts,
                type(e).__name__,
                e,
            )
            if not transient and n >= _max_attempts:
                _quarantine_spill(claimed, f"{type(e).__name__}: {e}", n)

    try:
        chatlog_config.record_spill_state()
    except Exception as e:  # noqa: BLE001 — bookkeeping only; the drain's work stands
        logger.warning("Spill state not recorded in %s: %s: %s", chatlog_config.STATE_FILE, type(e).__name__, e)
    return total_drained


async def embed_batch(
    conn: sqlite3.Connection,
    batch: list[tuple[str, str, str, str]],
    dry_run: bool = False,
) -> int:
    """
    Embed a batch of rows. Returns count embedded.
    batch format: [(id, content, title, metadata_json), ...]

    Kept for backwards compatibility. The main() loop now drives
    embed_sweep_lib.run_embed_loop directly; this function remains for
    any external caller that imported it. Internally still uses the
    same memory_core._embed_many primitive — bit-for-bit equivalent
    to the pre-extraction path.
    """
    if not batch:
        return 0

    texts = [content for _, content, _, _ in batch]

    # Import embedding function lazily
    from embedding_utils import pack as _pack
    from memory_core import _embed_many as embed_many

    try:
        embeddings = await embed_many(texts)
    except Exception as e:
        logger.error("Failed to embed batch: %s", e)
        return 0

    if not dry_run:
        try:
            from memory.backends import active_backend
            from memory.backends.base import EmbeddingRow

            rows_to_insert = [
                EmbeddingRow(str(uuid.uuid4()), mem_id, _pack(vec), model_str, len(vec), datetime.now(UTC).isoformat())
                for (mem_id, _, _, _), (vec, model_str) in zip(batch, embeddings, strict=True)
                if vec is not None
            ]
            if rows_to_insert:
                active_backend().insert_embeddings(conn, rows_to_insert, on_conflict="ignore")
                conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to insert embeddings: %s", e)
            return 0

    return len(batch)


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Check if a table exists in the DB."""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


async def get_unembed_count(conn: sqlite3.Connection) -> int | None:
    """Count rows needing embeddings; None when the count could not be taken.

    None, not 0: a failed query reported as 0 read as "nothing to do".
    """
    if not table_exists(conn, "memory_items") or not table_exists(conn, "memory_embeddings"):
        return 0

    try:
        row = conn.execute(
            """
            SELECT COUNT(*) as cnt
            FROM memory_items
            WHERE type='chat_log'
              AND is_deleted=0
              AND id NOT IN (SELECT memory_id FROM memory_embeddings)
            """
        ).fetchone()
        return row["cnt"] if row else 0
    except sqlite3.Error as e:
        logger.error("Failed to count unembedded rows (backlog unknown): %s", e)
        return None


def record_sweep_state(backlog: int | None, rows_embedded: int, spill_drained: int) -> None:
    """Locked merge of this run's results into the shared state file (the
    writer and status update other keys of the same file concurrently)."""

    def _mutate(state: dict) -> None:
        state["embed_backlog"] = backlog
        state["last_sweeper_run_at"] = utc_iso()
        state["last_sweeper_rows_embedded"] = rows_embedded
        state["last_sweeper_spill_drained"] = spill_drained

    try:
        chatlog_config.update_state(_mutate)
    except Exception as e:  # noqa: BLE001 — the run's work stands; say the bookkeeping did not
        logger.error("Failed to save sweeper state to %s: %s: %s", chatlog_config.STATE_FILE, type(e).__name__, e)


async def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Lazy embed chat log rows missing embeddings.")
    parser.add_argument(
        "--batch",
        type=int,
        default=None,
        help="Batch size (default from config.embed_sweeper.batch_size)",
    )
    parser.add_argument(
        "--max-per-run",
        type=int,
        default=None,
        help="Max rows per run (default from CHATLOG_EMBED_MAX_PER_RUN env or 10000)",
    )
    parser.add_argument(
        "--deadline",
        type=float,
        default=None,
        help="Soft wall-clock budget (seconds) for one run. The embed loop stops "
        "starting new batches once this elapses, so a large backlog is drained "
        "across several scheduled runs instead of monopolizing the GPU in one "
        "long run. Default from CHATLOG_EMBED_DEADLINE_S env or 60s; 0 disables "
        "(unbounded, the old behavior). max-per-run still caps total rows.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Query and log but don't embed",
    )
    parser.add_argument(
        "--drain-spill",
        action="store_true",
        help="Process spill files before embedding",
    )
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _task_runtime import add_log_file_arg, setup_task_runtime
    from m3_sdk import add_database_arg

    add_log_file_arg(parser)
    add_database_arg(parser)
    args = parser.parse_args()

    setup_task_runtime(args.log_file, lock_name="chatlog_embed_sweeper")

    if args.database:
        # M3_DATABASE unifies main+chatlog unless CHATLOG_DB_PATH overrides.
        pin_database(args.database)
        chatlog_config.invalidate_cache()

    # Resolve config
    cfg = chatlog_config.resolve_config()
    batch_size = args.batch or cfg.embed_sweeper.batch_size
    max_per_run = args.max_per_run or _m3cfg.setting("chatlog.embed.max_per_run")
    # Soft time budget per run. None -> env default (60s). 0 -> unbounded (old
    # behavior). Keeps a big backlog from pinning the GPU in one long run; the
    # next scheduled sweep picks up where this one left off (cursor-advanced).
    _deadline = args.deadline if args.deadline is not None else _m3cfg.setting("chatlog.embed.deadline_s")
    # run_embed_loop expects an ABSOLUTE time.monotonic() deadline, not a
    # duration — convert here (a raw duration like 60 would read as "already
    # elapsed" since the monotonic clock is far past 60 at runtime).
    deadline_s = None if _deadline <= 0 else time.monotonic() + _deadline

    # Spill drainage is backend-agnostic and must run BEFORE the SQLite-only
    # setup below (§10a/§3). chatlog_core._spill_batch fires on any flush
    # exception and on queue-full backpressure regardless of backend, and this
    # task is installed regardless of backend — so gating the drain behind a
    # local .db file made it a silent no-op on PostgreSQL, stranding spilled
    # turns on disk with only a log line. drain_spill() routes through the seam.
    early_spill_drained = 0
    if args.drain_spill or os.path.exists(chatlog_config.spill_dir()):
        early_spill_drained = await drain_spill()
        if early_spill_drained > 0:
            logger.info("Drained %d rows from spill", early_spill_drained)

    # Open DB connection. The embedding half below is still SQLite-bound (raw
    # sqlite3 cursors + embed_sweep_lib); on a PG deployment the backfill runs
    # via the cognitive loop's embed pass instead, so returning here after the
    # drain is correct, not a silent skip.
    # Gate on the BACKEND, not the file: a PostgreSQL deployment can still have
    # a stale SQLite file on disk, and embedding it would be silent wrong work.
    from m3_core.paths import resolve_backend_name

    db_path = chatlog_config.chatlog_db_path()
    _bk = resolve_backend_name()
    if _bk != "sqlite" or not os.path.exists(db_path):
        logger.info(
            "Skipping the SQLite embedding backfill; observed: backend=%s, local "
            "chat log DB %s %s; spill drained: %d. On a server backend the "
            "cognitive loop's embed pass does this. inspect: `m3 config get db.backend`",
            _bk,
            db_path,
            "present" if os.path.exists(db_path) else "absent",
            early_spill_drained,
        )
        return 0

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        logger.error("Failed to open chat log DB: %s", e)
        return 1

    start_time = asyncio.get_running_loop().time()

    try:
        spill_drained = early_spill_drained
        rows_embedded = 0
        batches_processed = 0

        # Check if schema exists
        if not table_exists(conn, "memory_items"):
            logger.info("Chat log schema not initialized yet; nothing to do")
            return 0

        # Query and embed batches via the shared embed_sweep_lib loop.
        # This delegates concurrency, batching, cursor advance, and per-
        # batch hardening (timeout, oversize/empty/bad-dim skip,
        # consecutive-fail abort) to one place that bin/embed_backfill.py
        # also uses. Behavior change vs. pre-extraction: ORDER BY id ASC
        # instead of ORDER BY created_at ASC. UUIDs aren't time-ordered,
        # so this changes within-batch ordering (negligible at sweeper
        # cadence) and gains infinite-loop protection on skipped rows
        # via the after_id cursor.
        from embed_sweep_lib import Counters as _Counters
        from embed_sweep_lib import run_embed_loop
        from memory_core import (
            _content_hash as _ch,
        )
        from memory_core import (
            _embed_many as _em,
        )
        from memory_core import (
            _pack as _mc_pack,
        )

        counters = _Counters()
        fetch_errors: list[str] = []

        # Fetch callback: pulls (after_id, limit) -> rows. Only chat_log
        # rows still missing an embedding row.
        def _fetch(after_id, limit):
            where = [
                "type='chat_log'",
                "is_deleted=0",
                "id NOT IN (SELECT memory_id FROM memory_embeddings)",
            ]
            params: list = []
            if after_id is not None:
                where.append("id > ?")
                params.append(after_id)
            sql = (
                f"SELECT id, content, title, metadata_json "
                f"FROM memory_items WHERE {' AND '.join(where)} "
                f"ORDER BY id LIMIT ?"
            )
            params.append(limit)
            try:
                return conn.execute(sql, params).fetchall()
            except sqlite3.Error as e:
                # [] ends the loop like "no more rows"; the flag is what tells
                # the two apart below.
                fetch_errors.append(f"{type(e).__name__}: {e}")
                logger.error("Failed to query unembedded rows from %s: %s", db_path, e)
                return []

        # Write callback: persists one embedding row. Chatlog DB has the
        # content_hash column (added in 003_chroma_sync_queue_align /
        # main migration 021's column add), so we write it.
        def _write(mid: str, vec: list[float], model_str: str, content_hash: str) -> bool:
            if args.dry_run:
                return True  # count it but don't write
            from memory.backends import active_backend
            from memory.backends.base import EmbeddingRow

            try:
                row = EmbeddingRow(
                    str(uuid.uuid4()),
                    mid,
                    _mc_pack(vec),
                    model_str,
                    len(vec),
                    datetime.now(UTC).isoformat(),
                    content_hash,
                )
                # The id is fresh, so the only conflict key cannot collide: the
                # row is always written.
                written = active_backend().insert_embeddings(conn, [row], on_conflict="ignore")
                conn.commit()
                return written > 0
            except sqlite3.Error as e:
                logger.error("Failed to insert embedding for %s: %s", mid[:8], e)
                return False

        # Drive the loop. limit=max_per_run preserves the existing
        # "max rows per scheduled run" budget.
        await run_embed_loop(
            fetch_candidates=_fetch,
            write_embedding=_write,
            counters=counters,
            embed_many=_em,
            content_hash_fn=_ch,
            transform_text=lambda t, _m: t,  # chatlog: no anchor augmentation
            batch_size=batch_size,
            concurrency=1,  # sweeper has historically run sequentially
            timeout_s=300.0,  # generous for a scheduled background job
            deadline_s=deadline_s,  # soft per-run wall-clock budget (see --deadline)
            max_consecutive_fails=5,
            max_row_bytes=32_768,
            # A long chat turn is exactly the row that used to be dropped here:
            # over bge-m3's ctx, skipped every run, invisible to semantic search
            # forever (2026-08-09: 3 such turns, and they also kept the cognitive
            # loop's embed gate permanently "has work"). Subdivide + mean-pool
            # instead, so a long turn is still searchable.
            oversize_mode="subdivide",
            expected_dim=None,  # don't reject by dim — chatlog has heterogenous models
            limit=max_per_run,
            log=lambda msg: logger.info("%s", msg),
        )
        rows_embedded = counters.embedded
        batches_processed = counters.batches_completed

        # Get remaining backlog count
        backlog = await get_unembed_count(conn)
        record_sweep_state(backlog, rows_embedded, spill_drained)

        elapsed = asyncio.get_running_loop().time() - start_time
        backlog_txt = "unknown (count failed)" if backlog is None else str(backlog)

        if fetch_errors:
            logger.error(
                "Embed sweep stopped early: the candidate query failed (%s) on %s. "
                "Embedded %d rows before it; spill drained: %d; backlog: %s. "
                "inspect: m3 chatlog doctor",
                fetch_errors[-1],
                db_path,
                rows_embedded,
                spill_drained,
                backlog_txt,
            )
            return 1
        if rows_embedded > 0 or spill_drained > 0:
            logger.info(
                "Embedded %d rows in %d batches (%.1fs), spill drained: %d, backlog remaining: %s",
                rows_embedded,
                batches_processed,
                elapsed,
                spill_drained,
                backlog_txt,
            )
        elif backlog is None:
            logger.error("Embedded 0 rows; the backlog count failed, so 'nothing to do' is unverified (%s)", db_path)
            return 1
        else:
            logger.info("Nothing to do (backlog: %d)", backlog)

        return 0

    except Exception as e:
        logger.exception("Sweeper failed: %s", e)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    # Logging is configured inside main() via setup_task_runtime once args
    # are parsed. A minimal fallback keeps logger output visible if main()
    # raises before that point.
    logging.basicConfig(
        level=logging.INFO,
        format="%(name)s: [%(levelname)s] %(message)s",
    )
    sys.exit(asyncio.run(main()))
