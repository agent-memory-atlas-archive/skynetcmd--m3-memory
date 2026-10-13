"""``M3Saver(BaseCheckpointSaver)`` — LangGraph checkpoint persistence on m3.

PR-4. A checkpointer is a DIFFERENT surface from ``M3Store`` (long-term memory)
and ``M3ChatMessageHistory`` (chat turns): it persists the **opaque serialized
graph state** LangGraph needs to pause/resume/time-travel a run — keyed by
``(thread_id, checkpoint_ns, checkpoint_id)`` — plus the per-task *pending writes*
that accumulate between super-steps.

Why NOT reuse ``memory_items`` / the chatlog: a checkpoint is machine state, not
knowledge. Running it through m3's embedder + contradiction/supersession pipeline
would be wrong on every axis — it isn't semantically searchable, two checkpoints
of the same thread are NOT contradictions to reconcile, and the blob is opaque
bytes, not text. So this store owns two **dedicated tables** in the m3 engine DB
(``m3_lg_checkpoints`` + ``m3_lg_writes``), self-healed on first use exactly like
the chatlog schema (§4 "just works") — no m3 migration coupling, no ToolSpec.

Faithful to the tenets:
  * §8 Affinity — ALL store work rides the ONE shared m3client loop-thread
    (``_run``/``_run_async``), so the connection pool stays thread-consistent;
    the async ``aput``/``aget_tuple``/… never block LangGraph's own loop.
  * §2.5 Topology — checkpoints live in the ENGINE (main) DB. We pin
    ``active_database`` for our own connections so a caller who set an
    ``active_database`` for chatlog work doesn't misroute checkpoint writes.
  * §3 Robustness — serialization goes through LangGraph's own ``serde`` (the
    contract), blobs are parameterized (§6), and a missing langgraph fails loud
    at import (this module is imported lazily by the package ``__init__``).
  * §7 Privacy — ``thread_id`` is the isolation key; a ``user_id`` from the
    config's ``configurable`` rides ``m3_lg_checkpoints.user_id`` for provenance
    and is enforced on read/list/delete when supplied (a run can't read another
    user's threads if it passes ``user_id``).
"""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator, Iterator, Sequence

# `List` (capitalized) is used for return annotations INSIDE this class because
# the class defines a method named ``list`` that shadows the builtin ``list`` in
# class scope — ``list[CheckpointTuple]`` would resolve to ``M3Saver.list[...]``.
from typing import Any, cast, override

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
)

from .m3client import M3Client

# §4/§7: default cap for an UNSCOPED list() (no thread_id, no explicit limit) so
# an accidental cross-thread enumeration can't pull the whole table. A
# thread-scoped list is bounded by that thread's own history and is not capped.
_UNSCOPED_LIST_CAP = 1000


# ── schema ────────────────────────────────────────────────────────────────────
# Owned by migrations 054 (SQLite) / pg_063 (PostgreSQL): m3 schemas change only
# through migrations. The integration issues no DDL; opening the store applies any
# pending migration. Checkpoints are keyed by the LangGraph triple; writes hang off
# a checkpoint by (task_id, idx); deleted_at marks a soft-deleted thread.


def _dialect():
    from memory.backends import dialect

    return dialect()


def _cfg(config: RunnableConfig | None) -> dict:
    return (config or {}).get("configurable", {}) or {}


