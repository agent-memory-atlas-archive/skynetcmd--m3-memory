from m3_core.paths import apply_early_argv as _apply_early_argv

# --config FILE (the file this job was registered with) is pinned, and every
# --set KEY=VALUE applied, before any import below binds a root or reads a
# setting at import time (chatlog_config, log dirs).
_apply_early_argv()

import base64
import json
import logging
import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from itertools import batched
from typing import cast

DEFAULT_PROTECTED_TYPES = ("preference", "user_fact", "task", "plan")

import memory_core
from llm_failover import (
    LLMUnavailable,
    apply_thinking_suppression,
    report_available,
    report_unavailable,
)
from m3_core.config import settings as _m3cfg
from m3_core.errors import log_swallowed
from memory_core import (
    _content_hash,
    _cosine,
    _db,
    _embed,
    _get_embed_client,
    _pack,
    _unpack,
    ctx,
    get_best_llm,
    m3_core_rs,
    memory_link_impl,
)

logger = logging.getLogger("memory_maintenance")


def _row_get(row, key, default=None):
    """Read a column from a seam row whether it exposes mapping access (both
    backends do via the seam) or only positional — tolerating a column the row's
    SELECT * didn't include on an older schema. Never raises."""
    try:
        if hasattr(row, "keys"):
            return row[key] if key in row.keys() else default
        return row[key]
    except Exception:
        return default


def _transfer_to_archive(item_id, reason, db):
    """Copy a memory into the archive tombstone table BEFORE the live row is
    soft-deleted/deleted by the maintenance pass. Routed entirely through the seam
    (`db`), so it writes to memory_archive in the PRIMARY store on both SQLite and
    PostgreSQL — replacing the old separate SQLite sidecar file whose table was
    never created (so every archive write silently no-opped). Idempotent: a
    re-archive of the same id upserts on the id PK.

    Wrapped in savepoint() so a failure here (e.g. pre-042 DB without the table)
    is isolated and does NOT abort the maintenance transaction on PG — the caller
    treats a False return as "not archived" and proceeds to delete anyway, matching
    the historical best-effort contract."""
    from memory.backends import dialect as _dialect
    from memory.db import savepoint as _savepoint

    _d = _dialect()
    _p = _d.param()
    now = datetime.now(UTC).isoformat()
    row = db.execute(f"SELECT * FROM memory_items WHERE id = {_p}", (item_id,)).fetchone()
    if not row:
        return False
    cols = ["id", "type", "title", "content", "agent_id", "user_id", "archive_reason", "archived_at"]
    vals = (
        _row_get(row, "id"),
        _row_get(row, "type"),
        _row_get(row, "title"),
        _row_get(row, "content"),
        _row_get(row, "agent_id"),
        _row_get(row, "user_id"),
        reason,
        now,
    )
    # Upsert on the id PK: re-archiving the same memory overwrites its tombstone
    # rather than raising, so the pass is safe to re-run.
    upsert = _d.on_conflict_update("(id)", [c for c in cols if c != "id"])
    sql = f"INSERT INTO memory_archive ({', '.join(cols)}) VALUES ({_d.placeholder(len(cols))}) {upsert}"
    try:
        with _savepoint(db):
            db.execute(sql, vals)
        return True
    except Exception as e:  # noqa: BLE001 — best-effort tombstone; never block the purge
        log_swallowed(logger, e, f"archive write skipped for {item_id}: {e}")
        return False


def memory_dedup_impl(threshold=None, dry_run=True, limit=0):
    """Find near-duplicate memory items by cosine similarity over embeddings.

    Returns a structured dict:
      {
        "count": <int total pairs found>,
        "groups": [
            {"a": <id kept>, "b": <id duplicate>, "title_a": <str>, "title_b": <str>, "score": <float>},
            ...
        ],
        "threshold": <float>,
        "scanned": <int embedding rows scanned>,
        "scan_limit": <int memory.dedup.limit>,
        "partitions": <int (user, agent, embed model, dim) partitions compared>,
        "applied": <bool — True if dry_run=False and every pair was superseded>,
        "applied_count": <int rows actually superseded>,
        "not_applied": [<b ids already closed by someone else>],
      }

    Pairs are only formed WITHIN one (user_id, agent_id, embed_model, dim)
    partition: two subjects' memories are never each other's duplicates, and
    vectors from different models are not comparable. Pinned memories are never
    candidates. Only the `default` vector of each memory is compared.

    `limit`: cap the returned `groups` list at this many entries (0 = no cap).
    `count` always reflects the true total found, even when groups is trimmed.

    On dry_run=False each duplicate `b` is SUPERSEDED by `a` through the shared
    supersede write (memory.write._mark_superseded): valid_to, updated_at, a
    `supersedes` edge and a history event, gated on the row still being live —
    so the result reports the rows changed, not the rows intended.

    `threshold`: unset reads memory.dedup.threshold at call time (a default
    argument would bind it at import and miss m3_config.toml).
    """
    import time

    from m3_sdk import _LAST_USER_INTERACTION

    from memory import config as _config

    if time.time() - _LAST_USER_INTERACTION < 15.0:
        logger.info("Active query session detected. Suspending curation pass to yield resources.")
        time.sleep(5.0)
    if threshold is None:
        threshold = _config.DEDUP_THRESHOLD
    scan_limit = int(_config.DEDUP_LIMIT)
    from memory.backends import dialect as _dialect

    _d = _dialect()
    _p = _d.param()
    with _db() as db:
        rows = db.execute(
            "SELECT me.memory_id, me.embedding, me.embed_model, mi.title, mi.user_id, mi.agent_id "
            "FROM memory_embeddings me JOIN memory_items mi ON me.memory_id = mi.id "
            "WHERE mi.is_deleted = 0 AND COALESCE(mi.pinned, 0) = 0 "
            "AND COALESCE(me.vector_kind, 'default') = 'default' "
            f"ORDER BY mi.created_at DESC LIMIT {_p}",
            (scan_limit,),
        ).fetchall()

    def _get(r, k):
        return r.get(k) if hasattr(r, "get") else _row_get(r, k)

    partitions: dict[tuple, list] = {}
    for r in rows:
        blob = r["embedding"]
        key = (
            _get(r, "user_id") or "",
            _get(r, "agent_id") or "",
            _get(r, "embed_model") or "",
            len(blob) if isinstance(blob, (bytes, bytearray, memoryview)) else -1,
        )
        partitions.setdefault(key, []).append(r)

    def _scan(ids, titles, raw_blobs) -> list[tuple]:
        # Rust hot path: concatenate all packed blobs into one contiguous bytes
        # buffer, then for each row i call cosine_batch_packed_flat over the
        # tail slice blobs[i+1:] (rayon, zero per-row copies). The pure-Python
        # fallback (m3_core_rs unavailable or erroring mid-scan) keeps the
        # per-pair _cosine semantics, so the output is identical across paths.
        n = len(ids)
        duplicates: list[tuple] = []  # (id_a, id_b, title_a, title_b, score)
        seen: set[str] = set()
        bytes_per_row = len(raw_blobs[0]) if n and isinstance(raw_blobs[0], (bytes, bytearray)) else 0
        dim = bytes_per_row // 4
        use_rust = (
            m3_core_rs is not None
            and n > 1
            and dim > 0
            and all(isinstance(b, (bytes, bytearray)) and len(b) == bytes_per_row for b in raw_blobs)
        )
        if use_rust:
            flat = b"".join(bytes(b) if isinstance(b, bytearray) else b for b in raw_blobs)
            unpacked = [_unpack(b) for b in raw_blobs]
            try:
                for i in range(n):
                    if ids[i] in seen:
                        continue
                    tail = flat[(i + 1) * bytes_per_row :]
                    if not tail:
                        break
                    scores = m3_core_rs.cosine_batch_packed_flat(unpacked[i], tail, dim)
                    # scores[k] is the cosine of row i vs row (i+1+k)
                    for k, score in enumerate(scores):
                        j = i + 1 + k
                        if ids[j] in seen:
                            continue
                        # Self-pair guard: a memory with several embedding rows
                        # appears at several indices.
                        if ids[i] == ids[j]:
                            continue
                        if score >= threshold:
                            duplicates.append((ids[i], ids[j], titles[i], titles[j], float(score)))
                            seen.add(ids[j])
                return duplicates
            except Exception as e:  # noqa: BLE001 — fall back rather than fail the survey
                logger.warning(
                    f"dedup Rust path failed mid-scan ({type(e).__name__}: {e}); falling back to pure-Python loop"
                )
                duplicates, seen = [], set()
        items = [(ids[i], _unpack(raw_blobs[i]), titles[i]) for i in range(n)]
        for i, (mid_a, vec_a, title_a) in enumerate(items):
            if mid_a in seen:
                continue
            for j in range(i + 1, len(items)):
                mid_b, vec_b, title_b = items[j]
                if mid_b in seen or mid_a == mid_b:  # self-pair guard, as above
                    continue
                score = _cosine(vec_a, vec_b)
                if score >= threshold:
                    duplicates.append((mid_a, mid_b, title_a, title_b, float(score)))
                    seen.add(mid_b)
        return duplicates

    duplicates: list[tuple] = []
    for part in partitions.values():
        if len(part) > 1:
            duplicates += _scan(
                [r["memory_id"] for r in part], [r["title"] for r in part], [r["embedding"] for r in part]
            )

    applied_count = 0
    not_applied: list[str] = []
    if not dry_run and duplicates:
        from memory import write as _write

        # _mark_superseded calls a memory_core callback bound lazily on the write
        # path; a caller outside it must bind first or the history write is None().
        _write._resolve_mc_callbacks()
        _mark_superseded = _write._mark_superseded
        now = datetime.now(UTC).isoformat()
        with _db() as db:
            for mid_a, mid_b, _, _, _ in duplicates:
                if _mark_superseded(mid_b, mid_a, close_at=now, db=db, actor_id="memory_dedup", require_active=True):
                    applied_count += 1
                else:
                    not_applied.append(mid_b)
        if not_applied:
            logger.warning(
                "memory_dedup: %d of %d duplicates were already closed by another writer and were left as they are",
                len(not_applied),
                len(duplicates),
            )

    groups = duplicates if not limit else duplicates[: int(limit)]
    return {
        "count": len(duplicates),
        "groups": [
            {"a": a, "b": b, "title_a": ta, "title_b": tb, "score": round(sc, 4)} for a, b, ta, tb, sc in groups
        ],
        "threshold": float(threshold),
        "scanned": len(rows),
        "scan_limit": scan_limit,
        "partitions": len(partitions),
        "applied": bool(duplicates) and not dry_run and applied_count == len(duplicates),
        "applied_count": applied_count,
        "not_applied": not_applied,
    }


# The three verdicts the ToolSpec advertises. `wrong` is accepted as a legacy
# ALIAS for `misleading` (it was the only negative branch the impl ever honoured,
# and it soft-deleted) -- see the docstring for why it no longer deletes.
_FEEDBACK_VERDICTS = ("useful", "not_useful", "misleading")

# How far one explicit verdict moves importance. `useful` keeps its historical
# +0.1 exactly (existing workflows must not change). `misleading` is priced
# HIGHER than useful in the same spirit as the confidence model, where
# CONTRADICTION_UNIT (0.10) is twice CORROBORATION_UNIT (0.05): negative evidence
# about correctness is worth more than positive evidence about usefulness.
_FEEDBACK_USEFUL_LIFT = 0.1
_FEEDBACK_MISLEADING_DROP = 0.2

# ── Decay floors ─────────────────────────────────────────────────────────────
#
# Decay used to run toward 0.0, so a memory's importance was bounded only by how
# long it had been ignored. A floor bounds it by what the memory has EARNED
# instead: forgetting should asymptote, not erase.
#
# Every value here sits AT OR BELOW what the live corpus already held when the
# floors were introduced (measured 2026-09-19 over 4,034 rows), so switching them
# on moves no row on the first pass -- it only bounds future decay. That makes
# the change observable in isolation: movement on pass one is a bug, not the
# feature.
#
#   pinned      observed min 0.75, avg 0.852  -> floor 0.70
#   graded      (new signal, no history)      -> floor 0.40  (above the 0.298
#                                                ordinary average, so a memory
#                                                someone called useful stays
#                                                distinguishable from the mass)
#   ordinary    observed min 0.097, avg 0.298 -> floor 0.01
#
# ⚠ WHY 0.01 AND NOT 0.05, WHICH IS WHERE THIS STARTED. "Safely below the
# observed 0.097 minimum" justifies *a* floor under 0.097; it does not pick one.
# Measured, because the reasoning was otherwise just a preference:
#
#   floor   score contribution   days of neglect   graded:ordinary
#           (IMPORTANCE_WEIGHT     to bottom out       ratio
#            * floor = 0.15*f)
#   0.05         0.0075               24.9              8x
#   0.03         0.0045               29.1             13x
#   0.01         0.0015               38.3             40x
#
# The ENTIRE spread from 0.05 to 0.01 is 0.006 of final score, against a
# TITLE_MATCH_BOOST of 0.15 -- one matching title word outweighs the whole
# question by 25x. So the choice is ranking-IRRELEVANT, and the tie-breaker is
# what else the number buys: at 0.01 a graded memory reads 40x an ordinary one
# instead of 8x, which is the separation the graded tier exists to create.
#
# The load-bearing property is NON-ZERO, not the magnitude: a floor above zero
# means an ignored memory keeps a residual weight and stays retrievable by
# content, contributing ~nothing to rank. That is "asymptote, not erase", and
# 0.01 satisfies it exactly as well as 0.05 while separating the tiers better.
#
# ⚠ Pinned rows are EXCLUDED from the decay UPDATE entirely (the WHERE clause),
# so _PINNED_FLOOR is the value the reinforcement pass and any future unpin path
# must respect -- not something this statement applies. Kept here so the tiers
# read as one policy rather than being split across two files.
_DECAY_RATE = 0.995
_PINNED_FLOOR = 0.70
_GRADED_FLOOR = 0.40
_ORDINARY_FLOOR = 0.01

# ── Reinforcement ────────────────────────────────────────────────────────────
#
# How much of a pass's decay a READ memory gets back. confidence.
# access_reinforcement() caps its lift at 0.05, and this scales that into a
# per-pass rate nudge: a hot memory decays at ~0.9995 instead of 0.995, so it
# fades roughly an order of magnitude more slowly without ever growing.
#
# ⚠ _REINFORCE_CEILING is strictly BELOW _GRADED_FLOOR and that gap is
# load-bearing, not cosmetic: it is the structural guarantee that no volume of
# retrieval reaches the standing one explicit `helpful` verdict earns. Seen is
# not used. If these two ever meet, a spurious memory swept into enough result
# sets becomes indistinguishable from one a human vouched for.
_REINFORCE_RATE_SHARE = 0.1
_REINFORCE_CEILING = _GRADED_FLOOR - 0.01

# Reporting-only threshold: memories below this and older than 30 days are
# SURFACED as archive candidates. Nothing acts on it automatically.
#
# ⚠ DERIVED FROM THE FLOOR ON PURPOSE. This was a hardcoded 0.05 that happened to
# equal the decay floor, which is the only reason its delete never fired -- decay
# stopped exactly at the line. Two independent constants with an undocumented
# relationship, where changing one re-enabled a destructive path governed by the
# other. Expressing it as floor*5 makes the dependency visible: retune the floor
# and the candidate band moves with it, by construction rather than by luck.
_ARCHIVE_CANDIDATE_THRESHOLD = _ORDINARY_FLOOR * 5


def _decay_floor_sql(_d) -> str:
    """Per-row decay floor as a SQL expression, rendered through the seam.

    A memory that someone explicitly graded `helpful` has evidence behind it that
    an untouched memory does not, so it must not decay into the same band. A CASE
    expression keeps that as ONE set-based UPDATE (§4: SQL does the work; no
    per-row Python loop) while still varying the bound per row.

    Uses helpful_count/unhelpful_count from migration 049. On a pre-049 DB the
    column reference raises, and the caller falls back to the flat 0.0 floor --
    the same absence-tolerant shape the pinned-column fallback already uses.
    """
    return (
        "CASE WHEN COALESCE(helpful_count, 0) > COALESCE(unhelpful_count, 0) "
        f"THEN {_GRADED_FLOOR} ELSE {_ORDINARY_FLOOR} END"
    )


