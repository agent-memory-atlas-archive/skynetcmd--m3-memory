"""Rows a split install holds in the wrong store.

Before 2026.10.15, sync pulled every warehouse row into both stores of a split
install: chat turns into the memory store, memories into the chat store. Sync now
takes only each store's own kind, but the copies it already made stay. This lists
them and, on repair, removes a copy only when its home store holds the same row
(same id, same content, same deleted state, at least as new), every link on it,
and an embedding wherever the copy has one. Anything else is kept and listed.

A copy is removed by DELETE, not marked deleted: a deleted mark would sync to
the warehouse and from there delete the home copy on every machine.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Tables in a store of its own that point at one of its memories by id, removed
# with the copy. Where both kinds share one database, these names hold the
# memory family's rows and each family's dependents follow its items by
# ON DELETE CASCADE; deleting by these names there would hit the wrong family.
_STORE_LOCAL_DEPENDENTS = (
    ("memory_item_entities", "memory_id"),
    ("entity_extraction_queue", "memory_id"),
    ("entity_relationships", "source_memory_id"),
    ("memory_history", "memory_id"),
    ("memory_access_log", "memory_id"),
    ("bypass_surface", "memory_id"),
    ("fact_enrichment_queue", "memory_id"),
    ("memory_corroborations", "memory_id"),
)
_CHUNK = 500  # ids per statement, under every backend's bind limit


@dataclass(slots=True)
class StoreReport:
    store: str
    holds: str  # the kind it should hold: "memory" or "chat"
    stray: int = 0  # rows of the other kind
    removable: int = 0  # strays whose home store holds the same row
    kept: dict[str, int] = field(default_factory=dict)  # reason -> count
    removed: int = 0


def _other(kind: str) -> str:
    return "chat" if kind == "memory" else "memory"


def _split_targets():
    """The two local stores of a split install, as sync resolves them; [] otherwise."""
    from pg_sync import resolve_sync_targets

    targets = [t for t in resolve_sync_targets() if t.kind in ("memory", "chat")]
    return targets if {t.kind for t in targets} == {"memory", "chat"} else []


def _chunks(ids):
    ids = list(ids)
    for i in range(0, len(ids), _CHUNK):
        yield ids[i : i + _CHUNK]


def _rows_by_id(cur, d, table: str, cols: str, ids) -> dict:
    out = {}
    for c in _chunks(ids):
        cur.execute(f"SELECT {cols} FROM {table} WHERE id IN ({d.placeholder(len(c))})", tuple(c))
        out.update({r[0]: tuple(r) for r in cur.fetchall()})
    return out


def _values_in(cur, d, table: str, col: str, ids) -> set:
    found: set = set()
    for c in _chunks(ids):
        cur.execute(f"SELECT {col} FROM {table} WHERE {col} IN ({d.placeholder(len(c))})", tuple(c))
        found.update(r[0] for r in cur.fetchall())
    return found


def _edges_on(cur, d, table: str, ids) -> dict:
    """memory id -> ids of the links in ``table`` that touch it."""
    out: dict = {}
    for c in _chunks(ids):
        ph = d.placeholder(len(c))
        cur.execute(f"SELECT id, from_id, to_id FROM {table} WHERE from_id IN ({ph}) OR to_id IN ({ph})", (*c, *c))
        for eid, a, b in (tuple(r) for r in cur.fetchall()):
            for mid in (a, b):
                out.setdefault(mid, set()).add(eid)
    return out


def _differs(copy, home) -> str | None:
    """Why a copy must stay although its home store holds the row, or None.
    Rows are (id, content_hash, content, is_deleted, updated_at). The home row
    is the record of its kind: a deleted mark the copy alone carries, under the
    same timestamp, never synced (its home copy stayed live everywhere)."""
    _, ch, cc, _cdel, cup = copy
    _, hh, hc, _hdel, hup = home
    if not ((ch == hh) if (ch and hh) else (cc == hc)):
        return "content differs from its home copy"
    if str(cup or "") > str(hup or ""):
        return "newer than its home copy"
    return None


_ITEM_COLS = "id, content_hash, content, is_deleted, updated_at"


def _classify(target, cur, home, hcur) -> tuple[StoreReport, list]:
    """The report for one store and the ids of its removable copies."""
    from memory.backends.dialect import dialect_for_connection
    from pg_sync import _kind_filter

    d, hd = dialect_for_connection(cur.connection), dialect_for_connection(hcur.connection)
    rep = StoreReport(target.name, target.kind)
    clause, params = _kind_filter(_other(target.kind), d.param())
    cur.execute(f"SELECT {_ITEM_COLS} FROM {target.table('items')} WHERE 1=1{clause}", params)
    strays = {r[0]: tuple(r) for r in cur.fetchall()}
    rep.stray = len(strays)
    if not strays:
        return rep, []

    homes = _rows_by_id(hcur, hd, home.table("items"), _ITEM_COLS, strays)
    copy_edges = _edges_on(cur, d, target.table("relationships"), strays)
    home_edge_ids = _values_in(hcur, hd, home.table("relationships"), "id", set().union(*copy_edges.values()))
    copy_emb = _values_in(cur, d, target.table("embeddings"), "memory_id", strays)
    home_emb = _values_in(hcur, hd, home.table("embeddings"), "memory_id", copy_emb)

    removable = []
    for mid, row in strays.items():
        if mid not in homes:
            why = "its only copy"
        elif differs := _differs(row, homes[mid]):
            why = differs
        elif not copy_edges.get(mid, set()) <= home_edge_ids:
            why = "has links its home store lacks"
        elif mid in copy_emb and mid not in home_emb:
            why = "has an embedding its home copy lacks"
        else:
            removable.append(mid)
            continue
        rep.kept[why] = rep.kept.get(why, 0) + 1
    rep.removable = len(removable)
    return rep, removable


def _remove(target, conn, cur, ids, own_store: bool) -> int:
    from memory.backends.dialect import dialect_for_connection

    d = dialect_for_connection(conn)
    tables = [(target.table("embeddings"), "memory_id")]
    if own_store:
        for table, col in _STORE_LOCAL_DEPENDENTS:
            sql, params = d.table_exists(table)
            cur.execute(sql, params)
            if cur.fetchone() is not None:
                tables.append((table, col))
    removed = 0
    for c in _chunks(ids):
        ph = d.placeholder(len(c))
        for table, col in tables:
            cur.execute(f"DELETE FROM {table} WHERE {col} IN ({ph})", tuple(c))
        cur.execute(f"DELETE FROM {target.table('relationships')} WHERE from_id IN ({ph}) OR to_id IN ({ph})", (*c, *c))
        cur.execute(f"DELETE FROM {target.table('items')} WHERE id IN ({ph})", tuple(c))
        removed += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
    return removed


def check(repair: bool = False) -> list[StoreReport]:
    """One report per store of a split install ([] when the install is not split).
    With ``repair``, removes each store's removable copies."""
    from pg_sync import _open_local_store

    targets = _split_targets()
    if not targets:
        return []
    reports = []
    for target in targets:
        home = next(t for t in targets if t.kind == _other(target.kind))
        with _open_local_store(target) as (conn, cur), _open_local_store(home) as (_hconn, hcur):
            if conn is None or _hconn is None:
                continue
            rep, ids = _classify(target, cur, home, hcur)
            if repair and ids:
                rep.removed = _remove(target, conn, cur, ids, own_store=target.uri != home.uri)
        reports.append(rep)
    return reports