class M3Saver(BaseCheckpointSaver):
    """A LangGraph ``BaseCheckpointSaver`` backed by m3's active main store.

    Wire it into a graph exactly like any checkpointer::

        from m3_memory.langchain import M3Saver
        graph = builder.compile(checkpointer=M3Saver())
        graph.invoke({...}, config={"configurable": {"thread_id": "t1"}})

    ``thread_id`` (required by LangGraph) is the resume/isolation key. An optional
    ``user_id`` in ``configurable`` is stored for provenance and, when supplied on
    a read/list/delete, scopes the query to that user's threads (§7).
    """

    def __init__(self, *, agent_id: str = "langchain", call_timeout: float = 30.0):
        super().__init__()  # sets up self.serde (JsonPlusSerializer)
        self._client = M3Client(agent_id=agent_id, call_timeout=call_timeout)
        self._healed = False

    # ── schema self-heal (one-time, on the loop-thread) ───────────────────────
    def _ensure_schema(self) -> None:
        if self._healed:
            return
        self._client._run(self._ensure_schema_async())
        self._healed = True

    async def _ensure_schema_async(self) -> None:
        from memory.db import _db

        with _db():  # the store's first open applies pending migrations (054 / pg_063)
            pass

    # ── version scheme (opaque monotonic string, like the sqlite saver) ───────
    @override
    def get_next_version(self, current: Any | None, channel: Any = None) -> str:
        """Monotonic version: ``NNNNNNNN.<hash>``. LangGraph only needs the
        ordering to be total and increasing; we mirror the reference sqlite/
        memory savers' integer-prefixed scheme."""
        if current is None:
            cur = 0
        elif isinstance(current, int):
            cur = current
        else:
            cur = int(str(current).split(".")[0])
        return f"{cur + 1:032d}"

    # ── serialization helpers ─────────────────────────────────────────────────
    def _dump(self, obj: Any) -> tuple[str, bytes]:
        """serde → (type, bytes). LangGraph's serde returns (type, bytes)."""
        return cast("tuple[str, bytes]", self.serde.dumps_typed(obj))

    def _load(self, type_: str | None, blob: Any) -> Any:
        return self.serde.loads_typed((type_ or "", bytes(blob)))

    # ── PUT: persist one checkpoint ───────────────────────────────────────────
    @override
    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        conf = _cfg(config)
        thread_id = conf["thread_id"]
        ns = conf.get("checkpoint_ns", "")
        ckpt_id = checkpoint["id"]
        parent_id = conf.get("checkpoint_id")  # the config we were resumed from
        user_id = conf.get("user_id", "") or ""

        c_type, c_blob = self._dump(checkpoint)
        # metadata rides the same serde so custom values round-trip (§2.4-style);
        # its type is stored alongside so loads_typed can decode it (the two blobs
        # can serialize to different types — json vs msgpack).
        m_type, m_blob = self._dump(dict(metadata))

        self._ensure_schema()
        self._client._run(
            self._put_async(
                thread_id,
                ns,
                ckpt_id,
                parent_id,
                user_id,
                c_type,
                m_type,
                c_blob,
                m_blob,
                checkpoint["ts"],
            )
        )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": ns,
                "checkpoint_id": ckpt_id,
            }
        }

    async def _put_async(
        self, thread_id, ns, ckpt_id, parent_id, user_id, c_type, m_type, c_blob, m_blob, created_at
    ) -> None:
        from memory.db import _db

        d = _dialect()
        upsert = d.on_conflict_update(
            "(thread_id, checkpoint_ns, checkpoint_id)",
            ["parent_id", "user_id", "type", "metadata_type", "checkpoint", "metadata", "created_at", "deleted_at"],
        )
        with _db() as db:
            db.execute(
                "INSERT INTO m3_lg_checkpoints "
                "(thread_id, checkpoint_ns, checkpoint_id, parent_id, user_id, "
                " type, metadata_type, checkpoint, metadata, created_at, deleted_at) "
                f"VALUES ({d.placeholder(11)}) {upsert}",
                (thread_id, ns, ckpt_id, parent_id, user_id, c_type, m_type, c_blob, m_blob, created_at, None),
            )

    @override
    async def aput(self, config, checkpoint, metadata, new_versions) -> RunnableConfig:
        return cast(
            "RunnableConfig", await self._client._run_async(lambda: self._aput_on_loop(config, checkpoint, metadata))
        )

    async def _aput_on_loop(self, config, checkpoint, metadata) -> RunnableConfig:
        conf = _cfg(config)
        thread_id = conf["thread_id"]
        ns = conf.get("checkpoint_ns", "")
        ckpt_id = checkpoint["id"]
        c_type, c_blob = self._dump(checkpoint)
        m_type, m_blob = self._dump(dict(metadata))
        await self._ensure_schema_async()
        await self._put_async(
            thread_id,
            ns,
            ckpt_id,
            conf.get("checkpoint_id"),
            conf.get("user_id", "") or "",
            c_type,
            m_type,
            c_blob,
            m_blob,
            checkpoint["ts"],
        )
        return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": ckpt_id}}

    # ── PUT_WRITES: pending writes for a task, idempotent by (task_id, idx) ────
    @override
    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        conf = _cfg(config)
        thread_id = conf["thread_id"]
        ns = conf.get("checkpoint_ns", "")
        ckpt_id = conf["checkpoint_id"]
        rows = self._writes_rows(thread_id, ns, ckpt_id, task_id, writes)
        self._ensure_schema()
        self._client._run(self._put_writes_async(rows))

    def _writes_rows(self, thread_id, ns, ckpt_id, task_id, writes) -> list[tuple]:
        rows = []
        for i, (channel, value) in enumerate(writes):
            # A special channel (e.g. a task's error/interrupt) has a stable slot
            # in WRITES_IDX_MAP so a retry overwrites rather than appends; normal
            # writes append at their sequence index.
            idx = WRITES_IDX_MAP.get(channel, i)
            t, blob = self._dump(value)
            rows.append((thread_id, ns, ckpt_id, task_id, idx, channel, t, blob))
        return rows

    async def _put_writes_async(self, rows: list[tuple]) -> None:
        from memory.db import _db

        d = _dialect()
        upsert = d.on_conflict_update(
            "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx)", ["channel", "type", "blob"]
        )
        with _db() as db:
            db.executemany(
                "INSERT INTO m3_lg_writes "
                "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, "
                f" channel, type, blob) VALUES ({d.placeholder(8)}) {upsert}",
                rows,
            )

    @override
    async def aput_writes(self, config, writes, task_id, task_path: str = "") -> None:
        conf = _cfg(config)
        rows = self._writes_rows(
            conf["thread_id"],
            conf.get("checkpoint_ns", ""),
            conf["checkpoint_id"],
            task_id,
            writes,
        )
        await self._client._run_async(lambda: self._aput_writes_on_loop(rows))

    async def _aput_writes_on_loop(self, rows) -> None:
        await self._ensure_schema_async()
        await self._put_writes_async(rows)

    # ── GET: one checkpoint tuple (latest for a thread, or a named id) ────────
    @override
    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        self._ensure_schema()
        return cast("CheckpointTuple | None", self._client._run(self._get_tuple_async(config)))

    @override
    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return cast("CheckpointTuple | None", await self._client._run_async(lambda: self._get_tuple_on_loop(config)))

    async def _get_tuple_on_loop(self, config: RunnableConfig) -> CheckpointTuple | None:
        await self._ensure_schema_async()
        return await self._get_tuple_async(config)

    async def _get_tuple_async(self, config: RunnableConfig) -> CheckpointTuple | None:
        from memory.db import _db

        conf = _cfg(config)
        thread_id = conf.get("thread_id")
        if not thread_id:
            return None
        ns = conf.get("checkpoint_ns", "")
        ckpt_id = get_checkpoint_id(config)
        user_id = conf.get("user_id")

        _p = _dialect().param()
        where = [f"thread_id = {_p}", f"checkpoint_ns = {_p}", "deleted_at IS NULL"]
        params: list[Any] = [thread_id, ns]
        if ckpt_id:
            where.append(f"checkpoint_id = {_p}")
            params.append(ckpt_id)
        if user_id:  # §7: scope to the user's threads when supplied
            where.append(f"user_id = {_p}")
            params.append(user_id)
        sql = (
            "SELECT thread_id, checkpoint_ns, checkpoint_id, parent_id, type, "
            "metadata_type, checkpoint, metadata FROM m3_lg_checkpoints "
            f"WHERE {' AND '.join(where)} "
            # named id → that row; otherwise the newest checkpoint for the thread.
            "ORDER BY created_at DESC, checkpoint_id DESC LIMIT 1"
        )
        with _db() as db:
            row = db.execute(sql, params).fetchone()
            if row is None:
                return None
            writes = self._load_writes(db, row["thread_id"], row["checkpoint_ns"], row["checkpoint_id"])
        return self._row_to_tuple(row, writes)

    def _load_writes(self, db, thread_id, ns, ckpt_id) -> list[tuple]:
        _p = _dialect().param()
        cur = db.execute(
            "SELECT task_id, channel, type, blob FROM m3_lg_writes "
            f"WHERE thread_id = {_p} AND checkpoint_ns = {_p} AND checkpoint_id = {_p} "
            "ORDER BY task_id, idx",
            (thread_id, ns, ckpt_id),
        )
        return [(w["task_id"], w["channel"], self._load(w["type"], w["blob"])) for w in cur.fetchall()]

    def _row_to_tuple(self, row, pending_writes) -> CheckpointTuple:
        checkpoint = self._load(row["type"], row["checkpoint"])
        metadata = self._load(row["metadata_type"], row["metadata"])
        base_conf: RunnableConfig = {
            "configurable": {
                "thread_id": row["thread_id"],
                "checkpoint_ns": row["checkpoint_ns"],
                "checkpoint_id": row["checkpoint_id"],
            }
        }
        parent_conf: RunnableConfig | None = None
        if row["parent_id"]:
            parent_conf = {
                "configurable": {
                    "thread_id": row["thread_id"],
                    "checkpoint_ns": row["checkpoint_ns"],
                    "checkpoint_id": row["parent_id"],
                }
            }
        return CheckpointTuple(
            config=base_conf,
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_conf,
            pending_writes=pending_writes,
        )

    # ── LIST: checkpoints for a thread, newest first (history / time-travel) ──
    @override
    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        self._ensure_schema()
        rows = self._client._run(self._list_async(config, filter, before, limit))
        yield from rows

    @override
    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        rows = await self._client._run_async(lambda: self._alist_on_loop(config, filter, before, limit))
        for r in rows:
            yield r

    async def _alist_on_loop(self, config, filter, before, limit):
        await self._ensure_schema_async()
        return await self._list_async(config, filter, before, limit)

    async def _list_async(
        self,
        config,
        filter,
        before,
        limit,
    ) -> builtins.list[CheckpointTuple]:
        from memory.db import _db

        conf = _cfg(config)  # _cfg tolerates None
        _p = _dialect().param()
        where: list[str] = ["deleted_at IS NULL"]
        params: list[Any] = []
        if conf.get("thread_id"):
            where.append(f"thread_id = {_p}")
            params.append(conf["thread_id"])
        if conf.get("checkpoint_ns") is not None and "checkpoint_ns" in conf:
            where.append(f"checkpoint_ns = {_p}")
            params.append(conf["checkpoint_ns"])
        if conf.get("user_id"):
            where.append(f"user_id = {_p}")
            params.append(conf["user_id"])
        if before is not None:
            b_id = get_checkpoint_id(before)
            if b_id:
                # newest-first, so "before X" = created strictly earlier than X.
                where.append(
                    f"created_at < (SELECT created_at FROM m3_lg_checkpoints WHERE checkpoint_id = {_p} LIMIT 1)"
                )
                params.append(b_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = (
            "SELECT thread_id, checkpoint_ns, checkpoint_id, parent_id, type, "
            "metadata_type, checkpoint, metadata FROM m3_lg_checkpoints"
            + clause
            + " ORDER BY created_at DESC, checkpoint_id DESC"
        )
        # §4/§7: never issue an unbounded, unscoped scan. LangGraph permits
        # list(None) to enumerate across threads (a legit admin/debug op, so we
        # don't raise as the memory tools do), but an UNSCOPED call with no limit
        # would pull the whole table. Cap it at the API boundary so an accidental
        # list() can't fetch every checkpoint in the DB. A thread-scoped call is
        # already bounded by that thread's history and keeps the caller's limit
        # verbatim (None = all of one thread, which is small).
        eff_limit = limit
        if eff_limit is None and not conf.get("thread_id"):
            eff_limit = _UNSCOPED_LIST_CAP
        if eff_limit is not None:
            sql += f" LIMIT {_p}"
            params.append(int(eff_limit))

        out: list[CheckpointTuple] = []
        with _db() as db:
            rows = db.execute(sql, params).fetchall()
            for row in rows:
                # metadata filter is applied post-load (values are serde blobs).
                md = self._load(row["metadata_type"], row["metadata"])
                if filter and not all(md.get(k) == v for k, v in filter.items()):
                    continue
                writes = self._load_writes(db, row["thread_id"], row["checkpoint_ns"], row["checkpoint_id"])
                out.append(self._row_to_tuple(row, writes))
        return out

    # ── DELETE: soft-delete a whole thread ────────────────────────────────────
    # Hard delete is GDPR-only (memory.deletion): the thread's checkpoints get
    # deleted_at and drop out of every read; their writes hang off them and go
    # with them. gdpr_forget erases a user's checkpoints for real.
    @override
    def delete_thread(self, thread_id: str) -> None:
        self._ensure_schema()
        self._client._run(self._delete_thread_async(thread_id))

    @override
    async def adelete_thread(self, thread_id: str) -> None:
        await self._client._run_async(lambda: self._adelete_thread_on_loop(thread_id))

    async def _adelete_thread_on_loop(self, thread_id: str) -> None:
        await self._ensure_schema_async()
        await self._delete_thread_async(thread_id)

    async def _delete_thread_async(self, thread_id: str) -> None:
        from datetime import UTC, datetime

        from memory.db import _db

        _p = _dialect().param()
        with _db() as db:
            db.execute(
                f"UPDATE m3_lg_checkpoints SET deleted_at = {_p} WHERE thread_id = {_p} AND deleted_at IS NULL",
                (datetime.now(UTC).isoformat(), thread_id),
            )