def _reinforce_importance(db) -> int:
    """Raise the decay floor of memories that are actually being USED.

    ⚠ THE ASYMMETRY THIS EXISTS TO FIX. m3 tracked access_count on every
    retrieval (1,034 rows carried counts; the hottest had been read 762 times)
    and nothing ever read it back. So a memory retrieved 762 times decayed at
    exactly the same rate as one nobody had touched in a year.

    ⚠ RETRIEVAL IS WEAK EVIDENCE, AND THIS DELIBERATELY TREATS IT THAT WAY.
    Being returned in a result set means the RANKER matched it -- not that it
    helped. A spurious memory that keeps matching queries would otherwise be
    strengthened by its own noise, entrenching exactly the wrong content. Three
    structural defences, none of them a tuning knob:

      1. confidence.access_reinforcement() is LOGARITHMIC and HARD-CAPPED
         (unit 0.01, cap 0.05): 50 reads and 10,000 reads both yield 0.05.
         Measured -- a runaway is impossible by construction, not by limit.
      2. The lift raises the FLOOR, never the importance directly. Being read a
         lot slows forgetting; it cannot manufacture salience.
      3. The reinforced floor is capped BELOW _GRADED_FLOOR, so no amount of
         retrieval reaches the band an explicit `helpful` verdict earns. Seen
         is not used, and used is not useful.

    ⚠ THIS SLOWS THE DECAY RATE; IT DOES NOT RAISE A FLOOR. The first version
    raised the floor instead, and a test caught it doing nothing: after 300
    passes a memory read 762 times and one never read had both settled at
    0.1111, identical. A floor only binds once decay REACHES it -- 423 passes to
    fall to 0.06, 780 to reach 0.01 -- so for the entire period anyone would
    notice, reinforcement was invisible. Lowering the ordinary floor to 0.01 made
    that worse. Slowing the rate acts on every pass from the first, which is what
    "frequently used memories fade more slowly" actually means.

    IDEMPOTENT BY RECOMPUTATION, like _reinforce_confidence: the retained
    importance is derived from the CURRENT access_count on every pass rather than
    accumulated as a delta. That is what keeps an hourly maintenance loop from
    compounding a weak signal into a strong one.

    Returns the number of rows whose decay was slowed.
    """
    from memory.backends import dialect as _dialect
    from memory.db import savepoint as _savepoint

    from memory import confidence as _conf

    _d = _dialect()
    _p = _d.param()

    # Only rows that have actually been read, are in the decay window, and are
    # not pinned (pinned already sits above every reinforced floor).
    try:
        with _savepoint(db):
            rows = db.execute(
                "SELECT id, COALESCE(access_count, 0) FROM memory_items "
                "WHERE is_deleted = 0 AND COALESCE(pinned, 0) = 0 "
                "AND COALESCE(access_count, 0) > 0"
            ).fetchall()
    except Exception as e:  # noqa: BLE001 — pre-pinned-column DB
        if not _is_missing_schema(e):
            raise
        return 0

    # Bucket by lift so N rows become a handful of set-based UPDATEs (§4: SQL
    # does the work; no per-row round trip). log2 bucketing means thousands of
    # rows collapse into at most ~6 distinct lift values.
    buckets: dict[float, list] = {}
    for mid, count in rows:
        lift = _conf.access_reinforcement(int(count))
        if lift > 0:
            buckets.setdefault(round(lift, 6), []).append(mid)

    raised = 0
    for lift, ids in buckets.items():
        # Give back a fraction of what this pass's decay just took. The decay
        # UPDATE has already run (same maintenance pass, earlier block), so this
        # restores `lift`-proportional ground rather than adding free importance:
        # a read memory decays more slowly, it does not grow.
        #
        # _REINFORCE_RATE_SHARE scales the capped lift (max 0.05) into a rate
        # nudge, and the result is clamped to _REINFORCE_CEILING -- strictly
        # below _GRADED_FLOOR, so no volume of retrieval reaches the band an
        # explicit `helpful` verdict earns. Seen is not used.
        givebacks = lift * _REINFORCE_RATE_SHARE
        for chunk in batched(ids, 500):
            placeholders = ", ".join([_p] * len(chunk))
            res = db.execute(
                f"UPDATE memory_items SET importance = "
                f"{_d.least(str(_REINFORCE_CEILING), f'importance * (1.0 + {givebacks})')} "
                f"WHERE id IN ({placeholders}) AND importance < {_REINFORCE_CEILING}",
                tuple(chunk),
            )
            raised += res.rowcount or 0
    return raised


# Archive reasons written by AUTONOMOUS passes. A memory removed for one of
# these was never a human's decision, so it is restorable by default. A reason
# NOT in this set (a user-invoked delete, a GDPR erasure) is deliberate and is
# never swept back in by memory_restore_impl.
_AUTONOMOUS_ARCHIVE_REASONS = ("low_importance",)
# Reasons written by a HUMAN-CONFIGURED policy (memory_set_retention): restorable
# only when named explicitly, never by the default sweep -- the next maintenance
# pass re-applies the policy unless it is changed first.
_POLICY_ARCHIVE_REASONS = ("retention_ttl", "retention_limit")


# ── Graded-feedback window ───────────────────────────────────────────────────
#
# An agent retrieves (id, content), decides, and calls back seconds-to-minutes
# later from a DIFFERENT process. The window bounds how late that callback may
# be, sourced from last_accessed_at (persisted, cross-process, stamped within
# 250 ms of retrieval by memory/db.py's batched flusher).
#
# ⚠ WHY A WINDOW AT ALL. Without one, memory_id is an unauthenticated write
# primitive: any caller could post `helpful` for any UUID with no evidence of
# ever having retrieved it. The window makes the RETRIEVAL the capability.
#
# ⚠ 5 MINUTES IS DELIBERATELY TIGHT AND HAS A KNOWN FAILURE MODE: a long
# tool-using turn can exceed it, and honest feedback then gets rejected — which
# teaches agents to stop sending it. Two mitigations, both required:
#   1. It is CONFIGURABLE without a code change or restart (below).
#   2. Rejections are counted, so "is the window wrong?" is a measurement rather
#      than a guess — see _FEEDBACK_WINDOW_REJECTIONS.
_FEEDBACK_WINDOW_MINUTES_DEFAULT = 5
_FEEDBACK_WINDOW_CFG_TTL = 5.0

# Upper bound on the feedback window, whatever route sets it.
#
# ⚠ THIS IS WHAT MAKES RETRIEVAL THE CAPABILITY. The window exists so a caller
# can only grade what it demonstrably just retrieved; without a ceiling, a
# per-call `window_minutes=525600` widens that to "anything retrieved this
# year", which is most of the store, and the grade stops being evidence of use.
# 24h is far past any plausible tool-using turn (the default is 5 minutes) while
# still bounding the claim. Clamped, not rejected: an over-wide request is a
# tuning mistake, not an attack, and the response reports the value used.
_FEEDBACK_WINDOW_MINUTES_MAX = 1440
_fb_window_cache: dict = {"ts": 0.0, "mtime": None, "minutes": None}

# Rejection counter, process-local: "is the window too tight for THIS process's
# agents", a live operational question. The durable record is per UTC day in
# memory_grade_daily (migration 058, credit loop part A): per day, so a tuning
# change shows as a change between days instead of blending into one total.
_FEEDBACK_WINDOW_REJECTIONS = {"accepted": 0, "rejected": 0}


_GRADE_RECORD_MISSING = (
    "observed: memory_grade_daily is missing, so this grade was applied but not recorded "
    "in the daily grading record. cause: the store has not reached migration 058. "
    "fix: python bin/migrate_memory.py up --yes"
)


def _utc_day(days_ago: int = 0) -> str:
    """A UTC date as YYYY-MM-DD (the memory_grade_daily key)."""
    return (datetime.now(UTC).date() - timedelta(days=days_ago)).isoformat()


def _record_grade_day(db, accepted: int, rejected: int) -> None:
    """Add today's accepted / stale-rejected verdicts to memory_grade_daily."""
    from memory.backends import dialect as _dialect

    _d = _dialect()
    _p = _d.param()
    db.execute(
        f"INSERT INTO memory_grade_daily (day, accepted, rejected_stale) VALUES ({_p}, {_p}, {_p}) "
        + _d.on_conflict_add("(day)", "memory_grade_daily", ["accepted", "rejected_stale"]),
        (_utc_day(), accepted, rejected),
    )


def _grade_days(db, window_days: int) -> dict:
    """memory_grade_daily totals over the last ``window_days`` UTC days (today included)."""
    from memory.backends import dialect as _dialect

    _p = _dialect().param()
    row = db.execute(
        f"SELECT COALESCE(SUM(accepted), 0) AS a, COALESCE(SUM(rejected_stale), 0) AS r "
        f"FROM memory_grade_daily WHERE day >= {_p}",
        (_utc_day(window_days - 1),),
    ).fetchone()
    a, r = int(row["a"]), int(row["r"])
    return {
        "window_days": window_days,
        "accepted": a,
        "rejected_stale": r,
        "rejection_rate": round(r / (a + r), 4) if a + r else 0.0,
    }


def _feedback_window_minutes(now: float | None = None) -> int:
    """Minutes a graded verdict stays valid after retrieval. file > env > default.

    Lives in the existing `.governor_config.json` under `feedback_window_minutes`
    rather than a new file — the same reasoning m3_core.gpu gives for putting
    gpu_probe_ttl_seconds there: a fourth config file for one number is the
    sprawl §2 warns about. Read directly rather than through governor's resolver
    to avoid a new import edge (§10a).

    ⚠ A CONFIG FILE, NOT JUST AN ENV VAR, because the cognitive loop that will
    read this runs headless: none of the Windows scheduled task, macOS launchd
    agent, or systemd --user unit reliably inherits a shell environment, so an
    env var that is perfect in a terminal is simply absent there (§3).

    mtime-cached, stat-throttled. Never raises.
    """
    import time as _time

    now = now if now is not None else _time.time()
    if now - _fb_window_cache["ts"] >= _FEEDBACK_WINDOW_CFG_TTL:
        _fb_window_cache["ts"] = now
        try:
            from m3_sdk import get_m3_config_root

            path = os.path.join(get_m3_config_root(), ".governor_config.json")
        except Exception:  # noqa: BLE001 — config root unresolvable
            path = None
        mtime = None
        if path:
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                mtime = None  # absent -> env + default
        if mtime != _fb_window_cache["mtime"]:
            _fb_window_cache["mtime"] = mtime
            val = None
            if mtime is not None and path:
                try:
                    import json as _json

                    with open(path, encoding="utf-8") as f:
                        val = (_json.load(f) or {}).get("feedback_window_minutes")
                except Exception as e:  # noqa: BLE001
                    # §3 never silent: a malformed file would otherwise revert to
                    # the default invisibly, and the operator's tuning would be
                    # dead while appearing live.
                    logger.warning(
                        "Governor config %s is unreadable/malformed (%s) — "
                        "feedback window falling back to env var + default.",
                        path,
                        e,
                    )
            _fb_window_cache["minutes"] = val

    val = _fb_window_cache["minutes"]
    if val is None:
        # Registry key governor.feedback_window_minutes (default 5); malformed fails at load.
        val = _m3cfg.setting("governor.feedback_window_minutes")
    try:
        return _clamp_feedback_window(int(val))
    except (TypeError, ValueError):
        return _FEEDBACK_WINDOW_MINUTES_DEFAULT


def _clamp_feedback_window(minutes: int) -> int:
    """Bound a feedback window to [1, _FEEDBACK_WINDOW_MINUTES_MAX].

    Single owner for the bound (§10a): the config file, the env var and the
    per-call `window_minutes` override all resolve through here, so widening the
    ceiling is one edit and no route can quietly exceed it.
    """
    return max(1, min(int(minutes), _FEEDBACK_WINDOW_MINUTES_MAX))


def memory_restore_impl(memory_id=None, reason=None, dry_run=True, limit=1000):
    """Bring back memories an AUTONOMOUS pass soft-deleted. Never user deletions.

    ⚠ WHY THIS EXISTS. Until now a background sweep soft-deleted every memory
    under an importance threshold: 5,001 of them, all stamped
    archive_reason='low_importance'. An autonomous process does not get to decide
    a deletion -- forgetting is DERANKING, and removal is a human's call. The
    sweep no longer deletes (it reports candidates), but the rows it already took
    need a way back.

    The distinction that makes this safe is recorded in the data: every archived
    row carries `archive_reason`. Only reasons in _AUTONOMOUS_ARCHIVE_REASONS are
    eligible. A memory you deleted on purpose stays deleted -- restoring those
    would be the same category error in the opposite direction, overriding a
    human decision with a machine one.

    Restoration is a FLAG FLIP, not a copy: the live row still exists with
    is_deleted=1 (verified: all 5,001 archived rows have their live row intact),
    so the content, embeddings and relationships never left.

    dry_run defaults TRUE. This writes to the memory store, and the caller should
    see the count before it happens.
    """
    from memory.backends import dialect as _dialect

    _d = _dialect()
    _p = _d.param()

    reasons = (reason,) if reason else _AUTONOMOUS_ARCHIVE_REASONS
    bad = [r for r in reasons if r not in (*_AUTONOMOUS_ARCHIVE_REASONS, *_POLICY_ARCHIVE_REASONS)]
    if bad:
        return {
            "ok": False,
            "restored": 0,
            "error": "reason_not_autonomous",
            "observed": f"reason={bad!r}",
            "note": ("only memories removed by an autonomous pass are restorable; a deliberate deletion stays deleted"),
            "accepted": [*_AUTONOMOUS_ARCHIVE_REASONS, *_POLICY_ARCHIVE_REASONS],
        }

    placeholders = ", ".join([_p] * len(reasons))
    with _db() as db:
        if memory_id:
            rows = db.execute(
                f"SELECT a.id FROM memory_archive a JOIN memory_items m ON m.id = a.id "
                f"WHERE a.id = {_p} AND m.is_deleted = 1 "
                f"AND a.archive_reason IN ({placeholders})",
                (memory_id, *reasons),
            ).fetchall()
        else:
            rows = db.execute(
                f"SELECT a.id FROM memory_archive a JOIN memory_items m ON m.id = a.id "
                f"WHERE m.is_deleted = 1 AND a.archive_reason IN ({placeholders}) "
                f"ORDER BY a.archived_at DESC LIMIT {int(limit)}",
                tuple(reasons),
            ).fetchall()

        ids = [r["id"] if hasattr(r, "keys") else r[0] for r in rows]
        # `limit` caps one pass; the total says how much a capped pass leaves.
        eligible = len(ids)
        if not memory_id:
            eligible = int(
                db.execute(
                    f"SELECT COUNT(*) AS n FROM memory_archive a JOIN memory_items m ON m.id = a.id "
                    f"WHERE m.is_deleted = 1 AND a.archive_reason IN ({placeholders})",
                    tuple(reasons),
                ).fetchone()["n"]
            )
        if dry_run:
            return {
                "ok": True,
                "applied": False,
                "dry_run": True,
                "would_restore": len(ids),
                "eligible": eligible,
                "capped": eligible > len(ids),
                "reasons": list(reasons),
                "note": (
                    f"re-run with dry_run=False to restore {len(ids)} of {eligible}"
                    + (f" (limit={int(limit)}; run again for the rest)" if eligible > len(ids) else "")
                ),
            }

        restored = 0
        for chunk in batched(ids, 500):
            ph = ", ".join([_p] * len(chunk))
            # Lift importance to AT LEAST the archive-candidate threshold (a row
            # swept below it comes back there; one that held more keeps it): back
            # in the corpus, still ranked low -- the deranked-not-deleted end state.
            res = db.execute(
                f"UPDATE memory_items SET is_deleted = 0, importance = "
                f"{_d.greatest(str(_ARCHIVE_CANDIDATE_THRESHOLD), 'importance')} "
                f"WHERE id IN ({ph})",
                tuple(chunk),
            )
            restored += res.rowcount or 0

    return {
        "ok": True,
        "applied": True,
        "restored": restored,
        "eligible": eligible,
        "remaining": max(eligible - restored, 0),
        "reasons": list(reasons),
        "note": ("restored at the archive-candidate threshold — present and searchable, still ranked low"),
    }