def _line(rep: StoreReport) -> str:
    want = "memories" if rep.holds == "memory" else "chat turns"
    other = "chat turns" if rep.holds == "memory" else "memories"
    s = f"{rep.store} store (holds {want}): {rep.stray} {other} copied in by an older sync"
    if rep.removed:
        s += f", {rep.removed} removed (each kept in its home store)"
    if rep.kept:
        s += "; kept: " + ", ".join(f"{n} {why}" for why, n in sorted(rep.kept.items()))
    return s


def run(brief: bool = False) -> int:
    """Report-only. Always returns 0: the copies cost space and duplicate search
    results, they lose nothing."""
    try:
        reports = [r for r in check() if r.stray]
    except Exception as e:  # noqa: BLE001 -- the probe must never crash the doctor
        print(f"misplaced rows: could not check ({type(e).__name__}: {e})")
        return 0
    if brief:
        if reports:
            n = sum(r.removable for r in reports)
            print(f"misplaced rows: {sum(r.stray for r in reports)} in the wrong store, {n} removable: m3 doctor --fix")
        return 0
    if reports:
        print("\n=== rows in the wrong store ===")
        for r in reports:
            print(f"  {_line(r)}; {r.removable} removable by m3 doctor --fix")
    return 0


def repair_and_report(dry_run: bool = False) -> None:
    """check(repair=True), one line per store that held copies (silent otherwise)."""
    try:
        reports = check(repair=not dry_run)
    except Exception as e:  # noqa: BLE001 -- never break the doctor
        print(f"  [skipped] rows in the wrong store: {type(e).__name__}: {e}")
        return
    for r in reports:
        if r.stray:
            tag = "dry-run" if dry_run else ("fixed" if r.removed == r.removable else "not fixed")
            print(f"  [{tag}] {_line(r)}")
