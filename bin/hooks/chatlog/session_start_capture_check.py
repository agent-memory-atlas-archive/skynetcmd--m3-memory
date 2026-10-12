#!/usr/bin/env python
"""SessionStart hook: verify m3 chatlog capture is actually LANDING writes.

Prints a plain one-line GREEN/RED status as a hook systemMessage. Deliberately
checks whether rows are being WRITTEN to the DB in the recent past, NOT whether
config.host_agents[*].enabled is set — that flag reflects only whether a per-turn
shell hook was wired into settings.json at init time, and reads `false` even when
the Stop-hook / MCP write path is capturing fine (confirmed 2026-06-13). A future
session running the CLAUDE.md mandated session-start check must trust DATA, not the
flag, or it gets a permanent false alarm (or false comfort).

Outputs hook JSON on stdout: {"systemMessage": "...", "suppressOutput": true}.
Never throws — a monitoring check must not break session start.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

WINDOW_MIN = 15


def _bin_on_path() -> Path:
    """Put the payload's bin/ first on sys.path (once); return the repo root.
    M3_HOME, if set, only LOCATES bin/; it never decides the DB."""
    repo = Path(os.environ.get("M3_HOME") or Path(__file__).resolve().parents[3])
    b = str(repo / "bin")
    if b not in sys.path:
        sys.path.insert(0, b)
    return repo


def _resolve_db() -> str:
    """Resolve the m3 agent-memory DB path portably (no hardcoded user path).

    Order: m3_sdk.resolve_db_path (canonical, honors M3_DATABASE / M3_ENGINE_ROOT
    / M3_MEMORY_ROOT exactly as the server does) -> M3_DB_PATH env (legacy) ->
    canonical engine root (<M3_ENGINE_ROOT|~/.m3/engine>/agent_memory.db) ->
    repo-root/engine/agent_memory.db (last-resort dev-clone guess).

    The canonical resolver is tried first AND its engine-root default is used
    for the fallback, so this hook checks the SAME DB the running MCP server
    writes to. The old fallback resolved the DB from M3_HOME/engine, which
    diverges from the server's M3_ENGINE_ROOT — the split-brain that produced a
    false "chatlog NOT writing" alarm against a stale pre-Homecoming copy.
    """
    # bin/hooks/chatlog/this_file.py -> repo root is parents[3]. M3_HOME (if set)
    # only helps LOCATE the bin/ dir to import m3_sdk; it does NOT decide the DB.
    repo = _bin_on_path()
    engine_root_fn: Callable[[], str] | None = None
    try:
        from m3_sdk import get_m3_engine_root, resolve_db_path

        engine_root_fn = get_m3_engine_root
        p = resolve_db_path(None)
        if p:
            return os.path.abspath(p)
    except Exception:  # noqa: BLE001 — fall back to path heuristics
        engine_root_fn = None
    env = os.environ.get("M3_DB_PATH")
    if env:
        return os.path.abspath(env)
    # Prefer the canonical engine root (matches the server) over the repo-relative
    # guess, which is a frozen pre-Homecoming copy on migrated installs.
    if engine_root_fn is not None:
        try:
            return os.path.abspath(os.path.join(engine_root_fn(), "agent_memory.db"))
        except Exception:  # noqa: BLE001
            pass
    # m3_sdk did not import: the stdlib-only roots owner still does (it applies
    # M3_ENGINE_ROOT > M3_MEMORY_ROOT/engine > ~/.m3/engine). The guess this used
    # to end on, <repo>/engine, is a pre-Homecoming location: on a migrated
    # install it named the wrong DB and raised a false "NOT writing" alarm.
    try:
        from m3_core.seams import roots

        return os.path.abspath(os.path.join(roots.engine_root(), "agent_memory.db"))
    except Exception:  # noqa: BLE001 — the code itself is unreachable
        return str(repo / "engine" / "agent_memory.db")


def _recent_window_sql() -> tuple[str, tuple]:
    """(sql, params) counting chat_log rows written in the last WINDOW_MIN.

    The "now minus N minutes" expression comes from the backend seam
    (DESIGN_PHILOSOPHIES §10a) rather than the SQLite-only
    ``datetime('now', ?)`` idiom, which raises on PostgreSQL. The minutes value
    is BOUND, not interpolated into the SQL text.

    This hook must never break session start, so a seam import failure falls
    back to the SQLite form -- the hook only ever opens a SQLite file by path.
    """
    base = "SELECT COUNT(*) FROM memory_items WHERE type = 'chat_log' AND "
    try:  # pragma: no cover - import shim for standalone hook execution
        from memory.backends.sqlite_backend import SqliteDialect

        d = SqliteDialect(backend="sqlite", param_style="qmark")
        return base + f"created_at > {d.now_minus_minutes(d.param())}", (int(WINDOW_MIN),)
    except Exception:  # noqa: BLE001
        return base + "created_at > datetime('now', ?)", (f"-{int(WINDOW_MIN)} minutes",)


def _candidate_dbs() -> list[str]:
    """Every store chat turns could be landing in — main AND chatlog.

    On a SPLIT topology (the default) turns live in `agent_chatlog.db`, a
    different file from `agent_memory.db`. Checking only the main DB finds ZERO
    chat_log rows on a perfectly healthy install and reports "capture NOT
    writing" — a false alarm on every session start, which is exactly the
    cry-wolf failure this check exists to prevent. Measured 2026-09-07: the main
    store had 0 chat_log rows in the window while the chatlog store had 654.

    The chatlog path comes from `chatlog_config.chatlog_db_path()`, the SAME
    resolver the writers use (chatlog_core, m3_core.context). Do NOT hand-derive
    it as a sibling of the main DB: that guess ignores `CHATLOG_DB_PATH`, the
    active-database ContextVar, and a `db_path` pinned in .chatlog_config.json,
    so a user who relocated their chatlog would get the false alarm back — the
    check would be reading a file nobody writes to.

    On a UNIFIED deployment both resolve to the same file, so the list collapses
    to one entry and behaviour is unchanged.
    """
    try:
        import chatlog_config

        paths = chatlog_config.chat_store_paths()
        if paths:
            return paths
    except Exception:  # noqa: BLE001
        pass
    # Payload unavailable — a fresh install, a mid-upgrade window, or this hook
    # copied somewhere without its siblings. A SessionStart hook must still
    # answer, so fall back to the conventional layout. Main-ONLY would be the
    # false alarm again: on a split topology the main store legitimately holds
    # 0 chat_log rows, so we would report "capture NOT writing" precisely when
    # we are least able to know.
    main = _resolve_db()
    out = [main]
    sibling = os.path.join(os.path.dirname(main), "agent_chatlog.db")
    if os.path.abspath(sibling) != os.path.abspath(main) and os.path.exists(sibling):
        out.append(os.path.abspath(sibling))
    return out


def _state_verdict() -> str | None:
    """The verdict from the chatlog writer's own state file, or None when the
    state cannot answer (payload missing, or nothing recorded yet on SQLite,
    where the file count below still can).

    ``last_write_at`` is stamped by ``chatlog_core`` after every flush that
    LANDED, whatever the backend, and it is what ``m3 chatlog status`` reports.
    Reading it opens no database, so it is right on PostgreSQL, where the turns
    live in PG and the SQLite files this hook used to open are not the store: it
    created empty stray files there and cried "status UNKNOWN" every session.
    """
    _bin_on_path()
    try:
        import chatlog_config
    except Exception:  # noqa: BLE001 -- payload not importable: the file count decides
        return None
    try:
        with open(chatlog_config.STATE_FILE, encoding="utf-8") as f:
            last = json.load(f).get("last_write_at")
    except FileNotFoundError:
        last = None
    except (OSError, ValueError) as e:
        return f"\U0001f6a8 m3 chatlog check could not read {chatlog_config.STATE_FILE}: {e} — capture status UNKNOWN."
    if not last:
        if _primary_is_sqlite():
            return None
        return (
            f"\U0001f6a8 m3 chatlog capture has recorded no write yet ({chatlog_config.STATE_FILE} has no "
            "last_write_at) — capture status UNKNOWN. Run `m3 chatlog doctor`."
        )
    from datetime import UTC, datetime

    try:
        age_min = (datetime.now(UTC) - datetime.fromisoformat(str(last))).total_seconds() / 60
    except ValueError:
        return f"\U0001f6a8 m3 chatlog state holds an unreadable last_write_at ({last!r}) — capture status UNKNOWN."
    if age_min <= WINDOW_MIN:
        return f"✅ m3 chatlog capture: WORKING (last write {age_min:.0f} min ago)"
    return (
        f"\U0001f6a8 WARNING: m3 chatlog capture NOT writing (last write {age_min:.0f} min ago, "
        f"at {last}). Turns from this session are NOT being preserved. Run `m3 chatlog doctor` "
        "before continuing substantive work. (Note: an MCP disconnect does NOT cause this — "
        "capture writes to the DB directly, independent of that connection.)"
    )


def _primary_is_sqlite() -> bool:
    try:
        from m3_core.paths import resolve_backend_name

        return resolve_backend_name() == "sqlite"
    except Exception:  # noqa: BLE001 -- unknown: the SQLite file count is the old behaviour
        return True


def main() -> None:
    verdict = _state_verdict()
    if verdict is not None:
        print(json.dumps({"systemMessage": verdict, "suppressOutput": True}))
        return
    dbs = _candidate_dbs()
    try:
        _sql, _params = _recent_window_sql()
        count = 0
        errors = []
        for db in dbs:
            try:
                conn = sqlite3.connect(db, timeout=5)
                try:
                    (n,) = conn.execute(_sql, _params).fetchone()
                    count += n
                finally:
                    conn.close()
            except Exception as e:  # noqa: BLE001 — a missing store is not fatal
                errors.append(f"{os.path.basename(db)}: {e}")
        # Only a TOTAL failure is "status unknown": if any store answered, the
        # count is authoritative (writes land in one store, not spread).
        if errors and len(errors) == len(dbs):
            raise RuntimeError("; ".join(errors))
    except Exception as exc:  # noqa: BLE001 — never break session start
        msg = (
            f"\U0001f6a8 m3 chatlog check FAILED to query DB: {exc} "
            "— capture status UNKNOWN. Verify before trusting memory."
        )
        print(json.dumps({"systemMessage": msg}))
        return

    if count > 0:
        msg = f"✅ m3 chatlog capture: WORKING ({count} rows/{WINDOW_MIN}min)"
    else:
        msg = (
            f"\U0001f6a8 WARNING: m3 chatlog capture NOT writing "
            f"(0 rows in last {WINDOW_MIN}min). Turns from this session are NOT "
            "being preserved. Run `m3 chatlog doctor` before continuing "
            "substantive work. (Note: an MCP disconnect does NOT cause this — "
            "capture writes to the DB directly, independent of that connection.)"
        )
    print(json.dumps({"systemMessage": msg, "suppressOutput": True}))


if __name__ == "__main__":
    main()
    sys.exit(0)