def memory_grade_impl(grades=None, window_minutes=None):
    """Grade retrieved memories AFTER using them. The citation signal m3 lacked.

    An agent retrieves (id, content), produces its answer, then calls this ONCE
    with a verdict per memory it was shown:

        memory_grade(grades=[{"memory_id": "...", "verdict": "helpful"},
                             {"memory_id": "...", "verdict": "unhelpful"}])

    ⚠ POST-ANSWER, NOT AT RETRIEVAL, and the distinction is the whole point.
    Grading at retrieval time rates relevance-on-sight — approximately what the
    ranker already computed — and feeding that back would be the ranker marking
    its own homework. "Which of these did I actually rely on?" is different
    information, and it is the only automatic signal separating USED from merely
    RETRIEVED. m3 stamps access_count on retrieval; that says *seen*. This says
    *used*.

    ⚠ TWO COUNTERS, NEVER A NET. helpful_count and unhelpful_count are stored
    separately (migration 049) and combined only at read time. A net is lossy
    exactly where it matters: +5/-5 (contested, heavily used) and +0/-0 (never
    seen) both net to zero while meaning opposite things. Separate counters also
    permit asymmetric weighting, which a pre-summed number cannot express — the
    confidence model keeps corroboration_count and contradiction_count apart for
    the identical reason.

    ⚠ TIME-BOUNDED. A verdict is only accepted while the memory was retrieved
    within the feedback window (default 5 min, configurable — see
    _feedback_window_minutes). Without that bound, memory_id is an
    unauthenticated write primitive: anyone could grade any UUID having never
    seen it. A LATE grade is NOT silently dropped — that would repeat the
    false-success defect this module was just fixed for. It returns
    applied=False with a reason, so the caller can see it did not count.

    Bulk by design (§4: "bulk variants for any tool an agent calls in a loop").
    Ten results graded one-at-a-time is ten round trips for one decision.
    """
    from memory.backends import dialect as _dialect

    if not grades:
        return {"ok": True, "applied": False, "graded": 0, "note": "no grades supplied"}

    _d = _dialect()
    _p = _d.param()
    requested = None
    if window_minutes:
        try:
            requested = int(window_minutes)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "applied": False,
                "graded": 0,
                "error": "bad_window_minutes",
                "observed": f"window_minutes={window_minutes!r} is not an integer",
            }
        minutes = _clamp_feedback_window(requested)
    else:
        minutes = _feedback_window_minutes()

    accepted, rejected, unknown = [], [], []
    with _db() as db:
        # One query resolves every id's eligibility: retrieved recently enough,
        # and still live. Set-based rather than a per-id round trip (§4).
        ids = [str(g.get("memory_id") or "") for g in grades if g.get("memory_id")]
        if not ids:
            return {"ok": False, "applied": False, "graded": 0, "error": "no_memory_ids"}
        ph = ", ".join([_p] * len(ids))
        fresh = {
            r[0]
            for r in db.execute(
                f"SELECT id FROM memory_items WHERE id IN ({ph}) "
                f"AND is_deleted = 0 AND last_accessed_at IS NOT NULL "
                f"AND {_d.age_minutes_lt('last_accessed_at', _p)}",
                (*ids, minutes),
            ).fetchall()
        }
        known = {r[0] for r in db.execute(f"SELECT id FROM memory_items WHERE id IN ({ph})", tuple(ids)).fetchall()}

        bump_helpful, bump_unhelpful = [], []
        for g in grades:
            mid = str(g.get("memory_id") or "")
            verdict = str(g.get("verdict") or "").strip().lower()
            if mid not in known:
                unknown.append(mid)
                continue
            if mid not in fresh:
                rejected.append(mid)
                continue
            if verdict in ("helpful", "up", "useful"):
                bump_helpful.append(mid)
                accepted.append(mid)
            elif verdict in ("unhelpful", "down", "not_useful"):
                bump_unhelpful.append(mid)
                accepted.append(mid)
            else:
                unknown.append(mid)

        for col, batch in (("helpful_count", bump_helpful), ("unhelpful_count", bump_unhelpful)):
            for chunk in batched(batch, 500):
                cph = ", ".join([_p] * len(chunk))
                db.execute(
                    f"UPDATE memory_items SET {col} = COALESCE({col}, 0) + 1 WHERE id IN ({cph})",
                    tuple(chunk),
                )
        # The daily record must not fail the grade it records. A store short of
        # migration 058 skips it, loudly (log + response), and the grade stands.
        record_skipped = False
        if accepted or rejected:
            from memory.db import tolerant_schema

            record_skipped = True
            with tolerant_schema(db):
                _record_grade_day(db, len(accepted), len(rejected))
                record_skipped = False

    _FEEDBACK_WINDOW_REJECTIONS["accepted"] += len(accepted)
    _FEEDBACK_WINDOW_REJECTIONS["rejected"] += len(rejected)

    out = {
        "ok": True,
        "applied": bool(accepted),
        "graded": len(accepted),
        "window_minutes": minutes,
    }
    if requested is not None and requested != minutes:
        # §3: applying a different window than the caller asked for, silently,
        # would make every downstream count mean something other than it says.
        out["window_clamped_from"] = requested
        out["note"] = (
            f"window_minutes={requested} exceeds the {_FEEDBACK_WINDOW_MINUTES_MAX}"
            f" min ceiling; graded against {minutes} min"
        )
    if rejected:
        # §3 evidence levels: say what was OBSERVED and name the knob.
        out["rejected_stale"] = len(rejected)
        out["reason"] = "outside_feedback_window"
        out["observed"] = (
            f"{len(rejected)} memory/ies were not retrieved within the last "
            f"{minutes} min; grade immediately after using them"
        )
        out["inspect"] = "raise `feedback_window_minutes` in .governor_config.json if long turns are being rejected"
    if unknown:
        out["unknown"] = len(unknown)
    if record_skipped:
        out["daily_record"] = _GRADE_RECORD_MISSING
        logger.warning(_GRADE_RECORD_MISSING)
    return out


def memory_feedback_stats_impl():
    """How often graded feedback is landing vs being rejected as stale.

    The 5-minute window is tight by choice. This makes "is it too tight?" a
    measurement rather than a guess — if rejections are a meaningful share of
    grades, widen `feedback_window_minutes` in .governor_config.json. Without
    this the default would be untunable in practice (§5: a knob nobody can
    evaluate is not a knob).

    The top-level counts are this process's (a live question: is the window
    right now?). ``today`` and ``last_7_days`` come from memory_grade_daily, so
    they survive a restart (credit loop part A).
    """
    a = _FEEDBACK_WINDOW_REJECTIONS["accepted"]
    r = _FEEDBACK_WINDOW_REJECTIONS["rejected"]
    total = a + r
    from memory.db import tolerant_schema

    today = week = None
    with _db() as db, tolerant_schema(db):
        today, week = _grade_days(db, 1), _grade_days(db, 7)
    if today is None:  # pre-058 store: say so instead of reporting zeros
        today = week = {"error": _GRADE_RECORD_MISSING}
    return {
        "ok": True,
        "accepted": a,
        "rejected_stale": r,
        "rejection_rate": round(r / total, 4) if total else 0.0,
        "today": today,
        "last_7_days": week,
        "window_minutes": _feedback_window_minutes(),
        "note": (
            "a high rejection rate means the window is too tight for this "
            "deployment's agents, not that the feedback was wrong"
        ),
    }


def memory_feedback_impl(memory_id, feedback="useful"):
    """Record an explicit verdict on a memory. Returns a structured result.

    ⚠ FIXED 2026-09-19: two of the three ADVERTISED verdicts were silently
    discarded. The ToolSpec enum has always been
    ("useful", "not_useful", "misleading"), but the impl branched on `useful` and
    `wrong` -- so `not_useful` and `misleading` fell through both branches and
    returned "Feedback 'x' applied to <id>" having done nothing. A false success
    is a §3 violation exactly like a false alarm: the caller cannot tell that its
    signal was thrown away, so the data is lost AND nobody learns it is lost.

    The three verdicts mean genuinely different things and must act differently:

      useful      -> +importance. The memory did its job.
      misleading  -> -importance AND a contradiction in the corroboration ledger.
                     This is a claim about CORRECTNESS, so it feeds the same
                     evidence trail memory.trust already maintains.
      not_useful  -> records the verdict WITHOUT touching importance. This is a
                     RETRIEVAL MISS, not a memory defect: the memory may be
                     perfectly correct and simply irrelevant to this query.
                     Lowering importance here would degrade good content because
                     the ranker mis-targeted it -- punishing the memory for the
                     search's mistake.

    ⚠ `wrong` used to SOFT-DELETE. It was never in the enum, so a schema-honouring
    client could not reach it, but a CLI caller could -- an undocumented
    destructive path on a tool marked default_allowed=True. It is now an alias for
    `misleading` (penalise, do not delete). Deleting a memory on one negative
    verdict is not recoverable and was never the advertised contract; use
    memory_delete for that.
    """
    fb = (feedback or "").strip().lower()
    if fb == "wrong":  # legacy alias; see docstring
        fb = "misleading"
    if fb not in _FEEDBACK_VERDICTS:
        # Structured, not an exception: the caller passed something the schema
        # does not allow, and needs to know which values ARE allowed.
        return {
            "ok": False,
            "applied": False,
            "memory_id": memory_id,
            "error": "unknown_verdict",
            "observed": f"feedback={feedback!r}",
            "accepted": list(_FEEDBACK_VERDICTS),
        }

    from memory.backends import dialect as _dialect

    _d = _dialect()
    _p = _d.param()

    delta = 0.0
    with _db() as db:
        if fb == "useful":
            # least(1.0, ...) keeps the documented [0,1] ceiling. Unchanged from
            # the original implementation on purpose.
            db.execute(
                f"UPDATE memory_items SET importance = "
                f"{_d.least('1.0', f'importance + {_FEEDBACK_USEFUL_LIFT}')} "
                f"WHERE id = {_p}",
                (memory_id,),
            )
            delta = _FEEDBACK_USEFUL_LIFT
        elif fb == "misleading":
            # greatest(0.0, ...) is the matching floor -- without it a memory
            # graded misleading repeatedly would go negative and then outrank
            # nothing in a way no other code path expects.
            db.execute(
                f"UPDATE memory_items SET importance = "
                f"{_d.greatest('0.0', f'importance - {_FEEDBACK_MISLEADING_DROP}')} "
                f"WHERE id = {_p}",
                (memory_id,),
            )
            delta = -_FEEDBACK_MISLEADING_DROP
        # not_useful: deliberately NO importance write. See docstring.

    return {
        "ok": True,
        "applied": True,
        "memory_id": memory_id,
        "verdict": fb,
        "importance_delta": delta,
        "note": ("recorded; importance unchanged (a retrieval miss is not a memory defect)")
        if fb == "not_useful"
        else "",
    }


def _reinforce_confidence(db):
    """Reinforcement pass (knowledge-maintenance Phase 3): make confidence a
    living signal.

      1. Re-aggregate confidence for memories with corroboration-ledger activity
         (the only ones whose evidence changed) — corroboration raised it,
         contradiction lowered it. Reuses the Phase-2 ledger aggregation.
      2. Decay-toward-neutral for memories that have a confidence but NO recent
         reinforcement (no ledger row, not accessed in the decay window). Unlike
         importance (which decays toward 0), confidence forgets toward
         *uncertainty* (NEUTRAL=0.5) — a fact nobody reconfirmed becomes less
         certain, not worthless. Done as ONE set-based UPDATE (§4): the pure
         decay_toward_neutral() is a linear interpolation toward NEUTRAL, which
         SQL expresses directly.

    ABSENCE-TOLERANT: a pre-035/036 DB (no confidence column / no ledger) makes
    this a no-op. Returns (reaggregated, decayed) counts.
    """
    from memory import confidence as _conf
    from memory import trust as _trust

    reaggregated = 0
    decayed = 0
    # Bind BEFORE the try: if the ledger query below fails with a missing-schema
    # error, the except swallows it (pre-036 DB has no ledger) and execution falls
    # through to the decay block, which reads active_ids — so it must always be
    # defined, or that path raises UnboundLocalError (observed in the cognitive
    # loop's maintenance pass).
    from memory.db import savepoint as _savepoint

    active_ids: set = set()
    try:
        # (1) Re-aggregate the memories with ledger activity. savepoint keeps the
        # txn usable on PG if memory_corroborations is absent (pre-036 DB).
        with _savepoint(db):
            active = db.execute("SELECT DISTINCT memory_id FROM memory_corroborations").fetchall()
            active_ids = {r[0] for r in active}
            for mid in active_ids:
                if _trust.reaggregate_confidence(db, mid) is not None:
                    reaggregated += 1
    except Exception as e:  # noqa: BLE001 — pre-036 DB has no ledger
        if not _is_missing_schema(e):
            raise

    try:
        # (2) Decay un-reinforced memories toward NEUTRAL in one UPDATE.
        # new = c + (NEUTRAL - c) * DECAY_RATE, clamped, skipping rows touched by
        # the ledger (already re-aggregated) and rows accessed in the last 7 days.
        from memory.backends import dialect as _dialect

        _d = _dialect()
        _p = _d.param()
        # Exclude the ledger-active ids via NOT IN. When the set is EMPTY, OMIT the
        # clause entirely — `id NOT IN (NULL)` evaluates to NULL/unknown for every
        # row (SQL three-valued logic), matching NOTHING, which silently disabled
        # the whole decay pass (bug 2026-07-24). An empty set means "exclude
        # nothing", i.e. no clause.
        _not_in = f" AND id NOT IN ({_d.placeholder(len(active_ids))})" if active_ids else ""
        _age7 = _d.age_days_gt("last_accessed_at", "7")
        params = [_conf.NEUTRAL, _conf.DECAY_RATE, *active_ids]
        try:
            with _savepoint(db):
                res = db.execute(
                    f"""
                    UPDATE memory_items
                       SET confidence = {_d.greatest("0.0", _d.least("1.0", f"confidence + ({_p} - confidence) * {_p}"))}
                     WHERE is_deleted = 0
                       AND confidence IS NOT NULL{_not_in}
                       AND (last_accessed_at IS NULL OR {_age7})
                       AND COALESCE(pinned, 0) = 0
                    """,
                    params,
                )
        except Exception as e:  # noqa: BLE001 — pre-pinned-column DB
            if not _is_missing_schema(e):
                raise
            res = db.execute(
                f"""
                UPDATE memory_items
                   SET confidence = {_d.greatest("0.0", _d.least("1.0", f"confidence + ({_p} - confidence) * {_p}"))}
                 WHERE is_deleted = 0
                   AND confidence IS NOT NULL{_not_in}
                   AND (last_accessed_at IS NULL OR {_age7})
                """,
                params,
            )
        decayed = res.rowcount
    except Exception as e:  # noqa: BLE001 — pre-035 DB has no confidence column
        if not _is_missing_schema(e):
            raise

    return reaggregated, decayed


def _is_missing_schema(exc) -> bool:
    """True for the pre-035/036 'no such column/table' errors the reinforcement
    pass tolerates (degrades to a no-op rather than failing maintenance).

    Delegates to the seam: SQLite raises OperationalError('no such column') while
    PostgreSQL raises UndefinedColumn/UndefinedTable with SQLSTATE 42703/42P01.
    dialect().is_undefined_object_error() classifies both from one call so this
    tolerance fires correctly on either backend (not just SQLite's message text)."""
    try:
        from memory.backends import dialect

        return dialect().is_undefined_object_error(exc)
    except Exception:
        # Seam unavailable (raw sqlite fixture) → fall back to the SQLite text match.
        msg = str(exc).lower()
        return "no such column" in msg or "no such table" in msg or "no column named" in msg


def memory_lifecycle_summary_impl(window_days: int = 7, top_n: int = 5) -> dict:
    """Windowed summary of what the memory system did to itself.

    Aggregates two append-only, timestamp-indexed ledgers over the last
    ``window_days`` days:
      * ``memory_history`` (mig 009) — create / update / delete / supersede
        events (``event`` column, indexed on ``created_at``).
      * ``memory_corroborations`` (mig 036) — corroboration (``delta>0``) and
        contradiction (``delta<0``) events.

    Returns a structured dict so an agent can narrate "we updated this belief 3
    times" and an operator can see lifecycle churn at a glance. Read-only; no
    writes, no background job. Both queries hit existing indexes.

    Old-DB tolerance: ``memory_corroborations`` only exists post-036. A missing
    table degrades that section to zero counts (via ``_is_missing_schema``)
    rather than failing the whole summary — mirroring the reinforcement pass.
    """
    window_days = max(1, int(window_days))
    top_n = max(0, int(top_n))
    from memory.backends import dialect as _dialect

    _d = _dialect()
    _p = _d.param()
    # window cutoff is now bound as an INT number of days via now_minus_days
    # (portable), not the SQLite-only "-N days" modifier string.
    _since = _d.now_minus_days(_p)  # e.g. "datetime('now','-'||?||' days')" / PG interval
    out: dict = {
        "window_days": window_days,
        "events": {"create": 0, "update": 0, "delete": 0, "supersede": 0},
        "corroboration": {"corroborated": 0, "contradicted": 0},
        "top_contradicted": [],
        "most_revised": [],
        "grading": {"graded_memories": 0, "helpful_total": 0, "unhelpful_total": 0},
    }
    from memory.db import tolerant_schema as _tolerant

    with _db() as db:
        # NOTE: no `db.row_factory = sqlite3.Row` here — that is SQLite-only and the
        # PG compat connection already yields name-addressable rows. The seam gives
        # row["col"] access on both backends.
        #
        # Each block below queries a table that may not exist on an un-migrated DB
        # (memory_history pre-009, memory_corroborations pre-036). tolerant_schema
        # wraps the block in a SAVEPOINT so a missing-schema error is swallowed AND
        # the outer transaction stays usable on PG — the bare try/except-missing
        # idiom left the PG txn aborted for every later block. Real errors still
        # raise (reraise_real=True default).
        #
        # Lifecycle events by type in the window.
        with _tolerant(db):
            for row in db.execute(
                f"SELECT event, COUNT(*) AS n FROM memory_history WHERE created_at >= {_since} GROUP BY event",
                (window_days,),
            ):
                if row["event"] in out["events"]:
                    out["events"][row["event"]] = row["n"]

        # Most-revised memories (update + supersede events per memory_id).
        if top_n:
            with _tolerant(db):
                out["most_revised"] = [
                    {"memory_id": r["memory_id"], "revisions": r["n"], "title": r["title"]}
                    for r in db.execute(
                        f"SELECT h.memory_id AS memory_id, COUNT(*) AS n, "
                        f"       COALESCE(m.title, '') AS title "
                        f"FROM memory_history h "
                        f"LEFT JOIN memory_items m ON m.id = h.memory_id "
                        f"WHERE h.created_at >= {_since} "
                        f"  AND h.event IN ('update', 'supersede') "
                        f"GROUP BY h.memory_id ORDER BY n DESC LIMIT {_p}",
                        (window_days, top_n),
                    )
                ]

        # Corroboration vs contradiction in the window (post-036 table).
        with _tolerant(db):
            for row in db.execute(
                f"SELECT CASE WHEN delta > 0 THEN 'corroborated' ELSE 'contradicted' END AS kind, "
                f"       COUNT(*) AS n FROM memory_corroborations "
                f"WHERE created_at >= {_since} GROUP BY kind",
                (window_days,),
            ):
                out["corroboration"][row["kind"]] = row["n"]

        # Most-contradicted memories (post-036 table).
        if top_n:
            with _tolerant(db):
                out["top_contradicted"] = [
                    {"memory_id": r["memory_id"], "contradiction_count": r["n"], "title": r["title"]}
                    for r in db.execute(
                        f"SELECT c.memory_id AS memory_id, COUNT(*) AS n, "
                        f"       COALESCE(m.title, '') AS title "
                        f"FROM memory_corroborations c "
                        f"LEFT JOIN memory_items m ON m.id = c.memory_id "
                        f"WHERE c.created_at >= {_since} AND c.delta < 0 "
                        f"GROUP BY c.memory_id ORDER BY n DESC LIMIT {_p}",
                        (window_days, top_n),
                    )
                ]

        # Grading (memory credit loop part A): memories ever graded, the two counters
        # (all time, never netted), and verdicts accepted / rejected in the window.
        with _tolerant(db):
            row = db.execute(
                "SELECT COUNT(*) AS g, COALESCE(SUM(helpful_count), 0) AS h, "
                "COALESCE(SUM(unhelpful_count), 0) AS u FROM memory_items "
                "WHERE COALESCE(helpful_count, 0) + COALESCE(unhelpful_count, 0) > 0"
            ).fetchone()
            out["grading"].update(
                graded_memories=int(row["g"]), helpful_total=int(row["h"]), unhelpful_total=int(row["u"])
            )
        with _tolerant(db):
            out["grading"]["window"] = _grade_days(db, window_days)
    return out


def _enforce_retention_policies(db):
    """Enforce per-agent memory limits and TTLs from agent_retention_policies table.

    Both soft-delete (is_deleted = 1, updated_at, a history event). TTL expiry is
    always archived first (reason ``retention_ttl``); max-count excess is archived
    (reason ``retention_limit``) when the policy's auto_archive is set, and a row
    whose archive write fails is LEFT LIVE rather than deleted without its
    tombstone. Pinned rows are exempt. Rows are kept and restorable by naming the
    reason (memory_restore reason=retention_ttl|retention_limit).

    Returns the rows soft-deleted; 0 only when the policy table is absent."""
    from memory.backends import dialect as _dialect
    from memory.db import _record_history
    from memory.db import savepoint as _savepoint

    _d = _dialect()
    _p = _d.param()
    try:
        with _savepoint(db):
            policies = db.execute("SELECT * FROM agent_retention_policies").fetchall()
    except Exception as e:
        if not _is_missing_schema(e):
            raise
        return 0  # pre-010 store: no policies to enforce
    now = datetime.now(UTC).isoformat()
    purged = 0
    not_archived: list[str] = []

    def _select(agent_id, tail_sql, params):
        # Dual path: prefer the pinned-aware form; fall back without `pinned` on a
        # pre-pinned-column DB (savepoint keeps the PG txn usable for the fallback).
        base = f"SELECT id FROM memory_items WHERE agent_id = {_p} AND is_deleted = 0 "
        try:
            with _savepoint(db):
                return db.execute(base + "AND COALESCE(pinned, 0) = 0 " + tail_sql, (agent_id, *params)).fetchall()
        except Exception as e:  # noqa: BLE001 — pre-pinned-column DB
            if not _is_missing_schema(e):
                raise
            return db.execute(base + tail_sql, (agent_id, *params)).fetchall()

    def _retire(ids, reason, archive):
        nonlocal purged
        for mid in ids:
            if archive and not _transfer_to_archive(mid, reason, db):
                not_archived.append(mid)
                continue
            res = db.execute(
                f"UPDATE memory_items SET is_deleted = 1, updated_at = {_p} WHERE id = {_p} AND is_deleted = 0",
                (now, mid),
            )
            if res.rowcount:
                purged += 1
                _record_history(mid, "delete", None, reason, "is_deleted", "retention_policy", db=db)

    for p in policies:
        agent_id = p["agent_id"]
        if p["ttl_days"] and p["ttl_days"] > 0:
            rows = _select(agent_id, f"AND {_d.age_days_gt('created_at', _p)}", (p["ttl_days"],))
            _retire([r["id"] for r in rows], "retention_ttl", True)
        # Max count enforcement (keep newest, soft-delete oldest excess)
        if p["max_memories"] and p["max_memories"] > 0:
            rows = _select(agent_id, f"ORDER BY created_at DESC {_d.all_rows_after_offset(_p)}", (p["max_memories"],))
            _retire([r["id"] for r in rows], "retention_limit", bool(p["auto_archive"]))
    if not_archived:
        logger.warning(
            "retention: %d memories were due but their archive write failed, so they were left live "
            "(first: %s). Check that memory_archive exists: m3 doctor --fix",
            len(not_archived),
            not_archived[0],
        )
    return purged


def memory_maintenance_impl(
    decay=True, purge_expired=True, prune_orphan_embeddings=True, reinforce=True, prune_orphan_queues=True
):
    import time

    from m3_sdk import _LAST_USER_INTERACTION

    if time.time() - _LAST_USER_INTERACTION < 15.0:
        logger.info("Active query session detected. Suspending curation pass to yield resources.")
        time.sleep(5.0)

    # The decay/retention/archive/refresh SQL below is now routed through the
    # dialect seam (param(), age_days_gt(), all_rows_after_offset(), group_concat(),
    # greatest/least(), is_undefined_object_error()) and every tolerant block uses a
    # savepoint (savepoint()/tolerant_schema()) so a caught missing-schema error no
    # longer aborts the PG txn. The pass is validated end-to-end on live PG by
    # test_memory_maintenance_pg_live — so the non-sqlite SKIP guard is gone. (The
    # earlier "can't adapt type dict" issue lived in memory_import_impl, a SEPARATE
    # function this pass never calls; it is fixed via the json_bind_value seam
    # primitive and covered by its own round-trip test.) VACUUM below is still
    # skipped on PG — that is genuinely SQLite-only, unlike decay/purge/retention.
    now = datetime.now(UTC).isoformat()
    from memory.backends import dialect as _dialect

    _d = _dialect()
    _p = _d.param()
    _age7 = _d.age_days_gt("created_at", "7")  # literal-day form (no bind)
    from memory.db import savepoint as _savepoint

    report = []
    refresh_notified: list[str] = []  # rung after the commit below
    with _db() as db:
        if decay:
            # The floor is a PER-ROW expression, not a constant: a memory the user
            # has graded `helpful` must not decay into the same band as one nobody
            # has ever found useful. Rendered through the seam (greatest/least)
            # because SQLite spells scalar max as MAX() while PostgreSQL's MAX is
            # an AGGREGATE and the scalar is GREATEST() — §10a.
            _floor = _decay_floor_sql(_d)
            _decayed = _d.greatest(_floor, f"importance * {_DECAY_RATE}")
            try:
                # savepoint isolates the pinned-column attempt so its failure on a
                # pre-pinned SQLite DB doesn't abort the txn before the fallback.
                with _savepoint(db):
                    res = db.execute(
                        f"UPDATE memory_items SET importance = {_decayed}, "
                        f"decay_rate = {_DECAY_RATE} "
                        f"WHERE is_deleted = 0 AND {_age7} "
                        f"AND COALESCE(pinned, 0) = 0"
                    )
            except Exception as e:  # noqa: BLE001 — pre-pinned-column DB
                if not _is_missing_schema(e):
                    raise
                # Pre-049 DBs have neither decay_rate nor helpful_count; fall all
                # the way back to the original flat-floor form rather than failing
                # the whole maintenance pass on an old schema.
                res = db.execute(
                    f"UPDATE memory_items SET importance = "
                    f"{_d.greatest('0.0', f'importance * {_DECAY_RATE}')} "
                    f"WHERE is_deleted = 0 AND {_age7}"
                )
            report.append(f"Decayed {res.rowcount} items")
        if reinforce:
            # ⚠ TWO DISTINCT REINFORCEMENTS OVER THE SAME ROWS. Do not merge them.
            #   confidence = "is this TRUE"   -- evidence from the corroboration
            #                                    ledger, decays toward NEUTRAL.
            #   importance = "does this MATTER" -- usage, decays toward a floor.
            # A memory can be certainly true and no longer matter, or urgently
            # relevant and unverified. Collapsing them into one number loses the
            # distinction the whole model rests on.
            i_count = _reinforce_importance(db)
            if i_count:
                report.append(f"Reinforced importance floor on {i_count} items")

            # Confidence reinforcement (Phase 3): re-aggregate ledger-active
            # memories, decay the un-reinforced toward NEUTRAL. No-op on pre-035/
            # 036 DBs. Distinct from importance decay above (toward 0).
            r_count, d_count = _reinforce_confidence(db)
            if r_count or d_count:
                report.append(f"Confidence: reaggregated {r_count}, decayed {d_count} toward neutral")
        if purge_expired:
            # Expiry SOFT-deletes (kept whole and recoverable): GDPR erasure is the
            # only hard delete (memory.deletion). Pinned rows are exempt.
            try:
                with _savepoint(db):
                    res = db.execute(
                        f"UPDATE memory_items SET is_deleted = 1, updated_at = {_p} "
                        f"WHERE expires_at < {_p} AND is_deleted = 0 AND COALESCE(pinned, 0) = 0",
                        (now, now),
                    )
            except Exception as e:  # noqa: BLE001 — pre-pinned-column DB
                if not _is_missing_schema(e):
                    raise
                res = db.execute(
                    f"UPDATE memory_items SET is_deleted = 1, updated_at = {_p} WHERE expires_at < {_p} AND is_deleted = 0",
                    (now, now),
                )
            report.append(f"Soft-deleted {res.rowcount} expired")
        if prune_orphan_embeddings:
            res = db.execute("DELETE FROM memory_embeddings WHERE memory_id NOT IN (SELECT id FROM memory_items)")
            report.append(f"Pruned {res.rowcount} orphans")
        # Read-access log retention (audit.read_log_retention_days, default 90; user
        # decision 2026-10-11). Guarded: the table is absent on a store below 053.
        try:
            with _savepoint(db):
                res = db.execute(
                    f"DELETE FROM memory_access_log WHERE {_d.age_days_gt('ts', _p)}",
                    (int(_m3cfg.setting("audit.read_log_retention_days")),),
                )
            report.append(f"Pruned {res.rowcount} read-log rows past retention")
        except Exception as e:  # noqa: BLE001 — pre-053 store
            if not _is_missing_schema(e):
                raise
        if prune_orphan_queues:
            # Reap entity_extraction_queue rows whose target memory is GONE or
            # SOFT-DELETED. The extraction worker only processes LIVE memories
            # (is_deleted=0), so a row pointing at a deleted memory can NEVER be
            # processed — it lingers forever, inflating the "pending" count. A row
            # is orphaned if its memory_id has no LIVE row in memory_items.
            #
            # Scoped to entity_extraction_queue only: it is the memory-keyed queue.
            # observation_queue / reflector_queue are CONVERSATION-keyed (no
            # memory_id column) — a different shape, out of scope here. Best-effort:
            # a missing table/column on an older schema is skipped, never fatal.
            try:
                with _savepoint(db):
                    res = db.execute(
                        "DELETE FROM entity_extraction_queue WHERE memory_id NOT IN "
                        "(SELECT id FROM memory_items WHERE COALESCE(is_deleted,0)=0)"
                    )
                if res.rowcount:
                    report.append(f"Reaped {res.rowcount} orphaned entity-queue row(s)")
            except Exception as e:  # noqa: BLE001 — missing table/column on old schema
                if not _is_missing_schema(e):
                    raise

        # Low-importance memories older than 30 days: SURFACED, NOT DELETED.
        #
        # ⚠ THIS PASS USED TO SOFT-DELETE. It selected `importance < 0.05` and set
        # is_deleted = 1 on every match -- 5,001 memories were soft-deleted this way
        # before the behaviour was changed. An autonomous background sweep does
        # not get to decide a deletion; forgetting is deranking, and removal is a
        # decision a human or an explicitly-invoked tool makes.
        #
        # ⚠ IT WAS ALSO A HIDDEN COUPLING. The 0.05 literal here silently tracked
        # the decay floor: while the floor was also 0.05, decay asymptoted exactly
        # AT this line and nothing ever crossed it, so the delete never fired.
        # Lowering the floor to 0.01 would have re-opened it against 3,742 aged
        # rows -- a tuning change to one constant silently re-enabling deletion
        # governed by another. Two constants, no relationship expressed anywhere.
        #
        # Now it reports a CANDIDATE COUNT and nothing else. Decay already
        # deranks these rows continuously (importance feeds the score directly),
        # which is the whole mechanism for making stale memories fade. They stay
        # retrievable by content, and `memory_maintenance` surfaces how many are
        # sitting at the bottom so a human can act if they want to.
        candidates = db.execute(
            f"SELECT COUNT(*) AS n FROM memory_items WHERE is_deleted = 0 "
            f"AND importance < {_ARCHIVE_CANDIDATE_THRESHOLD} "
            f"AND {_d.age_days_gt('created_at', '30')}"
        ).fetchone()
        n_candidates = (candidates["n"] if candidates else 0) or 0
        if n_candidates:
            report.append(
                f"{n_candidates} low-importance item(s) older than 30d are archive "
                f"CANDIDATES (not removed; run memory_delete or the curator to act)"
            )

        # Enforce agent retention policies
        retention_purged = _enforce_retention_policies(db)
        if retention_purged:
            report.append(f"Retention policies: purged {retention_purged} items")

        # Refresh queue: count memories whose refresh_on has arrived, and emit
        # one push notification per distinct agent with newly-due memories.
        # - Maintenance never mutates refresh flags (that's memory_update's job).
        # - Dedup against existing unacked refresh_due notifications so repeated
        #   maintenance runs don't flood the channel with duplicates.
        # savepoint: on PG a failure anywhere in this block (missing refresh_on
        # column on a very old DB, or the notifications insert) would otherwise
        # abort the whole maintenance txn — the broad except below logs it but
        # cannot un-poison the connection. The savepoint re-raises into the except,
        # leaving the txn alive so ANALYZE below still runs.
        try:
            with _savepoint(db):
                refresh_due = db.execute(
                    f"SELECT COUNT(*) FROM memory_items "
                    f"WHERE is_deleted = 0 AND refresh_on IS NOT NULL AND refresh_on <= {_p}",
                    (now,),
                ).fetchone()[0]
                if refresh_due:
                    report.append(
                        f"Refresh queue: {refresh_due} memor{'y' if refresh_due == 1 else 'ies'} due for review"
                    )

                    # Fan-out notifications by agent_id. NULL/empty agent_ids are
                    # skipped — notifications require a real agent_id.
                    agent_rows = db.execute(
                        f"SELECT agent_id, COUNT(*) as n, {_d.group_concat('id')} as ids "
                        f"FROM memory_items "
                        f"WHERE is_deleted = 0 AND refresh_on IS NOT NULL AND refresh_on <= {_p} "
                        f"  AND agent_id IS NOT NULL AND agent_id != '' "
                        f"GROUP BY agent_id",
                        (now,),
                    ).fetchall()

                    notified = 0
                    for ar in agent_rows:
                        aid = ar["agent_id"]
                        # Dedup: skip if this agent already has an unacked refresh_due notif
                        existing = db.execute(
                            f"SELECT 1 FROM notifications "
                            f"WHERE agent_id = {_p} AND kind = 'refresh_due' AND read_at IS NULL LIMIT 1",
                            (aid,),
                        ).fetchone()
                        if existing:
                            continue
                        sample = (ar["ids"] or "").split(",")[:3]
                        payload = json.dumps({"count": ar["n"], "sample_ids": sample})
                        db.execute(
                            f"INSERT INTO notifications (agent_id, kind, payload_json, created_at) "
                            f"VALUES ({_p}, 'refresh_due', {_p}, {_p})",
                            (aid, payload, now),
                        )
                        refresh_notified.append(aid)
                        notified += 1
                    if notified:
                        report.append(f"Refresh queue: notified {notified} agent(s)")
        except Exception as e:
            # refresh_on column may not exist on very old DBs that haven't run v014
            log_swallowed(logger, e, f"refresh queue check skipped: {e}")

        db.execute("ANALYZE")
        report.append("Statistics updated (ANALYZE)")

    # Storage compaction is delegated to the dialect via compact_storage() — NOT
    # gated on `resolve_backend_name() != "sqlite"` here. That call-site branch was
    # a latent bug: a third backend with a real client-issued compaction (MariaDB
    # OPTIMIZE TABLE, or a future store) would fall into the PG no-op side and never
    # compact. The seam owns the decision: SQLite VACUUMs (size-gated, out of txn),
    # PG returns its autovacuum no-op line, any future backend does its own thing.
    # The active SQLite path (may differ from a constant when a caller switched DBs
    # via active_database()/M3_DATABASE) is passed for the SQLite path; other
    # backends ignore it.
    try:
        active_path = memory_core._current_ctx().db_path
    except Exception:
        active_path = None
    report.append(_d.compact_storage(sqlite_path=active_path))

    # The refresh notices committed with the block above; wake their waiters.
    if refresh_notified:
        from memory.orchestration import _ring_doorbell

        for aid in refresh_notified:
            _ring_doorbell(aid)

    return "Maintenance complete:\n" + "\n".join(report)


def gdpr_export_impl(user_id: str) -> str:
    """Export everything held about a data subject (GDPR Art. 15 / Art. 20).

    Includes the RETAINED copies an erasure would remove, not only live rows:
    soft-deleted and superseded memories (``is_deleted``/``valid_to`` say which),
    fact_enriched rows derived from them, their history, archived tombstones, the
    read-access log, and the subject's rows in a separate chatlog store. A table
    this store does not have is named in ``missing_tables`` rather than read as
    "nothing held". The request is logged in gdpr_requests; ``request_logged``
    says whether that landed. Routed through the seam, so it runs on every backend."""
    import json
    import uuid

    if not user_id or not user_id.strip():
        return "Error: user_id is required"
    from memory.db import flush_read_log

    flush_read_log()  # queued reads are part of the record
    from memory.backends import dialect

    _d = dialect()
    _p = _d.param()
    T = _gdpr_tables("sqlite")
    missing: list[str] = []

    def _rows(db, table, sql, params):
        cur = _gdpr_try(db, sql, params)
        if cur is None:
            missing.append(table)
            return []
        return [dict(r) for r in cur.fetchall()]

    cols = "id, type, title, content, metadata_json, agent_id, importance, created_at, updated_at, is_deleted, valid_to"
    with _db() as db:
        items = _rows(db, "memory_items", f"SELECT {cols} FROM memory_items WHERE user_id = {_p}", (user_id,))
        derived = _gdpr_derived_ids(db, _d, [r["id"] for r in items], T)
        for c in batched(derived, _GDPR_CHUNK):
            items += _rows(
                db, "memory_items", f"SELECT {cols} FROM memory_items WHERE id IN ({_d.placeholder(len(c))})", list(c)
            )
        ids = [r["id"] for r in items]
        history: list[dict] = []
        for c in batched(ids, _GDPR_CHUNK):
            history += _rows(
                db,
                "memory_history",
                "SELECT memory_id, event, prev_value, new_value, field, actor_id, created_at FROM memory_history "
                f"WHERE memory_id IN ({_d.placeholder(len(c))}) ORDER BY created_at",
                list(c),
            )
        archive = _rows(
            db,
            "memory_archive",
            f"SELECT id, type, title, content, agent_id, archive_reason, archived_at FROM memory_archive WHERE user_id = {_p}",
            (user_id,),
        )
        # Who read this subject's memories, and when (Art. 30 / Art. 15).
        access = _rows(
            db,
            "memory_access_log",
            f"SELECT memory_id, agent_id, op, ts FROM memory_access_log WHERE user_id = {_p} ORDER BY ts",
            (user_id,),
        )
        chat: list[dict] = []
        factory, CT, _full, topology = _gdpr_chatlog_store()
        if factory is not None:
            with _gdpr_store_txn(factory) as cconn:
                chat = _rows(
                    cconn,
                    f"chatlog:{CT['items']}",
                    f"SELECT id, type, title, content, created_at, is_deleted FROM {CT['items']} WHERE user_id = {_p}",
                    (user_id,),
                )

        req_id: str | None = str(uuid.uuid4())
        request_logged = (
            _gdpr_try(
                db,
                "INSERT INTO gdpr_requests (id, subject_id, request_type, status, items_affected, completed_at) "
                f"VALUES ({_p}, {_p}, 'export', 'completed', {_p}, {_d.now()})",
                (req_id, user_id, len(items) + len(chat)),
            )
            is not None
        )
        if not request_logged:
            req_id = None
            logger.warning(
                "gdpr_export for a subject (%d items): not logged, gdpr_requests is missing; run m3 doctor --fix",
                len(items),
            )

    return json.dumps(
        {
            "user_id": user_id,
            "request_id": req_id,
            "request_logged": request_logged,
            "items_count": len(items),
            "live_count": sum(1 for r in items if not r.get("is_deleted")),
            "items": items,
            "derived_ids": derived,
            "history": history,
            "archive": archive,
            "access_log": access,
            "chatlog_store": topology,
            "chatlog_items": chat,
            "missing_tables": sorted(set(missing)),
        },
        indent=2,
        default=str,
    )


_GDPR_PLAN_TTL_S = 900  # a plan's token is good for 15 minutes
_GDPR_REQUIRED_COMPLIANCE = ("legal_basis", "verified_by")
_GDPR_CHUNK = 500  # explicit memory_ids per statement, well under every backend's bind limit
# A per-table plan count ORs that table's statements into one COUNT (a row two
# statements match is counted once) while the binds fit; past this it sums them
# and reports the table in `counts_upper_bound`.
_GDPR_COUNT_BINDS = 30000
# Main-store-only tables erased by memory id (and, for a data subject, by user_id).
# The tables the chatlog store shares are named by ROLE (chatlog_table_for).
_GDPR_BY_MEMORY = (
    ("memory_history", "memory_id", None),
    ("memory_access_log", "memory_id", "user_id"),
    ("bypass_surface", "memory_id", "user_id"),
    ("memory_archive", "id", "user_id"),
    ("fact_enrichment_queue", "memory_id", None),
)
# Main-store-only tables keyed by an erased entity id.
_GDPR_BY_ENTITY = (
    ("entity_embeddings", ("entity_id",)),
    ("entity_coalesce_embeddings", ("entity_id",)),
    ("entity_coalesce_candidates", ("entity_a", "entity_b")),
)
_GDPR_ROLES = ("items", "embeddings", "relationships", "entities", "item_entities", "entity_rel", "extraction_queue")
_LG_OF_USER = "SELECT thread_id, checkpoint_ns, checkpoint_id FROM m3_lg_checkpoints WHERE user_id = {p}"


def _gdpr_tables(backend_name) -> dict:
    """Role -> physical table, from the one owner of the chatlog table names."""
    from memory.backends.dialect import chatlog_table_for

    return {role: chatlog_table_for(role, backend_name) for role in _GDPR_ROLES}


def _gdpr_statements(_d, user_id, item_ids, *, extra_ids=(), entity_ids=(), T=None, full=True):
    """Every (table, where, params) an erasure touches, dependents before the items
    table. ONE list feeds the plan's counts and the erasure, so they cannot
    disagree. A data subject's memories are named by ONE subquery (one bind, however
    many there are); explicit ids go in chunks, so no statement exceeds a backend's
    bind limit. The items table is deleted by user_id, not by a subquery on itself
    (MariaDB rejects that form).

    ``extra_ids``: derived rows a subject's user_id predicate does not reach (see
    _gdpr_derived_ids). ``entity_ids``: entities only erased memories reference.
    ``T``: role -> table (main store by default); ``full`` adds the main-store-only
    tables. Every dependent is named explicitly — never left to an FK cascade, which
    an older store or a backend with FKs off does not fire."""
    p = _d.param()
    T = T or _gdpr_tables("sqlite")
    explicit = list(extra_ids) if user_id else list(item_ids)
    sets = []
    if user_id:
        sets.append((f"(SELECT id FROM {T['items']} WHERE user_id = {p})", [user_id]))
    sets += [(f"({_d.placeholder(len(c))})", list(c)) for c in batched(explicit, _GDPR_CHUNK)]
    out = []
    by_memory = [(T["embeddings"], "memory_id", None)]
    if full:
        by_memory += list(_GDPR_BY_MEMORY)
    by_memory += [(T["extraction_queue"], "memory_id", None), (T["item_entities"], "memory_id", None)]
    for table, id_col, user_col in by_memory:
        for i, (expr, params) in enumerate(sets):
            where, args = f"{id_col} IN {expr}", list(params)
            if user_id and user_col and i == 0:
                where, args = f"{where} OR {user_col} = {p}", [*args, user_id]
            out.append((table, where, args))
    for expr, params in sets:
        # A relationship fact sourced from an erased memory is its content.
        out.append((T["entity_rel"], f"source_memory_id IN {expr}", list(params)))
        if full:
            # No FK: a corroboration names the memory it backs, and source_ref the
            # memory that backed it.
            out.append(("memory_corroborations", f"memory_id IN {expr} OR source_ref IN {expr}", [*params, *params]))
    for c in batched(entity_ids, _GDPR_CHUNK):
        ph, ids = _d.placeholder(len(c)), list(c)
        out.append((T["entity_rel"], f"from_entity IN ({ph}) OR to_entity IN ({ph})", [*ids, *ids]))
        if full:
            for table, cols in _GDPR_BY_ENTITY:
                out.append((table, " OR ".join(f"{col} IN ({ph})" for col in cols), ids * len(cols)))
        out.append((T["item_entities"], f"entity_id IN ({ph})", ids))
        out.append((T["entities"], f"id IN ({ph})", ids))
    for expr, params in sets:
        out.append((T["relationships"], f"from_id IN {expr} OR to_id IN {expr}", [*params, *params]))
    if user_id and full:
        out.append(
            ("m3_lg_writes", f"(thread_id, checkpoint_ns, checkpoint_id) IN ({_LG_OF_USER.format(p=p)})", [user_id])
        )
        out.append(("m3_lg_checkpoints", f"user_id = {p}", [user_id]))
    if user_id:
        out.append((T["items"], f"user_id = {p}", [user_id]))
        sets = sets[1:]
    out += [(T["items"], f"id IN {expr}", params) for expr, params in sets]
    return out


def _gdpr_derived_ids(db, _d, item_ids, T) -> list[str]:
    """fact_enriched rows derived from an erased memory (a `references` edge from
    the fact to its source) that no other subject owns. The enricher wrote them
    without a user_id, so a subject's user_id predicate alone leaves them behind."""
    erased, out = set(item_ids), set()
    for c in batched(item_ids, _GDPR_CHUNK):
        cur = _gdpr_try(
            db,
            f"SELECT c.id AS id, c.user_id AS cu, s.user_id AS su FROM {T['relationships']} r "
            f"JOIN {T['items']} c ON c.id = r.from_id JOIN {T['items']} s ON s.id = r.to_id "
            f"WHERE r.relationship_type = 'references' AND c.type = 'fact_enriched' "
            f"AND r.to_id IN ({_d.placeholder(len(c))})",
            list(c),
        )
        if cur is None:
            return []
        for r in cur.fetchall():
            if r["id"] not in erased and (r["cu"] or "") in ("", r["su"] or ""):
                out.add(r["id"])
    return sorted(out)


def _gdpr_orphan_entities(db, _d, item_ids, T) -> list[str]:
    """Entities that ONLY erased memories reference: an entity a surviving memory
    mentions, or that a relationship sourced from a surviving memory names, is
    another subject's data too and is kept."""
    erased, cands = set(item_ids), set[str]()
    for c in batched(item_ids, _GDPR_CHUNK):
        cur = _gdpr_try(
            db,
            f"SELECT DISTINCT entity_id FROM {T['item_entities']} WHERE memory_id IN ({_d.placeholder(len(c))})",
            list(c),
        )
        if cur is None:
            return []
        cands.update(r["entity_id"] for r in cur.fetchall())
    keep = set()
    for c in batched(sorted(cands), _GDPR_CHUNK):
        ph, ids = _d.placeholder(len(c)), list(c)
        for r in db.execute(
            f"SELECT entity_id, memory_id FROM {T['item_entities']} WHERE entity_id IN ({ph})", ids
        ).fetchall():
            if r["memory_id"] not in erased:
                keep.add(r["entity_id"])
        cur = _gdpr_try(
            db,
            f"SELECT from_entity, to_entity, source_memory_id FROM {T['entity_rel']} "
            f"WHERE (from_entity IN ({ph}) OR to_entity IN ({ph})) AND source_memory_id IS NOT NULL",
            [*ids, *ids],
        )
        for r in cur.fetchall() if cur is not None else []:
            if r["source_memory_id"] not in erased:
                keep.update((r["from_entity"], r["to_entity"]))
    return sorted(cands - keep)


def _gdpr_count(db, statements) -> tuple[dict, list]:
    """Rows each table loses: (counts, tables whose count is an upper bound)."""
    parts: dict[str, list] = {}
    for table, where, params in statements:
        parts.setdefault(table, []).append((where, params))
    counts: dict[str, int] = {}
    upper: list[str] = []
    for table, ps in parts.items():
        binds = sum(len(a) for _, a in ps)
        groups = [ps] if binds <= _GDPR_COUNT_BINDS else [[x] for x in ps]
        if len(groups) > 1:
            upper.append(table)
        for g in groups:
            where = " OR ".join(f"({w})" for w, _ in g)
            cur = _gdpr_try(db, f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", [a for _, args in g for a in args])
            if cur is None:
                break
            counts[table] = counts.get(table, 0) + int(cur.fetchone()["n"])
    return counts, upper


def _gdpr_try(db, sql, params):
    """Run one statement in a savepoint; None ONLY when its table is absent (an
    older store: nothing there). Any other failure raises: an erasure that skips
    a table it could not write would leave personal data behind, silently."""
    from memory.db import savepoint

    try:
        with savepoint(db):
            return db.execute(sql, params)
    except Exception as e:
        if not _is_missing_schema(e):
            raise
        return None


def _gdpr_plan(db, _d, user_id, memory_ids, T=None, full=True) -> dict:
    """What an erasure of this target removes from ONE store: the item ids (owned
    plus derived), the statements, and the rows each table loses."""
    _p = _d.param()
    T = T or _gdpr_tables("sqlite")
    if user_id:
        cur = _gdpr_try(db, f"SELECT id FROM {T['items']} WHERE user_id = {_p}", (user_id,))
        rows = cur.fetchall() if cur is not None else []
    else:
        rows = []
        for c in batched(memory_ids, _GDPR_CHUNK):
            cur = _gdpr_try(db, f"SELECT id FROM {T['items']} WHERE id IN ({_d.placeholder(len(c))})", list(c))
            rows += cur.fetchall() if cur is not None else []
    owned = sorted(r["id"] for r in rows)
    derived = _gdpr_derived_ids(db, _d, owned, T)
    item_ids = sorted({*owned, *derived})
    entity_ids = _gdpr_orphan_entities(db, _d, item_ids, T)
    statements = _gdpr_statements(
        _d, user_id, item_ids, extra_ids=derived if user_id else (), entity_ids=entity_ids, T=T, full=full
    )
    counts, upper = _gdpr_count(db, statements)
    return {
        "item_ids": item_ids,
        "derived_ids": derived,
        "entity_ids": entity_ids,
        "statements": statements,
        "counts": counts,
        "upper": upper,
    }


def _gdpr_chatlog_store():
    """(connection factory, role tables, full, topology) for the chatlog store when
    the main-store statements do NOT already reach its rows; (None, ..., topology)
    when they do or it holds nothing.

    - files backend, chatlog file == main file ("unified"): its rows are main rows.
    - files backend, a second file ("separate"): same schema, full statement set.
    - server backend ("chat_log_tables"): chat_log_* tables in the same database.
    """
    from memory.backends import active_backend
    from memory.db import _current_ctx_local

    backend = active_backend()
    ctx_ = _current_ctx_local()
    if not backend.stores_databases_as_files:
        return ctx_.get_chatlog_conn, _gdpr_tables(backend.name), False, "chat_log_tables"
    import chatlog_config
    from m3_core.paths import canonical_path

    path = chatlog_config.chatlog_db_path()
    if canonical_path(path) == canonical_path(ctx_.db_path):
        return None, None, False, "unified"
    if not os.path.exists(path):
        return None, None, False, "separate (no file)"
    return ctx_.get_chatlog_conn, _gdpr_tables("sqlite"), True, "separate"


def _gdpr_plan_chatlog(_d, user_id, memory_ids) -> dict:
    """The chatlog store's share of an erasure plan (see _gdpr_chatlog_store)."""
    factory, T, full, topology = _gdpr_chatlog_store()
    if factory is None:
        return {"topology": topology, "item_ids": [], "counts": {}, "upper": [], "statements": []}
    with _gdpr_store_txn(factory) as conn:
        plan = _gdpr_plan(conn, _d, user_id, memory_ids, T=T, full=full)
    return {**plan, "topology": topology, "factory": factory}


def _gdpr_digest(user_id, memory_ids, item_ids) -> str:
    import hashlib
    import json as _json

    body = _json.dumps({"user_id": user_id, "memory_ids": sorted(memory_ids or []), "items": item_ids}, sort_keys=True)
    # Binds a confirmation to its plan; not a security control (FIPS boundary).
    return hashlib.sha256(body.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def _gdpr_digest_ids(plan: dict, chat: dict) -> list[str]:
    """Every row id a confirmation is bound to, across both stores."""
    return [*plan["item_ids"], *(f"chatlog:{i}" for i in chat["item_ids"])]


class _gdpr_store_txn:
    """Commit a second store's connection on clean exit, roll it back on error:
    the chatlog pool hands out a raw connection that does neither."""

    def __init__(self, factory):
        self._cm = factory()

    def __enter__(self):
        self._conn = self._cm.__enter__()
        return self._conn

    def __exit__(self, et, ev, tb):
        try:
            if et is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._cm.__exit__(et, ev, tb)
        return False


def _gdpr_ids_arg(memory_ids) -> list[str]:
    if isinstance(memory_ids, str):
        memory_ids = memory_ids.split(",")
    return [str(i).strip() for i in (memory_ids or []) if str(i).strip()]


def _gdpr_copies_elsewhere() -> list[dict]:
    """Copies of this store that gdpr_forget does not reach: with a warehouse
    configured, sync has copied rows to it (and from it to other machines)."""
    try:
        from m3_sdk import resolve_warehouse_dsn

        configured = bool(resolve_warehouse_dsn())
    except Exception as e:  # noqa: BLE001 -- unknown is reported, never assumed empty
        return [
            {
                "where": "unknown",
                "why": f"could not tell whether a warehouse is configured ({type(e).__name__}: {e})",
                "inspect": "m3 doctor",
            }
        ]
    if not configured:
        return []
    return [
        {
            "where": "the configured warehouse, and every machine that syncs with it",
            "why": "gdpr_forget erases this machine's stores; sync copied these rows elsewhere and does not erase them",
            "inspect": "erase on each machine that syncs, and remove the rows from the warehouse",
        }
    ]


def gdpr_forget_impl(
    user_id: str = "",
    compliance: "dict | str | None" = None,
    memory_ids: "list[str] | str | None" = None,
    confirm: str = "",
) -> "dict | str":
    """Right to be forgotten (GDPR Article 17): the ONLY hard delete in m3.

    Two steps, so an erasure is never one careless call:

    1. **Plan** — ``gdpr_forget(user_id=...)`` for a data subject, or
       ``gdpr_forget(memory_ids=[...])`` for specific memories. Erases NOTHING;
       returns what would be erased, table by table, and a token valid for 15
       minutes. The plan is recorded in ``gdpr_requests`` (status ``planned``).
    2. **Confirm** — the same call plus ``confirm=<token>`` and ``compliance``
       holding at least ``legal_basis`` and ``verified_by``. Refused when the token
       is unknown, used or expired, or when the memories to erase changed since
       the plan (run step 1 again).

    Every other delete in m3 is a soft delete (memory.deletion).

    The erasure covers the items and everything that holds their content or
    points at them: embeddings, relationships, history, the read-access log,
    bypass-surface rows, archived copies, corroborations, queue rows, entity
    mentions, relationship facts sourced from them, entities ONLY erased memories
    reference (an entity a surviving memory mentions is kept), fact_enriched rows
    derived from them, the same rows in a separate chatlog store, and (for a data
    subject) their LangGraph checkpoints. Wiki syntheses derived from an erased
    member are restricted and the compile ledger's cached membership is flushed
    (Art. 17 derived content); a hook or audit failure is returned in ``failures``
    with ``complete`` False.

    ``compliance`` is the operator's record of the erasure's program-layer context
    (Art. 5(2) accountability): ``legal_basis`` (Art. 17(1) ground), ``reason``,
    ``verified_by`` / ``verification_method``, ``authorized_by``, ``external_ref``,
    ``retained_note`` (Art. 17(3) exemption). Stored verbatim in the tamper-evident
    audit trail, never surfaced elsewhere. It does NOT make m3 a DSAR platform —
    program-level record-keeping is the operator's (docs/COMPLIANCE.md). A dict or
    a JSON string; unknown keys are kept.
    """
    import json as _json
    import time
    import uuid

    user_id = (user_id or "").strip()
    ids_requested = _gdpr_ids_arg(memory_ids)
    if bool(user_id) == bool(ids_requested):
        return "Error: pass exactly one of user_id (a data subject) or memory_ids (specific memories)"
    if isinstance(compliance, str):
        try:
            compliance = _json.loads(compliance) if compliance.strip() else None
        except ValueError:
            compliance = {"note": compliance}  # keep raw text rather than lose it
    if compliance is not None and not isinstance(compliance, dict):
        compliance = None

    # Land queued audit rows BEFORE the write transaction (a flush inside it would
    # wait on that lock), so they are counted and erased below too.
    from memory.db import flush_read_log

    flush_read_log()
    from memory.backends import dialect

    _d = dialect()
    _p = _d.param()
    subject = user_id or f"memory_ids:{len(ids_requested)}"

    if not confirm:
        chat = _gdpr_plan_chatlog(_d, user_id, ids_requested)
        with _db() as db:
            plan = _gdpr_plan(db, _d, user_id, ids_requested)
            item_ids = plan["item_ids"]
            req_id = str(uuid.uuid4())
            exp = int(time.time()) + _GDPR_PLAN_TTL_S
            if (
                _gdpr_try(
                    db,
                    "INSERT INTO gdpr_requests (id, subject_id, request_type, status, items_affected) "
                    f"VALUES ({_p}, {_p}, 'forget', 'planned', {_p})",
                    (req_id, subject, len(item_ids) + len(chat["item_ids"])),
                )
                is None
            ):
                return (
                    "Error: cannot record the erasure plan: the gdpr_requests table is missing from this store. "
                    "inspect: run the migrations (m3 doctor --fix)"
                )
        token = f"{req_id}.{exp}.{_gdpr_digest(user_id, ids_requested, _gdpr_digest_ids(plan, chat))}"
        return {
            "step": "plan",
            "erased": False,
            "target": {"user_id": user_id} if user_id else {"memory_ids": ids_requested},
            "counts": plan["counts"],
            "chatlog_store": chat["topology"],
            "chatlog_counts": chat["counts"],
            "counts_upper_bound": sorted({*plan["upper"], *(f"chatlog:{t}" for t in chat["upper"])}),
            "derived_ids": plan["derived_ids"],
            "entities_erased": len(plan["entity_ids"]) + len(chat.get("entity_ids", [])),
            "not_found": sorted(set(ids_requested) - set(item_ids) - set(chat["item_ids"])),
            "copies_remaining": _gdpr_copies_elsewhere(),  # what confirming will not reach
            "token": token,
            "expires_at": datetime.fromtimestamp(exp, UTC).isoformat(),
            "requires": list(_GDPR_REQUIRED_COMPLIANCE),
            "next": (
                "nothing is erased yet. To erase, call gdpr_forget again with the same target, "
                f"confirm='{token}', and compliance holding {' and '.join(_GDPR_REQUIRED_COMPLIANCE)}."
            ),
        }

    # ── step 2: confirm ────────────────────────────────────────────────────────
    missing = [k for k in _GDPR_REQUIRED_COMPLIANCE if not str((compliance or {}).get(k, "")).strip()]
    if missing:
        return f"Error: an erasure needs a compliance record; missing: {', '.join(missing)}. Nothing was erased."
    try:
        req_id, exp_s, digest = confirm.strip().split(".")
        exp = int(exp_s)
    except ValueError:
        return "Error: confirm is not a token from gdpr_forget's plan step. Nothing was erased."
    if time.time() > exp:
        return (
            f"Error: the plan's token expired at {datetime.fromtimestamp(exp, UTC).isoformat()} "
            f"(request {req_id}); run gdpr_forget without confirm for a new plan. Nothing was erased."
        )

    with _db() as db:
        cur = _gdpr_try(
            db,
            f"SELECT status, subject_id, items_affected FROM gdpr_requests WHERE id = {_p} AND request_type = 'forget'",
            (req_id,),
        )
        row = cur.fetchone() if cur is not None else None
        if row is None or row["status"] != "planned" or row["subject_id"] != subject:
            observed = (
                "no such request"
                if row is None
                else f"status={row['status']}, planned for {row['subject_id']!r} (this call: {subject!r})"
            )
            return (
                f"Error: no open erasure plan for request {req_id} ({observed}). "
                "Run gdpr_forget without confirm for a new plan. Nothing was erased."
            )
        plan = _gdpr_plan(db, _d, user_id, ids_requested)
        chat = _gdpr_plan_chatlog(_d, user_id, ids_requested)
        item_ids, counts = plan["item_ids"], plan["counts"]
        if _gdpr_digest(user_id, ids_requested, _gdpr_digest_ids(plan, chat)) != digest:
            return (
                f"Error: the memories to erase changed since the plan (request {req_id}: planned "
                f"{row['items_affected']}, now {len(item_ids) + len(chat['item_ids'])}); run gdpr_forget "
                "without confirm for a new plan. Nothing was erased."
            )

        # WIKI hooks (Art. 17 derived content), BEFORE the relationship delete
        # removes the `consolidates` edges they read. Each runs in a savepoint so a
        # failure cannot abort the erasure's transaction (PG), and is REPORTED in the
        # result (`failures`, complete=False) -- a synthesis left rendering after its
        # source is erased is a compliance breach the operator must see.
        failures: list[dict] = []
        hooks: dict[str, object] = {}
        if item_ids:
            from memory.db import savepoint

            _bin = os.path.dirname(os.path.abspath(__file__))
            import sys as _sys

            if _bin not in _sys.path:
                _sys.path.insert(0, _bin)
            for name, module, func, consequence in (
                (
                    "wiki_restricted",
                    "wiki.erasure",
                    "restrict_derived_on_erasure",
                    "syntheses derived from the erased memories may still render",
                ),
                (
                    "wiki_ledger_flush",
                    "wiki.ledger",
                    "scan_and_flush_on_erasure",
                    "a wiki cluster membership cache may still hold an erased id",
                ),
            ):
                try:
                    import importlib

                    hook = getattr(importlib.import_module(module), func)
                    with savepoint(db):
                        hooks[name] = hook(db, _d, item_ids, timestamp=datetime.now(UTC).isoformat())
                except Exception as e:  # noqa: BLE001 -- reported in the result, never swallowed
                    logger.warning(
                        "gdpr_forget request %s: %s.%s failed (%s: %s); %s. The erasure proceeds; "
                        "inspect the wiki for content derived from these %d memories.",
                        req_id,
                        module,
                        func,
                        type(e).__name__,
                        e,
                        consequence,
                        len(item_ids),
                    )
                    failures.append({"step": name, "error": f"{type(e).__name__}: {e}", "consequence": consequence})

        # Explicit enumeration, never reliance on a cascade firing; the same
        # statements the plan counted. Optional tables absent on an older store
        # are skipped (_gdpr_try); any other failure aborts the whole erasure.
        for table, where, params in plan["statements"]:
            _gdpr_try(db, f"DELETE FROM {table} WHERE {where}", params)
        # The chatlog store commits just before the main store does: a failure in
        # either raises here and the main transaction rolls back (nothing erased
        # there; the plan stays open for a retry).
        if chat.get("factory") is not None:
            with _gdpr_store_txn(chat["factory"]) as cconn:
                for table, where, params in chat["statements"]:
                    _gdpr_try(cconn, f"DELETE FROM {table} WHERE {where}", params)
        status = "completed" if not failures else "completed_with_failures"
        db.execute(
            f"UPDATE gdpr_requests SET status = {_p}, items_affected = {_p}, completed_at = {_d.now()} WHERE id = {_p}",
            (status, len(item_ids) + len(chat["item_ids"]), req_id),
        )

    audit_logged = False
    try:
        from audit_trail import write_audit_entry

        audit_meta = {
            "request_id": req_id,
            "items_affected": len(item_ids) + len(chat["item_ids"]),
            "counts": counts,
            "chatlog_counts": chat["counts"],
            "compliance": compliance,
        }
        if not user_id:
            audit_meta["memory_ids"] = item_ids
        audit_meta.update(hooks)
        if failures:
            audit_meta["failures"] = failures
        write_audit_entry(action="gdpr_forget", target_id=subject, metadata=audit_meta)
        audit_logged = True
    except Exception as e:  # noqa: BLE001 -- reported in the result
        logger.warning(f"gdpr_forget request {req_id}: audit trail entry not written ({type(e).__name__}: {e})")
        failures.append(
            {
                "step": "audit_trail",
                "error": f"{type(e).__name__}: {e}",
                "consequence": "the erasure is not in the tamper-evident audit trail (gdpr_requests has it)",
            }
        )

    remaining = _gdpr_copies_elsewhere()
    return {
        "step": "erased",
        "erased": True,
        # Complete means every copy m3 knows of is gone. This erases this
        # machine's stores only; a synced copy is not reached.
        "complete": not failures and not remaining,
        "copies_remaining": remaining,
        "request_id": req_id,
        "items_erased": len(item_ids) + len(chat["item_ids"]),
        "counts": counts,
        "chatlog_store": chat["topology"],
        "chatlog_counts": chat["counts"],
        "derived_ids": plan["derived_ids"],
        "entities_erased": len(plan["entity_ids"]) + len(chat.get("entity_ids", [])),
        **hooks,
        "audit_logged": audit_logged,
        "failures": failures,
    }


def memory_set_retention_impl(agent_id: str, max_memories: int = 1000, ttl_days: int = 0, auto_archive: int = 1) -> str:
    """Set or update agent retention policy."""
    if not agent_id or not agent_id.strip():
        return "Error: agent_id is required"
    try:
        from memory.backends import dialect as _dialect

        _d = _dialect()
        _p = _d.param()
        with _db() as db:
            db.execute(
                f"INSERT INTO agent_retention_policies (agent_id, max_memories, ttl_days, auto_archive, updated_at) "
                f"VALUES ({_p}, {_p}, {_p}, {_p}, {_d.now()}) "
                + _d.on_conflict_update("(agent_id)", ["max_memories", "ttl_days", "auto_archive", "updated_at"]),
                (agent_id, max_memories, ttl_days, auto_archive),
            )
        return f"Retention policy set for agent '{agent_id}': max={max_memories}, ttl={ttl_days}d, auto_archive={bool(auto_archive)}"
    except Exception as e:
        return f"Error setting retention policy: {e}"


def memory_export_impl(
    agent_filter="", type_filter="", since="", output_format="json"
):  # output_format: reserved, JSON only today (bin/check_unreferenced.py RESERVED)
    """Export memories as portable JSON. Filter by agent, type, or date."""
    from memory.backends import dialect as _dialect

    _p = _dialect().param()
    where = ["mi.is_deleted = 0"]
    params = []
    if agent_filter:
        where.append(f"mi.agent_id = {_p}")
        params.append(agent_filter)
    if type_filter:
        where.append(f"mi.type = {_p}")
        params.append(type_filter)
    if since:
        where.append(f"mi.created_at >= {_p}")
        params.append(since)

    where_sql = " AND ".join(where)

    with _db() as db:
        rows = db.execute(f"SELECT * FROM memory_items mi WHERE {where_sql}", params).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            mid = item["id"]
            # Fetch embeddings
            embs = db.execute(
                f"SELECT embedding, embed_model, dim, created_at, content_hash FROM memory_embeddings WHERE memory_id = {_p}",
                (mid,),
            ).fetchall()
            item["embeddings"] = []
            for e in embs:
                edata = dict(e)
                if edata["embedding"]:
                    edata["embedding"] = base64.b64encode(edata["embedding"]).decode("utf-8")
                item["embeddings"].append(edata)

            # Fetch relationships
            rels = db.execute(
                f"SELECT to_id, relationship_type, created_at FROM memory_relationships WHERE from_id = {_p}", (mid,)
            ).fetchall()
            item["relationships"] = [dict(r) for r in rels]
            items.append(item)

    return json.dumps(
        {"exported_at": datetime.now(UTC).isoformat(), "item_count": len(items), "items": items}, indent=2, default=str
    )


def memory_import_impl(data: str):
    """Import memories from a JSON export. UPSERT semantics — safe to re-run."""
    try:
        payload = json.loads(data)
        items = payload.get("items", [])
    except Exception as e:
        return f"Error parsing import data: {e}"

    from memory.backends import active_backend
    from memory.backends import dialect as _dialect
    from memory.backends.base import EmbeddingRow

    _d = _dialect()
    _p = _d.param()
    i_count, e_count, r_count = 0, 0, 0
    with _db() as db:
        for item in items:
            # 1. UPSERT memory_items
            fields = [
                "id",
                "type",
                "title",
                "content",
                "metadata_json",
                "agent_id",
                "model_id",
                "change_agent",
                "importance",
                "source",
                "origin_device",
                "user_id",
                "scope",
                "expires_at",
                "created_at",
                "updated_at",
                "valid_from",
                "valid_to",
                "content_hash",
                "is_deleted",
            ]
            # Filter item to only include known fields
            clean_item = {k: item.get(k) for k in fields if k in item}
            # metadata_json rides back as a dict on a PG export (JSONB whole-column
            # read → dict); binding a bare dict raises "can't adapt type dict". The
            # seam serializes it to a string, which binds on both backends.
            if "metadata_json" in clean_item:
                clean_item["metadata_json"] = _d.json_bind_value(clean_item["metadata_json"])
            placeholders = _d.placeholder(len(clean_item))
            columns = ", ".join(clean_item.keys())
            set_cols = [k for k in clean_item if k != "id"]
            if set_cols:
                sql = (
                    f"INSERT INTO memory_items ({columns}) VALUES ({placeholders}) "
                    f"{_d.on_conflict_update('(id)', set_cols)}"
                )
            else:  # an id-only item: nothing to overwrite, keep the existing row
                sql = (
                    f"{_d.insert_or_ignore()} memory_items ({columns}) VALUES ({placeholders}) "
                    f"{_d.on_conflict_ignore(conflict_target='(id)')}"
                ).rstrip()
            db.execute(sql, list(clean_item.values()))
            i_count += 1

            # 2. Re-insert embeddings. INSERT OR REPLACE is SQLite-only → dialect
            #    upsert on the id PK (overwrite the row's mutable columns).
            mid = clean_item["id"]
            emb_rows = [
                EmbeddingRow(
                    str(uuid.uuid4()),
                    mid,
                    base64.b64decode(edata["embedding"]) if edata.get("embedding") else None,
                    edata.get("embed_model"),
                    edata.get("dim"),
                    edata.get("created_at"),
                    edata.get("content_hash"),
                )
                for edata in item.get("embeddings", [])
            ]
            e_count += active_backend().insert_embeddings(db, emb_rows, on_conflict="update")

            # 3. Re-insert relationships. Idempotent on the (from,to,type) unique
            #    edge (migration 039) — re-import is a no-op, not a dup.
            _rel_suffix = _d.on_conflict_ignore(conflict_target="(from_id, to_id, relationship_type)")
            for rdata in item.get("relationships", []):
                db.execute(
                    f"{_d.insert_or_ignore()} memory_relationships (id, from_id, to_id, relationship_type, created_at) "
                    f"VALUES ({_d.placeholder(5)}) {_rel_suffix}",
                    (
                        str(uuid.uuid4()),
                        mid,
                        rdata.get("to_id"),
                        rdata.get("relationship_type"),
                        rdata.get("created_at"),
                    ),
                )
                r_count += 1

    return f"Imported {i_count} items, {e_count} embeddings, {r_count} relationships"


def _consolidate_fetch_sql(_d, g_type, g_agent, g_user, *, max_importance, stale_cutoff, limit):
    """The ONE predicate for which rows of a group consolidate -- the preview
    counts with it and the write fetches with it, so they cannot disagree.

    A NULL agent_id/user_id group must match its own rows (`col = NULL` is never
    true), so both are compared NULL-safely."""
    p = _d.param()
    sql = (
        f"SELECT id, title, content FROM memory_items "
        f"WHERE type = {p} AND {_d.null_safe_equals('agent_id', p)} "
        f"AND {_d.null_safe_equals('user_id', p)} "
        f"AND is_deleted = 0 AND COALESCE(pinned, 0) = 0 AND COALESCE(importance, 0) <= {p}"
    )
    params = [g_type, g_agent, g_user, max_importance]
    if stale_cutoff:
        sql += f" AND created_at < {p}"
        params.append(stale_cutoff)
    sql += f" ORDER BY created_at ASC LIMIT {p}"
    params.append(limit)
    return sql, params


async def memory_consolidate_tool_impl(
    agent_filter: str = "",
    type_filter: str = "",
    threshold: int = 20,
    stale_days: int = 0,
    dry_run: bool = True,
):
    """The memory_consolidate TOOL: one agent's memories, previewed unless
    dry_run is False. The engine (memory_consolidate_impl) serves the autonomous
    belief job, which scans every agent; a tool call must name one."""
    if not (agent_filter or "").strip():
        return (
            "Error: agent_filter is required: consolidation soft-deletes its sources, so a tool call "
            "names the agent whose memories it rolls up. Nothing was changed."
        )
    return await memory_consolidate_impl(
        type_filter=type_filter,
        agent_filter=agent_filter,
        threshold=threshold,
        stale_days=stale_days,
        dry_run=dry_run,
    )


async def memory_consolidate_impl(
    type_filter="",
    agent_filter="",
    threshold=20,
    stale_days: int = 0,
    max_importance: float = 1.0,
    protected_types=DEFAULT_PROTECTED_TYPES,
    dry_run: bool = False,
    target_type: str = "summary",
):
    """Consolidate the oldest memories of one (type, agent, user) group into a
    summary using the local LLM.

    Safety gates:
      stale_days: only consider items older than N days (0 = no age filter)
      max_importance: skip items with importance above this floor (default 1.0 = no filter)
      protected_types: types never consolidated (defaults to preference/user_fact/task/plan)
      pinned memories are never consolidated
      dry_run: preview what would happen without writes or LLM calls; the preview
        counts with the same predicate the write uses
      target_type: the type of the consolidated output row. Default 'summary'
        (manual/curator rollups). Autonomous belief consolidation (Phase 4)
        passes 'belief' so the two provenance paths stay distinguishable; a
        'belief' row also gets a high first-class confidence.

    A group is skipped -- sources untouched -- when the LLM returns an empty
    summary, the summary cannot be embedded, or a source link fails. Sources are
    closed through the shared supersede write (valid_to, history, edge).
    """
    now_dt = datetime.now(UTC)
    stale_cutoff = (now_dt - timedelta(days=stale_days)).isoformat() if stale_days > 0 else None

    from memory.backends import active_backend
    from memory.backends import dialect as _dialect
    from memory.backends.base import EmbeddingRow

    from memory import write as _write

    _d = _dialect()
    _p = _d.param()
    # 1. Query groups exceeding threshold
    sql = "SELECT type, agent_id, user_id, COUNT(*) as cnt FROM memory_items WHERE is_deleted = 0 AND COALESCE(pinned, 0) = 0"
    params = []
    if type_filter:
        sql += f" AND type = {_p}"
        params.append(type_filter)
    if agent_filter:
        sql += f" AND agent_id = {_p}"
        params.append(agent_filter)
    if protected_types:
        placeholders = _d.placeholder(len(protected_types))
        sql += f" AND type NOT IN ({placeholders})"
        params.extend(protected_types)
    # HAVING can't reference the SELECT alias `cnt` on PG → repeat COUNT(*).
    sql += f" GROUP BY type, agent_id, user_id HAVING COUNT(*) > {_p}"
    params.append(threshold)

    with _db() as db:
        groups = db.execute(sql, params).fetchall()

    if not groups:
        return "No memory groups exceed consolidation threshold."

    def _fetch(g):
        fsql, fparams = _consolidate_fetch_sql(
            _d,
            g["type"],
            g["agent_id"],
            g["user_id"],
            max_importance=max_importance,
            stale_cutoff=stale_cutoff,
            limit=g["cnt"] - threshold,
        )
        with _db() as db:
            return db.execute(fsql, fparams).fetchall()

    if dry_run:
        preview = []
        for g in groups:
            n = len(_fetch(g))
            preview.append(
                f"{g['type']}/{g['agent_id']} (user={g['user_id']}): {n} of {g['cnt']} items would consolidate"
                + ("" if n else " (none pass the age/importance gates)")
            )
        return "DRY RUN — no changes. Candidates:\n" + "\n".join(preview)

    token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
    client = _get_embed_client()
    result = await get_best_llm(client, token)
    if not result:
        return "Error: No local LLM available for consolidation."
    base_url, model = result
    _write._resolve_mc_callbacks()  # _mark_superseded's history write is bound lazily

    results = []
    for g in groups:
        g_type, g_agent, g_user = g["type"], g["agent_id"], g["user_id"]
        label = f"{g_type}/{g_agent} (user={g_user})"
        # 2. Fetch oldest N items, honoring stale_days + importance + pinned gates
        rows = _fetch(g)
        if not rows:
            continue

        # 3. Concatenate content
        items_text = "\n".join(f"- {r['title'] or '(untitled)'}: {r['content']}" for r in rows)

        # 4. Call LLM
        prompt = f"Consolidate these {len(rows)} memory items into a single comprehensive summary. Preserve all facts, decisions, and key details.\n\n{items_text}"
        try:
            chat_url = f"{base_url}/chat/completions"
            resp = await client.post(
                chat_url,
                # Reasoning models leave `content` empty; without this the
                # consolidation silently returns nothing.
                json=apply_thinking_suppression(
                    {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3}, chat_url
                ),
                headers={"Authorization": f"Bearer {token}"},
                timeout=memory_core.LLM_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            if "choices" not in data or not data["choices"]:
                results.append(f"Error consolidating {label}: LLM {model} returned no choices; sources untouched")
                continue
            summary_text = (data["choices"][0]["message"]["content"] or "").strip()
        except Exception as e:
            results.append(f"Error consolidating {label}: {type(e).__name__}: {e}; sources untouched")
            continue
        if not summary_text:
            results.append(
                f"Error consolidating {label}: LLM {model} returned an empty summary for {len(rows)} items; "
                "sources untouched"
            )
            continue

        # 5. Store summary — only once it is embeddable, so it stays searchable.
        summary_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        s_vec, s_model = await _embed(summary_text)
        if not s_vec:
            results.append(
                f"Error consolidating {label}: the summary could not be embedded (the embedder returned no "
                f"vector); {len(rows)} sources untouched. Check: m3 doctor"
            )
            continue

        _label = "belief" if target_type == "belief" else "summary"
        _title = (
            f"Belief from {len(rows)} {g_type} memories ({g_agent})"
            if target_type == "belief"
            else f"Consolidated {g_type} memories for {g_agent}"
        )

        class _LinkFailed(Exception):
            pass

        closed = 0
        try:
            with _db() as db:
                db.execute(
                    f"INSERT INTO memory_items (id, type, title, content, agent_id, user_id, created_at, content_hash) "
                    f"VALUES ({_d.placeholder(8)})",
                    (summary_id, target_type, _title, summary_text, g_agent, g_user, now, _content_hash(summary_text)),
                )
                # A belief is a high-order, multi-source abstraction — give it a high
                # first-class confidence (knowledge-maintenance Phase 4). Guarded so a
                # pre-035 DB (no confidence column) simply skips it; savepoint keeps
                # the PG transaction usable.
                if target_type == "belief":
                    from memory.db import savepoint as _savepoint

                    try:
                        with _savepoint(db):
                            db.execute(f"UPDATE memory_items SET confidence = {_p} WHERE id = {_p}", (0.85, summary_id))
                    except Exception as ce:  # noqa: BLE001 — pre-035 DB lacks confidence
                        if not _is_missing_schema(ce):
                            raise
                active_backend().insert_embeddings(
                    db,
                    [
                        EmbeddingRow(
                            str(uuid.uuid4()),
                            summary_id,
                            _pack(s_vec),
                            s_model,
                            len(s_vec),
                            now,
                            _content_hash(summary_text),
                        )
                    ],
                )
                # 6. Link to sources and 7. close them. A failed link raises, rolling
                # the whole group back: a source closed without its edge is lost.
                for r in rows:
                    link = memory_link_impl(summary_id, r["id"], "consolidates", db=db)
                    if not (link.startswith("Linked:") or link.startswith("Link already exists")):
                        raise _LinkFailed(f"{r['id']}: {link}")
                    if _write._mark_superseded(
                        r["id"], summary_id, close_at=now, db=db, actor_id="memory_consolidate", require_active=True
                    ):
                        closed += 1
        except _LinkFailed as e:
            results.append(f"Error consolidating {label}: link failed ({e}); nothing written, sources untouched")
            continue

        tail = "" if closed == len(rows) else f" ({len(rows) - closed} were closed concurrently and left as they are)"
        results.append(f"Consolidated {closed} of {len(rows)} {g_type} items into {_label} {summary_id}{tail}")

    return "\n".join(results)


# ── Procedural distillation (tasks → reusable `procedure` memories) ──────────
#
# Sibling of memory_consolidate_impl: where consolidation rolls up N episodic
# `observation` memories into a `belief`, distillation rolls up successful task
# runs (a task + its step/result memories) into a reusable `procedure` memory
# via a `distills_from` edge. KEY DIFFERENCE from consolidation: sources are
# PRESERVED (never soft-deleted) — the completed tasks stay queryable; a
# procedure augments history, it doesn't replace it.
#
# BACKEND-AGNOSTIC throughout: all SQL goes through dialect() (param()/
# now_minus_days()), and the write reuses the already-agnostic
# memory_write_impl (which embeds + inserts via the seam) and memory_link_impl.
# No raw SQLite INSERTs, no per-backend branch — runs on SQLite, PostgreSQL, and
# a future MariaDB unchanged.

# Valid procedure sub-kinds. Soft-validated: an unknown kind is allowed (users
# extend), but a model reply outside this set defaults to "skill".
VALID_PROCEDURE_KINDS = frozenset({"skill", "runbook", "how_to", "checklist"})


def undistilled_task_clause(d) -> str:
    """SQL predicate over ``tasks``: no non-deleted procedure was distilled from
    this task yet. The single owner — both the distillation selection and the
    loop's work gate (m3_cognitive_loop.has_distill_work) append it, so the gate
    cannot keep reporting work the pass will not do. A superseded copy still
    counts: it proves the task was distilled.

    Without it every pass re-distilled every completed task and contradiction
    detection superseded the previous copy (~5 procedure writes per minute on
    2026-10-04)."""
    jx = d.json_extract_text("p.metadata_json", "distilled_from_task")
    return (
        f"NOT EXISTS (SELECT 1 FROM memory_items p WHERE p.type = 'procedure' AND p.is_deleted = 0 AND {jx} = tasks.id)"
    )


def _resolve_distill_model() -> str:
    """Return the M3_DISTILL_MODEL selector (local-first default).

    - unset / "slm"  → the local `procedure_local` SLM profile (sovereign default)
    - "llm"          → largest local model via get_best_llm failover
    - any other value → treated as a profile NAME (another local model, or a
                        cloud endpoint via a `backend: anthropic|openai` profile)
    """
    return (_m3cfg.setting("loop.distill_model") or "slm").strip()


async def _distill_call_model(prompt: str) -> "str | None":
    """Run the distillation prompt through the resolved model. Returns the raw
    reply text, or None if this call failed. Local-first, cloud-capable — the
    resolution is config, never a forced cloud dependency.

    Raises llm_failover.LLMUnavailable when no model can answer at all (no
    endpoint lists a model, or the server is unreachable). That is a SYSTEM
    state: the pass stops instead of failing every task the same way."""
    selector = _resolve_distill_model()

    if selector == "llm":
        # Largest local model (the belief-consolidation path).
        token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
        client = _get_embed_client()
        result = await get_best_llm(client, token)
        if not result:
            raise LLMUnavailable(
                "observed: get_best_llm found no endpoint listing a usable model. "
                "inspect: load a model in the local server, or M3_LLM_ENDPOINTS_CSV."
            )
        base_url, model = result
        try:
            chat_url = f"{base_url}/chat/completions"
            resp = await client.post(
                chat_url,
                json=apply_thinking_suppression(
                    {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}, chat_url
                ),
                headers={"Authorization": f"Bearer {token}"},
                timeout=memory_core.LLM_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("choices"):
                return None
            return cast("str | None", data["choices"][0]["message"]["content"])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"distill llm call failed: {type(e).__name__}: {e}")
            return None

    # "slm" (default) or a named profile → the shared Profile loader + _call_model
    # (which already dispatches openai|anthropic, so cloud is config-only).
    import httpx
    from slm_intent import _call_model, load_profile

    prof_name = "procedure_local" if selector in ("", "slm") else selector
    prof = load_profile(prof_name)
    if prof is None:
        logger.warning(f"distill profile {prof_name!r} not found; skipping distillation")
        return None
    try:
        async with httpx.AsyncClient(timeout=prof.timeout_s) as client:
            return await _call_model(prof, prompt, client)
    except LLMUnavailable:
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        # The server itself is unreachable: a system state, not this task's fault.
        raise LLMUnavailable(f"observed: {type(e).__name__} reaching {prof.url}: {e}") from e
    except Exception as e:  # noqa: BLE001
        logger.warning(f"distill slm call failed (profile={prof_name!r}): {type(e).__name__}: {e}")
        return None


def _parse_procedure(text: str) -> "dict | None":
    """Parse the model's JSON procedure. Mirrors run_reflector's parse pattern
    (strip fences + JSON_RE + json.loads). Returns None on malformed / empty
    (no steps) output."""
    from agent_protocol import strip_code_fences
    from run_reflector import JSON_RE

    m = JSON_RE.search(strip_code_fences(text or ""))
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    steps = obj.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    kind = str(obj.get("procedure_kind", "skill")).strip().lower()
    if kind not in VALID_PROCEDURE_KINDS:
        kind = "skill"
    obj["procedure_kind"] = kind
    return obj


def _render_procedure_markdown(proc: dict) -> str:
    """Human-readable markdown body for a distilled procedure (also what gets
    embedded, so it's searchable)."""
    lines = [f"# {proc.get('name') or 'Procedure'}", ""]
    pre = proc.get("preconditions") or []
    if isinstance(pre, list) and pre:
        lines.append("## Preconditions")
        lines.extend(f"- {p}" for p in pre)
        lines.append("")
    lines.append("## Steps")
    for i, step in enumerate(proc.get("steps") or [], 1):
        lines.append(f"{i}. {step}")
    lines.append("")
    got = proc.get("gotchas") or []
    if isinstance(got, list) and got:
        lines.append("## Gotchas")
        lines.extend(f"- {g}" for g in got)
    return "\n".join(lines).strip()


async def memory_distill_procedures_impl(
    stale_days: int = 3,
    threshold: int = 1,
    max_procedures: int = 20,
    dry_run: bool = False,
):
    """Distill successful (completed) task runs into reusable `procedure` memories.

    Backend-agnostic via dialect() + memory_write_impl + memory_link_impl.

    Args:
      stale_days: only consider tasks completed more than N days ago (lets a
        just-finished task settle before it's distilled). 0 = no age filter.
      threshold: minimum number of candidate completed tasks required before any
        distillation runs (anti-noise). Default 1.
      max_procedures: cap procedures written per run (anti-runaway).
      dry_run: preview candidates without any LLM call or write.

    A source task must be state='completed', not deleted, and carry a
    result_memory_id (so there is a distillable result). Sources are PRESERVED
    (linked via 'distills_from', never soft-deleted).
    """
    from memory.backends import dialect
    from memory_core import memory_write_impl

    _d = dialect()
    p = _d.param()

    # 1. Select completed, non-deleted tasks with a result, aged past stale_days.
    #    Built through the dialect so the same SQL runs on SQLite / PG / MariaDB.
    where = ["state = 'completed'", "deleted_at IS NULL", "result_memory_id IS NOT NULL", undistilled_task_clause(_d)]
    params: list = []
    if stale_days > 0:
        where.append(f"completed_at IS NOT NULL AND completed_at < {_d.now_minus_days(p)}")
        params.append(stale_days)
    # NOTE: the tasks table carries no conversation_id/user_id/agent_id — those
    # live on the memories. We derive conversation_id + user_id from the task's
    # RESULT memory below, and use owner_agent/created_by for attribution.
    sql = (
        "SELECT id, title, description, result_memory_id, owner_agent, "
        "created_by FROM tasks WHERE " + " AND ".join(where) + " ORDER BY completed_at ASC"
    )

    with _db() as db:
        tasks = db.execute(sql, params).fetchall()

    if len(tasks) < max(threshold, 1):
        return f"No procedural distillation: {len(tasks)} completed task(s) with results (threshold {threshold})."

    tasks = tasks[:max_procedures]

    if dry_run:
        preview = [f"- task {t['id']}: {t['title'] or '(untitled)'}" for t in tasks]
        return f"DRY RUN — {len(tasks)} task(s) would distill into procedures:\n" + "\n".join(preview)

    results: list[str] = []
    for t in tasks:
        # 2. Gather the task's result + step memories. The result memory is the
        #    anchor; sibling step memories share its conversation_id (if any).
        #    conversation_id + user_id are read from the RESULT memory (the tasks
        #    table carries neither), so the procedure lands under the same tenant.
        src_ids: list[str] = []
        step_texts: list[str] = []
        conv = None
        res_user_id = ""
        with _db() as db:
            res = db.execute(
                f"SELECT id, title, content, conversation_id, user_id "
                f"FROM memory_items WHERE id = {p} AND is_deleted = 0",
                (t["result_memory_id"],),
            ).fetchone()
            if res:
                src_ids.append(res["id"])
                step_texts.append(f"- RESULT: {res['title'] or ''}: {res['content']}")
                conv = res["conversation_id"]
                res_user_id = res["user_id"] or ""
            if conv:
                steps = db.execute(
                    f"SELECT id, title, content FROM memory_items "
                    f"WHERE conversation_id = {p} AND is_deleted = 0 "
                    f"AND id <> {p} ORDER BY created_at ASC LIMIT 50",
                    (conv, t["result_memory_id"]),
                ).fetchall()
                for s in steps:
                    src_ids.append(s["id"])
                    step_texts.append(f"- STEP: {s['title'] or ''}: {s['content']}")

        if not src_ids:
            continue

        # 3. Distill via the pluggable local-first/cloud model.
        prompt = (
            "Distill this successful task run into a reusable procedure.\n\n"
            f"TASK: {t['title'] or '(untitled)'}\n"
            f"DESCRIPTION: {t['description'] or ''}\n\n"
            "STEPS AND RESULT:\n" + "\n".join(step_texts)
        )
        try:
            reply = await _distill_call_model(prompt)
        except LLMUnavailable as e:
            report_unavailable(logger, "distillation", e)
            remaining = len(tasks) - tasks.index(t)
            results.append(f"Stopped: local LLM unavailable; {remaining} task(s) left for the next pass. {e}")
            break
        report_available(logger, "distillation")
        if not reply:
            results.append(f"Skipped task {t['id']}: no distillation model output.")
            continue
        proc = _parse_procedure(reply)
        if proc is None:
            results.append(f"Skipped task {t['id']}: model returned no coherent procedure.")
            continue

        # 4. Write the procedure via the backend-agnostic impl (embeds + inserts
        #    through the seam). procedure_kind + steps ride metadata_json.
        body = _render_procedure_markdown(proc)
        meta = json.dumps(
            {
                "procedure_kind": proc["procedure_kind"],
                "steps": proc.get("steps") or [],
                "preconditions": proc.get("preconditions") or [],
                "gotchas": proc.get("gotchas") or [],
                "distilled_from_task": t["id"],
            }
        )
        proc_id = await memory_write_impl(
            type="procedure",
            content=body,
            title=(proc.get("name") or (t["title"] or "Procedure"))[:200],
            metadata=meta,
            agent_id=t["owner_agent"] or t["created_by"] or "",
            importance=0.8,
            source="distillation",
            user_id=res_user_id,
            confidence=0.85,
        )
        # memory_write_impl's success string is "Created: <uuid>[ (…)]". Parse
        # the uuid defensively (same contract supersede relies on); skip the
        # provenance link if the write was rejected / the format changed.
        if not (isinstance(proc_id, str) and proc_id.startswith("Created:")):
            results.append(f"Skipped task {t['id']}: procedure write failed: {proc_id}")
            continue
        new_id = proc_id.split("Created:", 1)[1].strip().split()[0]

        # 5. Provenance: link the procedure to each source (sources PRESERVED).
        with _db() as db:
            for sid in src_ids:
                memory_link_impl(new_id, sid, "distills_from", db=db)

        results.append(
            f"Distilled task {t['id']} → procedure {new_id} (kind={proc['procedure_kind']}, {len(src_ids)} source(s))"
        )

    return "\n".join(results) if results else "No procedures distilled."


if __name__ == "__main__":
    # Scheduled-task entrypoint. Previously invoked via
    #   python -c "import memory_maintenance; memory_maintenance.memory_maintenance_impl()"
    # which never reached this block. install_schedules.py
    # now invokes this file as a script so logging + single-instance locking
    # apply. The helper call lives here (not in memory_maintenance_impl) so
    # MCP-server imports of this module are unaffected.
    import argparse
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _task_runtime import add_log_file_arg, setup_task_runtime

    parser = argparse.ArgumentParser(description="Daily memory maintenance (decay, prune orphans, retention).")
    add_log_file_arg(parser)
    args = parser.parse_args()

    setup_task_runtime(
        args.log_file,
        lock_name="memory_maintenance",
        logger_name="memory_maintenance",
    )

    import time as _time

    _MAX_RETRIES = 3
    _RETRY_DELAY = 30  # seconds — wait for any concurrent MCP write transaction to finish
    for _attempt in range(1, _MAX_RETRIES + 1):
        try:
            print(memory_maintenance_impl())
            break
        except sqlite3.OperationalError as _e:
            if "database is locked" not in str(_e) or _attempt == _MAX_RETRIES:
                raise
            logger.warning(
                f"database is locked (attempt {_attempt}/{_MAX_RETRIES}), "
                f"retrying in {_RETRY_DELAY}s — MCP server may be mid-write"
            )
            _time.sleep(_RETRY_DELAY)
