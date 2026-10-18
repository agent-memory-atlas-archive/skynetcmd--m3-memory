"""Doctor status-section renderers + their read helpers.

Extracted verbatim from installer.py. installer.py re-imports all of these so
`installer.<name>` still resolves (tests reference e.g. installer._roots_section
directly).

ONE CAREFUL CASE: `_resolve_chatlog_db` calls `_developer_bridge`, which is a
monkeypatch target that STAYS in installer.py (tests do
`monkeypatch.setattr(installer, "_developer_bridge", ...)`). A bound
`from m3_memory.installer import _developer_bridge` would capture the original
function object at import time and silently ignore any such patch. So we reach
it via the module object, resolved lazily inside the function body — exactly
the pattern used in m3_memory/wizard/persist.py for `_ask_yes_no`.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import cast

from m3_memory import _bin
from m3_memory._platform import os_name as _os_name


def _sqlite3_cli_hint() -> str | None:
    """Return a per-OS install instruction if the `sqlite3` CLI is missing.

    Returns None if the CLI is already on PATH (no action needed).
    Purely advisory — we don't run sudo commands on the user's behalf.
    """
    import shutil

    if shutil.which("sqlite3"):
        return None

    system = _os_name()
    if system == "Linux":
        # Try /etc/os-release for the package manager.
        distro = ""
        try:
            for line in Path("/etc/os-release").read_text().splitlines():
                if line.startswith("ID="):
                    distro = line.split("=", 1)[1].strip().strip('"')
                    break
        except OSError:
            pass
        if distro in ("debian", "ubuntu", "linuxmint", "pop"):
            cmd = "sudo apt install -y sqlite3"
        elif distro in ("fedora", "rhel", "centos", "rocky", "almalinux"):
            cmd = "sudo dnf install -y sqlite"
        elif distro in ("arch", "manjaro"):
            cmd = "sudo pacman -S sqlite"
        else:
            cmd = "install `sqlite3` with your package manager"
        return f"[!] `sqlite3` CLI not found -{cmd}"
    if system == "Darwin":
        # macOS ships sqlite3 in /usr/bin; if absent something's unusual.
        return "[!] `sqlite3` CLI not found (unexpected on macOS) — `brew install sqlite`"
    if system == "Windows":
        return "[!] `sqlite3` CLI not found -`winget install SQLite.SQLite` or download sqlite-tools from https://sqlite.org/download.html"
    return "[!] `sqlite3` CLI not found; install it for ad-hoc DB inspection"


def _resolve_chatlog_db(cfg: dict) -> Path | None:
    """Best-effort resolution of the chatlog DB path.

    Order: CHATLOG_DB_PATH env, M3_DATABASE env (shared DB case), config's
    chatlog_db_path, the DECOUPLED ENGINE ROOT (M3_ENGINE_ROOT / ~/.m3/engine —
    the canonical home for chatlog DBs in the decoupled layout, which is now the
    default), then the legacy <repo_path>/memory/agent_chatlog.db.

    The engine-root check MUST come before the repo-relative fallback: with
    decoupled roots, the chatlog DB lives at <engine>/agent_chatlog.db, NOT under
    the repo. Without this, doctor/`m3 status` read a non-existent repo-relative
    path and falsely report "no captures" even when a real chatlog DB exists in
    the engine root (observed 2026-06-27 on a macOS decoupled-roots install: a
    592MB agent_chatlog.db reported as empty).
    Returns None only when nothing resolves — i.e. the system isn't installed yet.
    """
    from m3_core.paths import configured_chatlog_db

    env_chatlog = configured_chatlog_db()
    if env_chatlog:
        return Path(env_chatlog).expanduser()
    from m3_core.paths import configured_main_db

    env_main = configured_main_db()
    if env_main:
        return Path(env_main).expanduser()
    cfg_chatlog = cfg.get("chatlog_db_path")
    if cfg_chatlog:
        return Path(cfg_chatlog).expanduser()
    # Decoupled engine root — the canonical location for chatlog DBs today.
    try:
        _bin.ensure_on_path()  # one rule; parents[2]/bin missed a wheel install
        from m3_sdk import get_m3_engine_root

        eng_db = Path(get_m3_engine_root()) / "agent_chatlog.db"
        if os.path.isfile(eng_db):
            return eng_db
    except Exception:  # noqa: BLE001 — fall through to the legacy paths below
        pass
    repo = cfg.get("repo_path")
    if repo:
        return Path(repo) / "memory" / "agent_chatlog.db"
    # Developer case: `pip install -e .` from a repo clone. No config file,
    # but the DB lives next to the sibling bridge we already resolve.
    #
    # _developer_bridge is a monkeypatch target that stays in installer.py —
    # reach it via the module object at call time, not a bound import, so a
    # test's monkeypatch.setattr(installer, "_developer_bridge", ...) is honored.
    from m3_memory import installer as _inst

    dev = _inst._developer_bridge()
    if dev:
        candidate = dev.parent.parent / "memory" / "agent_chatlog.db"
        if os.path.exists(candidate):
            return candidate
    return None


# Chat rows: every row of the chatlog's own types, live ones counted apart. The
# total includes soft-deleted (decayed) rows, which is what `m3 chatlog status`
# counts; the stores panel counts live rows. Both were shown as bare "rows" and
# read as a contradiction (2026-10-17). The
# newest row may have arrived by sync from another machine, so it is not the
# last capture on this one; that comes from the chatlog state file.
_CHAT_STATS_SELECT = (
    "SELECT COUNT(*), SUM(CASE WHEN COALESCE(is_deleted, 0) = 0 THEN 1 ELSE 0 END), MAX(created_at) "
    "FROM {table} WHERE type IN ('chat_log', 'message')"
)


def _chat_stats_row(out: dict, row) -> None:
    out.update(ok=True, rows=row[0] or 0, live=row[1] or 0, last_at=str(row[2] or ""))


def _last_local_capture() -> str:
    """When this machine last flushed a captured turn: the chatlog state file's
    ``last_write_at``, read by its one owner (chatlog_config.read_state, which
    warns on a corrupt file) — the same field `m3 chatlog status` reports. ""
    when none is recorded; the reason when the payload cannot be loaded."""
    try:
        _bin.ensure_on_path()
        from chatlog_config import read_state
    except ImportError as e:
        return f"(not readable: {e})"
    return str(read_state().get("last_write_at") or "")


def _print_chat_stats(stats: dict) -> None:
    print(f"  chat rows:               {stats['live']} live, {stats['rows']} stored incl. deleted")
    print(f"  newest row:              {stats['last_at'] or '(none)'}  (any machine, synced rows included)")
    print(f"  last capture here:       {_last_local_capture() or '(none recorded)'}")


def _chatlog_db_stats(db_path: Path) -> dict:
    """Open DB read-only, report row counts + last-capture timestamp.

    Returns {ok, rows, last_at, error}. Uses stdlib sqlite3 only — no
    dependency on the bin/ payload so doctor still works if the repo
    clone is incomplete.
    """
    out = {"ok": False, "rows": 0, "live": 0, "last_at": "", "error": ""}
    if not os.path.isfile(db_path):
        out["error"] = "file not found"
        return out
    try:
        from m3_core.paths import seam_backend

        _cm = seam_backend().open_readonly(str(db_path))
        conn = _cm.__enter__()
        try:
            _chat_stats_row(out, conn.execute(_CHAT_STATS_SELECT.format(table="memory_items")).fetchone())
        finally:
            _cm.__exit__(None, None, None)
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            # A fresh install before the first capture: an empty chat log, not
            # an unreadable one (reporting it unreadable made a new install
            # read DEGRADED).
            out["ok"] = True
        else:
            out["error"] = str(e)
    return out


def _chatlog_server_stats() -> tuple[str, dict] | None:
    """(table, stats) when the chatlog is tables in a server database, else None.

    On a file backend the chatlog is a SQLite file and _chatlog_db_stats reads
    it; resolving a file path on PostgreSQL found nothing and the section said
    "db not yet created (no captures written)" on a healthy install.
    """
    try:
        from m3_core.paths import seam_backend, store_is_a_file
        from memory.backends.dialect import chatlog_table
    except ImportError:  # stripped payload: the file-only report below
        return None
    if store_is_a_file():
        return None
    table = chatlog_table("items")
    out = {"ok": False, "rows": 0, "live": 0, "last_at": "", "error": ""}
    try:
        with seam_backend().open_readonly("") as conn:
            _chat_stats_row(out, conn.execute(_CHAT_STATS_SELECT.format(table=table)).fetchone())
    except Exception as e:  # noqa: BLE001 — doctor reports it; never raises
        out["error"] = f"{type(e).__name__}: {e}"
    return table, out


def _read_json(path: Path) -> dict | None:
    try:
        return cast("dict | None", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def _claude_hook_state() -> dict:
    """Detect Stop / PreCompact hooks in ~/.claude/settings.json."""
    settings = _read_json(Path.home() / ".claude" / "settings.json")
    if settings is None:
        return {"configured": False}
    hooks = settings.get("hooks") or {}
    stop = any("chatlog" in json.dumps(h).lower() for h in (hooks.get("Stop") or []))
    pre = any("chatlog" in json.dumps(h).lower() for h in (hooks.get("PreCompact") or []))
    return {"configured": True, "stop": stop, "precompact": pre}


def _gemini_hook_state() -> dict:
    """Detect MCP registration + SessionEnd hook in ~/.gemini/settings.json.

    Gemini CLI uses a single SessionEnd hook (no Stop/PreCompact split).
    Capture works when (a) the memory MCP is registered so in-session
    tool calls work and (b) the SessionEnd hook runs chatlog_ingest on exit.
    """
    settings = _read_json(Path.home() / ".gemini" / "settings.json")
    if settings is None:
        return {"configured": False}
    mcp = settings.get("mcpServers") or {}
    hooks = settings.get("hooks") or {}
    session_end = any("chatlog" in json.dumps(h).lower() for h in (hooks.get("SessionEnd") or []))
    return {
        "configured": True,
        "memory_mcp": "memory" in mcp,
        "session_end": session_end,
    }


def _chatlog_section(cfg: dict) -> int:
    """Emit the chatlog health section. Returns 0 (informational — never
    the reason doctor exits nonzero)."""
    print()
    print("chatlog subsystem:")

    pg_chat = _chatlog_server_stats()
    if pg_chat is not None:
        table, stats = pg_chat
        print(f"  store:                   {table} (in the primary database)")
        if stats["ok"]:
            _print_chat_stats(stats)
        else:
            print(f"  status:                  unreadable ({stats['error']})")
    elif (db_path := _resolve_chatlog_db(cfg)) is None:
        print("  db_path:                 (unresolved — install-m3 not run yet)")
    else:
        print(f"  db_path:                 {db_path}")
        stats = _chatlog_db_stats(db_path)
        if stats["ok"]:
            _print_chat_stats(stats)
        elif stats["error"] == "file not found":
            print("  status:                  db not yet created (no captures written)")
        else:
            print(f"  status:                  unreadable ({stats['error']})")

    claude = _claude_hook_state()
    if not claude["configured"]:
        print("  claude hooks:            ~/.claude/settings.json not found")
    else:
        stop_mark = "[on]" if claude["stop"] else "[off]"
        pre_mark = "[on]" if claude["precompact"] else "[off]"
        print(f"  claude hooks:            Stop {stop_mark}  PreCompact {pre_mark}")

    gemini = _gemini_hook_state()
    if not gemini["configured"]:
        print("  gemini mcp:              ~/.gemini/settings.json not found")
    else:
        mcp_mark = "[on]" if gemini["memory_mcp"] else "[off]"
        se_mark = "[on]" if gemini["session_end"] else "[off]"
        print(f"  gemini mcp (memory):     {mcp_mark}  SessionEnd {se_mark}")
    return 0


def _embedder_tier_section() -> None:
    """Emit the 'which embedder tier is live' line (Project Oxidation status).

    Tells the user whether the native in-process embedder (the oxidized hot
    path) is active or whether m3 is on the pure-Python HTTP fallback. Purely
    informational — never changes doctor's exit code. Best-effort: if the
    rust_core_install probe isn't importable, stay silent rather than error.
    """
    try:
        from m3_memory.rust_core_install import active_embedder_tier

        tier = active_embedder_tier()
    except Exception:  # noqa: BLE001 — informational only
        return
    print()
    print("embedder (Project Oxidation):")
    if tier.get("native"):
        print(f"  status:                  {tier['summary']}")
    else:
        # Wrap the longer fallback summary so it stays readable in a terminal.
        print("  status:                  pure-Python fallback")
        for line in tier["summary"].split(" — "):
            print(f"    {line.strip()}")
    _shared_embedder_status()


def _shared_embedder_status() -> None:
    """Report whether m3 is in SHARED-embedder mode (.embed_config.json disables
    the per-process in-process embedder and defers to one shared server), and if
    so, HEALTH-CHECK that server. A shared config pointing at a DEAD endpoint is a
    silent-failure trap (§3): every embed would slow-cascade or fail — so warn
    loudly. Read-only; never changes doctor's exit code. Best-effort."""
    try:
        import json
        import os

        from m3_memory import _roots

        path = os.path.join(_roots.config_root(), ".embed_config.json")
        if not os.path.exists(path):
            # Shared mode is the shipped DEFAULT, so its absence is an anomaly to
            # flag, not a neutral state. A per-process fleet means N embedder
            # copies in host RAM and no single self-healed server.
            print(
                "  mode:                    [WARN] per-process — shared mode NOT enabled (this is the shipped default)"
            )
            print("    fix: re-run `m3 setup` (enables it automatically), or `m3 embedder shared` to enable it now.")
            print(
                "    then register the keep-alive task: "
                "`python bin/install_schedules.py --add embed-server` (admin shell)."
            )
            return
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except Exception as e:  # noqa: BLE001 — informational only
        print(f"  mode:                    UNKNOWN (.embed_config.json unreadable: {e})")
        return

    if not cfg.get("disable_inproc_embedder"):
        print(
            "  mode:                    per-process (.embed_config.json present but in-process embedder not disabled)"
        )
        return

    try:
        _bin.ensure_on_path()
        from m3_core import tier2_client
    except Exception as e:  # noqa: BLE001 — informational only
        print(f"  shared server:           UNKNOWN (cannot load the tier-2 client: {type(e).__name__}: {e})")
        return
    url, source = tier2_client.fallback_url(cfg)
    print(f"  mode:                    SHARED — deferring to {url} ({source})")
    # One classifier for every tier-2 check: a 401 or an untrusted certificate
    # is a reachable server with the wrong credentials, not a dead one, and gets
    # its own fix. Non-http(s) URLs are refused unread (bandit B310).
    diag, body = tier2_client.health(url, timeout=3)
    if diag.state == "ok":
        model = body.get("model")
        detail = f"model={model}, dim={body.get('dim')}" if model else "plaintext OK; no model reported"
        print(f"  shared server:           OK ({detail})")
        return
    if diag.state == "loading":
        print("  shared server:           [WARN] up but still loading its model — embeds slow-cascade until it serves.")
        return
    label = "UNREACHABLE" if not diag.answering else "MISCONFIGURED"
    print(f"  shared server:           [WARN] {label} ({diag.state}) — {diag.detail}")
    if diag.fix:
        print(f"    fix: {diag.fix}")
    if not diag.answering:
        print("    m3 is configured to defer to it; or run `m3 embedder unshared` to revert to per-process embedders.")


def _crypto_section() -> None:
    """Emit the crypto/FIPS status line (backend, FIPS tier, trusted lib path).

    Shows whether crypto runs on the DEFAULT (Python) backend or wolfCrypt, the
    FIPS tier, and — importantly for security review — WHICH absolute, trusted
    path the wolfSSL library was loaded from (M3 never loads it by bare name).
    Best-effort and read-only; never changes doctor's exit code.
    """
    try:
        _bin.ensure_on_path()  # one rule; parents[2]/bin missed a wheel install
        from crypto_provider import active_crypto_status

        cs = active_crypto_status()
    except Exception:  # noqa: BLE001 — informational only
        return
    print()
    print("crypto (FIPS):")
    tier = "strict" if cs["fips_strict"] else ("mode" if cs["fips_mode"] else "off")
    print(f"  backend:                 {cs['backend']}  (FIPS: {tier})")
    if cs["backend"] == "WOLFSSL" and cs.get("lib_path"):
        print(
            f"  wolfSSL loaded from:     {cs['lib_path']}" + ("  [integrity-pinned]" if cs["integrity_pinned"] else "")
        )
        print(f"  validated FIPS module:   {'yes' if cs['fips_validated'] else 'no (open-source build)'}")
        sha = cs.get("lib_sha256")
        if sha:
            print(f"  loaded lib SHA-256:      {sha}")
            if not cs["integrity_pinned"]:
                # Help the operator self-pin THEIR build (M3 doesn't ship wolfSSL).
                print("    (to detect later tampering, pin your trusted build:")
                print(f"     m3 config set crypto.wolfssl_sha256 {sha} )")
    else:
        print(f"  status:                  {cs['summary']}")


def _roots_section() -> None:
    """Report the decoupled three-root layout and flag the split-brain hazard.

    The engine (DBs) and config roots can be relocated independently of the
    repo (M3_MEMORY_ROOT / M3_ENGINE_ROOT / M3_CONFIG_ROOT). When only SOME of
    those env vars are pinned, the MCP server and the chatlog hook can resolve
    different roots (the documented split-brain — see CLAUDE.md "Homecoming
    Architecture"). doctor is the natural place to surface where each root
    actually resolves and to warn when the pinning is partial.

    Best-effort: if the SDK resolvers aren't importable (stripped env), skip
    the section rather than fail doctor.
    """
    try:
        _bin.ensure_on_path()  # one rule; parents[2]/bin missed a wheel install
        from m3_sdk import (
            get_m3_config_root,
            get_m3_engine_root,
            get_m3_root,
        )
    except Exception:  # noqa: BLE001 — informational only
        return

    from m3_memory import _roots

    def _src(key: str, default: str) -> str:
        if _roots.file_root(key):
            return f"(m3_config.toml [paths] {key})"
        if key != "memory_root" and _roots.memory_root_pin():
            return "(derived from [paths] memory_root)"
        return f"(default {default})"

    print()
    print("decoupled roots:")
    mem = get_m3_root()
    cfg_root = get_m3_config_root()
    eng_root = get_m3_engine_root()
    mem_src = _src("memory_root", "~/.m3" if _roots.memory_root_migrated() else "~/.m3-memory")
    _cfg_root, cfg_tier = _roots.config_root_with_tier()
    cfg_src = {_roots.CONFIG_ROOT_ARG: "(--config)", _roots.CONFIG_ROOT_ENV: "(M3_CONFIG_ROOT)"}.get(
        cfg_tier, "(default ~/.m3/config)"
    )
    eng_src = _src("engine_root", "~/.m3/engine")
    print(f"  memory root (repo/state): {mem}  {mem_src}")
    print(f"  config root:              {cfg_root}  {cfg_src}")
    print(f"  engine root (DBs):        {eng_root}  {eng_src}")

    # Engine DBs presence — the thing users actually care about.
    eng = Path(eng_root)
    dbs = sorted(p.name for p in eng.glob("*.db")) if os.path.isdir(eng) else []
    if dbs:
        print(f"  engine DBs present:       {', '.join(dbs)}")
    elif os.path.isdir(eng):
        print("  engine DBs present:       (none yet — no captures/migrations written)")
    else:
        print("  engine DBs present:       (engine root does not exist yet)")


def _fmt_dual_time(value: object) -> str:
    """'LOCAL (ZULU)' timestamp, e.g. ``2026-07-19 14:32:01 EDT (2026-07-19T18:32:01Z)``:
    m3_core.runtime.local_and_utc owns the format."""
    _bin.ensure_on_path()
    from m3_core.runtime import local_and_utc

    return str(local_and_utc(value))


def _rows_fn(conn):
    """A ``(sql, params) -> rows`` callable over a connection with ``.execute``
    (sqlite3, the seam's compat connections) or a bare DB-API one (psycopg2)."""
    if hasattr(conn, "execute"):
        return lambda sql, params=(): conn.execute(sql, params).fetchall()
    cur = conn.cursor()

    def rows(sql, params=()):
        cur.execute(sql, params)
        return cur.fetchall()

    return rows


def _table_stats(rows, dialect, table: str, *, items: bool) -> dict | None:
    """{rows, last_updated} for one store table, or None when it is absent.

    ``items``: a memory_items-shaped table (live rows, last update); else a plain
    count. Names come from the seam (chatlog_table_for, qualified_table), so the
    same call reports a SQLite file's table or a PostgreSQL schema's.
    """
    sql, params = dialect.table_exists(table)
    if not rows(sql, params):
        return None
    if items:
        n = rows(f"SELECT COUNT(*) FROM {table} WHERE COALESCE(is_deleted,0)=0")[0][0]
        last = rows(f"SELECT MAX(COALESCE(updated_at, created_at)) FROM {table}")[0][0]
        return {"rows": n, "last_updated": last, "count": "live rows"}
    return {"rows": rows(f"SELECT COUNT(*) FROM {table}")[0][0], "last_updated": None, "count": "rows"}


def _sqlite_store_stats(db_path: str) -> dict | None:
    """(rows, last_updated) for a SQLite store file, or None if absent/unreadable.

    Read-only, best-effort. ``rows`` counts live memory_items; ``last_updated`` is
    the max of updated_at/created_at seen. A store that lacks memory_items (e.g. a
    files DB) reports rows via its own primary table when recognizable, else 0.
    """
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        from m3_core.paths import seam_backend

        _cm = seam_backend().open_readonly(str(db_path))
        conn = _cm.__enter__()
    except sqlite3.Error:
        return None
    try:
        from memory.backends.dialect import dialect_for

        d, rows = dialect_for("sqlite"), _rows_fn(conn)
        return (
            _table_stats(rows, d, "memory_items", items=True)
            or _table_stats(rows, d, "leaves", items=False)  # files DB
            or {"rows": 0, "last_updated": None}
        )
    except sqlite3.Error:
        return None
    finally:
        _cm.__exit__(None, None, None)


def _cdw_sync_section() -> None:
    """If a CDW (data warehouse) DSN is defined, report last sync watermarks.

    The warehouse DSN is resolve_cdw_pg_dsn (M3_CDW_PG_URL > PG_URL deprecated).
    Sync watermarks live on the SQLite side in ``sync_watermarks(direction TEXT
    PK, last_synced_at TEXT)`` (pg_sync.py). Report each direction's last sync in
    LOCAL (ZULU) time. Silent when no CDW is configured (the common case).
    """
    try:
        _bin.ensure_on_path()  # one rule; parents[2]/bin missed a wheel install
        from m3_sdk import PgRole, resolve_db_path, resolve_pg_dsn
    except Exception:  # noqa: BLE001 — informational only
        return
    try:
        cdw = resolve_pg_dsn(PgRole.WAREHOUSE)
    except Exception:  # noqa: BLE001
        cdw = ""
    if not cdw:
        return  # no warehouse configured — nothing to report

    from m3_core.secret_store import mask_dsn_passwords  # the one masker

    masked = mask_dsn_passwords(cdw)
    print()
    print("data warehouse (CDW) sync:")
    print(f"  warehouse DSN:            {masked}")

    core_db = ""
    try:
        core_db = resolve_db_path(None)
    except Exception:  # noqa: BLE001
        pass
    if not core_db or not os.path.exists(core_db):
        print("  [!] no local store to read sync watermarks from.")
        return
    try:
        from m3_core.paths import seam_backend

        _cm = seam_backend().open_readonly(str(core_db))
        conn = _cm.__enter__()
    except sqlite3.Error:
        print("  [!] could not open the local store to read watermarks.")
        return
    try:
        have = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_watermarks'").fetchone()
        if not have:
            print("  last sync:                never (no sync_watermarks yet)")
            return
        rows = conn.execute("SELECT direction, last_synced_at FROM sync_watermarks ORDER BY direction").fetchall()
        if not rows:
            print("  last sync:                never (no watermarks recorded)")
            return
        for direction, ts in rows:
            print(f"  {direction:<24} {_fmt_dual_time(ts)}")
    except sqlite3.Error as e:
        print(f"  [!] could not read watermarks: {e}")
    finally:
        _cm.__exit__(None, None, None)


def _backend_section(cfg: dict) -> None:
    """Report the active PRIMARY database backend and, on PostgreSQL, its health.

    The runtime selects its backend from registry key ``db.backend``
    (m3_config.toml; ``M3_DB_BACKEND`` only as a legacy name below the file) and
    the DSN from the secrets chain. The installer's ``db_backend`` record in
    config.json is a report, not the switch, so a mismatch is a real footgun
    (the operator thinks they're on PG but the process still opens SQLite, or
    vice-versa). doctor is the place to surface both and flag the divergence,
    and to confirm a PG DSN is reachable rather than let the first real query be
    the discovery.

    Best-effort: importing the seam can fail on a stripped env — skip the section
    rather than crash doctor.
    """
    try:
        _bin.ensure_on_path()  # one rule; parents[2]/bin missed a wheel install
        from memory.backends import resolve_backend_name
    except Exception:  # noqa: BLE001 — informational only
        return

    print()
    print("primary database backend:")
    try:
        live = resolve_backend_name()
    except Exception as e:  # noqa: BLE001 — a bad db.backend value lands here
        print(f"  [!] backend unresolved: {e}")
        return
    try:
        from m3_core.config.settings import origin_of

        src = f"({origin_of('db.backend')})"
    except Exception as e:  # noqa: BLE001 -- doctor reports, never crashes
        src = f"(origin unknown: {e})"
    print(f"  active backend (runtime): {live}  {src}")

    # The installer's record vs the selector: the switch is db.backend.
    recorded = cfg.get("db_backend")
    if recorded and recorded != live:
        print(f"  [!] config.json records db_backend={recorded!r} but db.backend resolves to {live!r}.")
        print(f"      fix: `m3 config set db.backend {recorded}` (the runtime reads that), or re-run install.")

    if live == "sqlite":
        # Report the three local stores (core / chat / files) with full paths,
        # row counts, and last-updated time. Chat and core may be the SAME file
        # (chatlog_db == main_db) — dedupe the display. "If present and
        # discernible": a store that doesn't exist is shown as absent, not an error.
        try:
            _bin.ensure_on_path()  # one rule; parents[2]/bin missed a wheel install
            from m3_sdk import resolve_db_path

            core_db = resolve_db_path(None)
            try:
                # chatlog_db_path(), NOT the DEFAULT_DB_PATH constant. That
                # constant is evaluated at IMPORT time, so it never sees
                # CHATLOG_DB_PATH / M3_CHATLOG_DB_PATH, the active-database
                # ContextVar, or a `db_path` pinned in .chatlog_config.json —
                # `m3 doctor` reported the DEFAULT path to anyone who had
                # relocated their chatlog, i.e. was confidently wrong exactly
                # where a user most needs the truth.
                from chatlog_config import chatlog_db_path

                chat_db = chatlog_db_path()
            except Exception:  # noqa: BLE001
                chat_db = ""
            try:
                from memory.config import FILES_DB_PATH as files_db
            except Exception:  # noqa: BLE001
                files_db = ""
        except Exception:  # noqa: BLE001 — informational only
            return

        stores = [("core", core_db)]
        if chat_db and os.path.abspath(chat_db) != os.path.abspath(core_db or ""):
            stores.append(("chat", chat_db))
        else:
            stores.append(("chat", core_db))  # same file as core
        if files_db:
            stores.append(("files", files_db))

        print("  stores (SQLite):")
        seen_paths: set = set()
        for label, path in stores:
            if not path:
                print(f"    {label:<6} —  (path not discernible)")
                continue
            ap = os.path.abspath(path)
            shared = "  (shared with core)" if ap in seen_paths else ""
            seen_paths.add(ap)
            stats = _sqlite_store_stats(path)
            if stats is None:
                print(f"    {label:<6} {path}  (absent){shared}")
            else:
                last = _fmt_dual_time(stats["last_updated"]) if stats["last_updated"] else "—"
                print(f"    {label:<6} {path}")
                print(f"           {stats.get('count', 'rows')}: {stats['rows']}   last updated: {last}{shared}")

        _cdw_sync_section()
        return

    # PostgreSQL: report the (masked) DSN and probe reachability.
    try:
        from m3_sdk import PgRole, resolve_pg_dsn

        dsn = resolve_pg_dsn(PgRole.PRIMARY)
    except Exception:  # noqa: BLE001
        dsn = ""
    if not dsn:
        print("  [!] db.backend is postgres but no primary DSN is stored, so the primary store")
        print("      cannot open. fix: `m3 secret set M3_PRIMARY_PG_URL` (prompts for it).")
        return

    from m3_core.secret_store import mask_dsn_passwords  # the one masker

    masked = mask_dsn_passwords(dsn)
    print(f"  primary DSN:              {masked}")
    try:
        from m3_core.pgconn import pg_connect  # FIPS transport check, one owner

        conn = pg_connect(dsn, connect_timeout=5)
        cur = conn.cursor()
        # Identity (host/db) for the panel — prepared as the same shape other
        # SQL backends (MariaDB, …) would report.
        try:
            cur.execute("SELECT current_database(), inet_server_addr()::text, current_schema()")
            dbname, host, schema = cur.fetchone()
        except Exception:  # noqa: BLE001 — older PG / restricted role
            dbname = host = schema = None
        # Every store is a set of tables in this one database. Report the same
        # three the SQLite panel lists, named through the seam.
        from memory.backends.dialect import chatlog_table_for, dialect_for

        d = dialect_for("postgres")
        rows = _rows_fn(conn)
        store_stats = [
            ("core", "memory_items", _table_stats(rows, d, "memory_items", items=True)),
            (
                "chat",
                chatlog_table_for("items", "postgres"),
                _table_stats(rows, d, chatlog_table_for("items", "postgres"), items=True),
            ),
            (
                "files",
                d.qualified_table("leaves", schema="files"),
                _table_stats(rows, d, d.qualified_table("leaves", schema="files"), items=False),
            ),
        ]
        conn.close()
        if dbname:
            print(f"  identity:                 db={dbname} host={host or 'local'} schema={schema or 'public'}")
        print("  reachable:                yes")
        print("  stores (PostgreSQL tables):")
        for label, table, st in store_stats:
            if st is None:
                print(f"    {label:<6} {table}  (absent)")
                continue
            last = _fmt_dual_time(st["last_updated"]) if st["last_updated"] else "—"
            print(f"    {label:<6} {table}")
            print(f"           {st['count']}: {st['rows']}   last updated: {last}")
    except Exception as e:  # noqa: BLE001 — connection / auth / undefined-table
        from m3_core.secret_store import mask_dsn_passwords  # the one masker

        detail = mask_dsn_passwords(str(e).strip())
        print(f"  reachable:                NO — {detail}")
        print("      Start/reach the PostgreSQL server. The schema builds")
        print("      automatically on the first successful connect.")

    _cdw_sync_section()


# The legacy env names that LOCATE a store, each with its fix. m3 does not read
# them and never moves them into a file: a database PATH is a locator, so the
# fix is a flag, a named store or the chatlog config's db_path (M3_DATABASE design
# Q2, 2026-10-12; chatlog design, 2026-10-13).
LOCATOR_ENV_FIXES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("M3_DATABASE",),
        "pass --database PATH to the command, or add [stores.<name>] to m3_config.toml and pass --store NAME",
    ),
    (
        ("M3_CHATLOG_DB_PATH", "CHATLOG_DB_PATH", "M3_CHATLOG_DB", "CHATLOG_DB", "M3_CHATLOG_DATABASE"),
        "pass --chatlog-db PATH to the command, or set db_path in .chatlog_config.json",
    ),
)


def locator_env_leftovers() -> list[str]:
    """Report lines for every place a store-locating legacy env name is still set,
    with its fix; [] when none. Shared by `m3 doctor` and `m3 config import-env`."""
    from m3_memory.install.env_sources import scan

    unchecked: list[str] = []
    found = scan([n for names, _ in LOCATOR_ENV_FIXES for n in names], unchecked=unchecked)
    lines = [
        f"  [i] could not check {u} for the legacy store names; inspect it by hand." for u in sorted(set(unchecked))
    ]
    for names, fix in LOCATOR_ENV_FIXES:
        for name in names:
            places = sorted({f.place for f in found if f.name == name and f.value.strip()})
            if places:
                lines.append(
                    f"  [i] {name} is set in: "
                    + "; ".join(places)
                    + f". m3 does not read it. fix: {fix}; then delete it there."
                )
    return lines


def _database_env_section() -> None:
    try:
        lines = locator_env_leftovers()
    except Exception as e:  # noqa: BLE001 -- doctor reports, never crashes
        lines = [f"  [!] could not check where the legacy store names are set: {e}"]
    if lines:
        print()
        print("\n".join(lines))


def _inside(path: str, root: str) -> bool:
    p, r = (os.path.normcase(os.path.realpath(x)) for x in (path, root))
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different drives
        return False


def git_tree_of(path: str) -> str | None:
    """The git working tree ``path`` sits in, or None."""
    d = os.path.dirname(os.path.realpath(path))
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _secrets_file_exposure_section(brief: bool = False) -> None:
    """Warn when the secrets file sits where it can leave this machine: inside a
    git working tree (a commit publishes it) or a synced folder (the sync client
    uploads it). The file is plaintext by decision, protected only
    by its permissions, so where it lives is the protection.
    Silent when there is no secrets file or it is in neither."""
    try:
        _bin.ensure_on_path()
        from m3_core.secret_store import secrets_file_path

        from m3_memory._platform import synced_folder_roots

        path = str(secrets_file_path())
    except Exception as e:  # noqa: BLE001 -- doctor reports, never crashes
        print()
        print(f"  [!] could not locate the secrets file to check where it lives: {e}")
        return
    if not os.path.isfile(path):
        return
    problems = []
    tree = git_tree_of(path)
    if tree:
        problems.append(f"it is inside the git working tree {tree}: a commit would publish it")
    problems += [
        f"it is inside {service} ({root}): the sync client uploads it"
        for service, root in synced_folder_roots()
        if _inside(path, root)
    ]
    for problem in problems:
        print()
        print(f"  [!] the secrets file {path} holds plaintext secrets, and {problem}.")
        print(
            "      fix: move it out, and point `secrets.file` in m3_config.toml at the new place "
            "(`m3 config set secrets.file <path> --yes`)."
        )
    _secrets_file_disk_encryption(path, brief=brief)


def _secrets_file_disk_encryption(path: str, *, brief: bool = False) -> None:
    """Warn when the secrets file holds VALUES on a disk that is not encrypted
    (BitLocker / FileVault off): anyone with the disk or a copy reads them. Names
    each secret and the shell-independent command that moves it to the OS
    keyring, never a value (user design, 2026-10-12). Silent when the disk is
    encrypted, the file holds no values, or this OS is not checked."""
    from m3_core.secret_store import SecretsFileError, read_secrets

    from m3_memory._platform import volume_encryption

    enc = volume_encryption(path)
    if enc is None or enc.encrypted is True:
        return
    try:
        names = sorted(read_secrets())  # entries holding a value, not keyring pointers
    except SecretsFileError as e:
        print()
        print(f"  [!] could not read the secrets file to check its disk: {e}")
        return
    if not names:
        return
    count = f"{len(names)} secret{'s' if len(names) != 1 else ''}"
    print()
    if brief:  # doctor's default: the finding in one line, the detail under --verbose
        # [i] for an unknown state: a warning for what was not observed is a false alarm (§3).
        mark, state = ("[i]", "a disk m3 could not check") if enc.encrypted is None else ("[!]", "an unencrypted disk")
        print(
            f"  {mark} the secrets file holds {count} in plain text on {state} ({enc.detail}); "
            "`m3 doctor --verbose` shows how to move them."
        )
        return
    if enc.encrypted is None:
        print(f"  [i] could not tell whether the disk holding the secrets file is encrypted ({enc.detail});")
        print(f"      {path} holds {count} in plain text.")
        return
    print(f"  [!] the secrets file {path} holds {count} in plain text, and its disk is not")
    print(f"      encrypted ({enc.detail}): anyone with the disk or a copy of it can read them.")
    print(f"      Turn on disk encryption, or move them to {enc.keyring}. Type only the command")
    print("      shown, NOT the secret: m3 then asks you for the value (nothing is shown as you")
    print("      type). The same in any shell:")
    for name in names:
        print(f"        m3 secret set {name} --keyring")


def _secrets_in_env_section(brief: bool = False) -> None:
    """Name m3 secrets still set in the environment, rc files, `.env`, the
    Windows registry or MCP env blocks, which m3 no longer reads.
    [!] for one m3 cannot resolve from the secrets file or keyring: that feature
    is off until it is stored. [i] for a leftover plaintext copy of one it does
    resolve. Another tool's key in its own MCP entry is listed under --verbose
    only, by name and file. Names and places only, never values."""
    try:
        _bin.ensure_on_path()
        from m3_core.config import REGISTRY, KeyClass
        from m3_core.secret_store import explain_named_secret

        from m3_memory.install.env_sources import scan

        unchecked: list[str] = []
        found = scan([n for k in REGISTRY if k.klass is KeyClass.SECRET for n in k.env_names], unchecked=unchecked)
    except Exception as e:  # noqa: BLE001 -- doctor reports, never crashes
        print()
        print(f"  [!] could not check where secrets are still set: {e}")
        return
    ours: dict[str, list] = {}
    others = []
    for f in found:
        if not f.value.strip():
            continue
        if not f.m3_surface:
            others.append(f)
            continue
        key = REGISTRY.by_env(f.name)
        if key is not None:
            ours.setdefault(key.name, []).append(f)
    if not ours and (brief or not (others or unchecked)):
        return
    unresolved = sorted(n for n in ours if not explain_named_secret(n).value)
    leftover = sorted(n for n in ours if n not in unresolved)
    print()
    if brief:
        if unresolved:
            print(
                f"  [!] {', '.join(unresolved)}: set only in the environment or startup files, which m3 no "
                "longer reads for secrets; `m3 config import-env` stores them (`m3 doctor --verbose` lists where)."
            )
        if leftover:
            print(
                f"  [i] {len(leftover)} secret(s) m3 reads from the secrets file or keyring also have plaintext "
                "copies in the environment or startup files; `m3 doctor --verbose` lists where."
            )
        return
    for name in unresolved:
        print(f"  [!] {name} is set only where m3 no longer reads secrets, so m3 runs without it:")
        print(f"        {', '.join(sorted({f'{f.place} ({f.name})' for f in ours[name]}))}")
        print(f"      fix: `m3 config import-env` (moves it), or `m3 secret set {name}` (prompts).")
    for name in leftover:
        # A startup-file line that looks the value up (Keychain, a password
        # manager) stores nothing on disk: the exposure is that every process
        # started from that shell inherits it. Calling it a "plaintext copy"
        # was wrong (2026-10-17).
        literal = sorted({f"{f.place} ({f.name})" for f in ours[name] if not f.computed})
        looked_up = sorted({f"{f.place} ({f.name})" for f in ours[name] if f.computed})
        if literal:
            print(f"  [i] {name}: plaintext copies m3 does not read; remove them once nothing else does:")
            print(f"        {', '.join(literal)}")
        if looked_up:
            print(
                f"  [i] {name}: exported into every shell's environment by a lookup at startup (no value stored "
                "there), which m3 does not read; drop the export once nothing else needs it:"
            )
            print(f"        {', '.join(looked_up)}")
    for f in sorted(others, key=lambda f: (f.name, f.place)):
        print(f"  [i] {f.name} in {f.place}: another tool's key; m3 leaves it there.")
    for u in sorted(set(unchecked)):
        print(f"  [i] could not check {u} for secrets; inspect it by hand.")


def config_root_section(brief: bool = False) -> int:
    """Which m3_config.toml is authoritative, and every place that would read a
    different one. Locator order: --config > M3_CONFIG_ROOT > ~/.m3/config.
    With a non-default root, a process that does not see M3_CONFIG_ROOT (an m3
    MCP server entry without it, a new terminal, a service) silently reads
    ~/.m3/config: two configs, possibly two stores. Reported: the file in use and
    its locator; places where M3_CONFIG_ROOT names another root; m3 MCP entries
    missing it; and a second config file that is ignored. Returns the number of
    problems (exit-code neutral). Paths only, never values of settings."""
    try:
        _bin.ensure_on_path()
        from m3_core.config import locate

        from m3_memory._roots import CONFIG_FILE_NAME, default_config_root, same_dir
        from m3_memory.install.env_sources import _server_entries, is_m3_server, mcp_config_files, scan

        loc = locate()
        unchecked: list[str] = []
        found = scan(["M3_CONFIG_ROOT"], unchecked=unchecked)
    except Exception as e:  # noqa: BLE001 -- doctor reports, never crashes
        print()
        print(f"  [!] could not check which m3_config.toml is authoritative: {e}")
        return 0
    root = str(loc.root)
    default = default_config_root()
    non_default = not same_dir(root, default)
    problems: list[str] = []
    for f in found:
        if not f.m3_surface:
            continue  # another tool's entry: not a surface m3 runs from
        value = os.path.expanduser(f.value.strip())
        if value and not same_dir(value, root):
            problems.append(
                f"M3_CONFIG_ROOT in {f.place} names {value}, but this process uses {root}: processes "
                f"started from there read another config. fix: set it to {root} there, or remove it"
            )
    if non_default:
        try:
            files = mcp_config_files()
        except Exception:  # noqa: BLE001 -- the sweep above already reported what it could
            files = []
        for path in files:
            try:
                import json

                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh) or {}
            except (OSError, ValueError):
                continue
            for server, entry in _server_entries(data if isinstance(data, dict) else {}):
                entry = entry if isinstance(entry, dict) else {}
                if is_m3_server(server, entry) and not (entry.get("env") or {}).get("M3_CONFIG_ROOT"):
                    problems.append(
                        f"{path} (server {server!r}) has no M3_CONFIG_ROOT, so that server reads {default}. "
                        "fix: `m3 doctor --fix` writes it"
                    )
    second = os.path.join(default, CONFIG_FILE_NAME)
    if non_default and os.path.isfile(second):
        problems.append(f"{second} also exists and is ignored (the config in use is {loc.file}); remove or merge it")
    locator = {
        "arg:--config": "--config",
        "env:M3_CONFIG_ROOT": "M3_CONFIG_ROOT",
    }.get(str(loc.tier), "the default location")
    if brief and not problems:
        return 0
    print()
    print(f"config: {loc.file} (located by {locator})")
    for line in problems:
        print(f"  [!] {line}")
    for u in sorted(set(unchecked)):
        print(f"  [i] could not check {u} for M3_CONFIG_ROOT; inspect it by hand")
    if non_default and not problems:
        scope = "readable " if unchecked else ""
        print(f"  [i] every {scope}m3 surface found names {root}")
    if non_default:
        print(
            f"  [i] set M3_CONFIG_ROOT={root} wherever m3 runs (your shell or user environment, each agent's "
            "MCP server entry, services); m3 setup writes it where m3 owns the entry"
        )
    return len(problems)


def m3_env_settings_section(brief: bool = False) -> int:
    """The last doctor section: every m3 setting still set as an M3_*
    environment variable (the process, a shell startup file, `.env`, the Windows
    registry, an m3 MCP entry's env block, or an m3 service definition). m3 does
    not read them: m3_config.toml has precedence and a key it lacks takes its
    default. Each is listed with where it is set, what m3 uses instead, and the
    advice to delete it. m3 reads one variable, M3_CONFIG_ROOT, and that one only
    when it is not the default. Names and places only, never values. Silent when
    nothing is set. Returns the number reported (exit-code neutral)."""
    try:
        _bin.ensure_on_path()
        from m3_core.config import REGISTRY, KeyClass
        from m3_core.config import settings as _settings
        from m3_core.paths import get_m3_config_root

        from m3_memory._roots import default_config_root, same_dir
        from m3_memory.install.env_sources import scan

        unchecked: list[str] = []
        names = [n for k in REGISTRY if k.klass is KeyClass.CONFIG for n in k.env_names if n.startswith("M3_")]
        found = scan([*names, "M3_CONFIG_ROOT"], unchecked=unchecked)
        current = _settings.current()
    except Exception as e:  # noqa: BLE001 -- doctor reports, never crashes
        print()
        print(f"  [!] could not check which M3_* variables are still set: {e}")
        return 0
    by_name: dict[str, set[str]] = {}
    config_root_places: set[str] = set()
    for f in found:
        if not f.value.strip() or not f.m3_surface:
            continue
        if f.name == "M3_CONFIG_ROOT":
            value = os.path.expanduser(f.value)
            if same_dir(value, get_m3_config_root()) and same_dir(value, default_config_root()):
                config_root_places.add(f.place)
            continue
        by_name.setdefault(f.name, set()).add(f.place)
    if not by_name and not config_root_places:
        return 0
    from m3_core.config import locate

    cfg_file = locate().file
    print()
    print(f"M3_* environment variables m3 does not read ({cfg_file} has precedence):")
    from_default = 0
    for name in sorted(by_name):
        key = REGISTRY.by_env(name)
        if key is None:
            continue
        where = ", ".join(sorted(by_name[name]))
        in_file = current.origin(key).startswith("file")
        from_default += not in_file
        uses = "the value in m3_config.toml" if in_file else "the default (m3_config.toml does not set it)"
        print(f"  [not read] {name} -> {key.name}: m3 uses {uses}; set in {where}")
        if not in_file:
            print(f"      to choose a value: `m3 config set {key.name} <value>`")
    print(
        "  Delete these variables (you or your agent): the export lines in the shell files named, "
        "Windows user variables (`[Environment]::SetEnvironmentVariable('NAME', $null, 'User')`), "
        "the env entries in MCP configs, Environment lines in service definitions, and the "
        "entries in the legacy .env / .llm_config.json files named."
    )
    if from_default:
        print("  `m3 config import-env` copies their values into m3_config.toml instead, if you want m3 to use them.")
    if config_root_places:
        print(
            f"  [i] M3_CONFIG_ROOT is set to the default in {', '.join(sorted(config_root_places))}; "
            "remove it there (set it only for a non-default config root, on every surface)."
        )
    for u in sorted(set(unchecked)):
        print(f"  [i] could not check {u}; inspect it by hand.")
    return len(by_name)


def _interrupted_rewrite_section() -> None:
    """Name each file whose last rewrite began and never finished (a BEGIN with
    no END in its .backup_history), with the restore. Silent when none."""
    try:
        _bin.ensure_on_path()
        from m3_core.atomic_io import backup_paths, unfinished_rewrites

        found = unfinished_rewrites()
    except Exception as e:  # noqa: BLE001 -- doctor reports, never crashes
        print()
        print(f"  [!] could not check for interrupted rewrites: {e}. inspect: <config root>/.backup_index")
        return
    for path, by, began in found:
        bak, history = backup_paths(path)
        print()
        print(f"  [!] a rewrite of {path} by {by} began {began} and did not finish;")
        print(f"      the file may be incomplete. restore: copy {bak} over {path.name} (details: {history})")


def _deprecated_env_section() -> None:
    """Old (un-namespaced) env names still set anywhere m3 looks (the process, shell
    startup files, .env, MCP env blocks, service definitions, the Windows registry).
    m3 reads none of them; each is named with where it is set, to delete. Silent
    when none is set. The M3_* names are reported by m3_env_settings_section."""
    try:
        _bin.ensure_on_path()  # one rule; parents[2]/bin missed a wheel install
        from m3_core.paths import all_env_renames

        from m3_memory.install.env_sources import scan

        renames = all_env_renames()
        old = [n for n in renames if not n.startswith("M3_")]
        unchecked: list[str] = []
        found = scan(old, unchecked=unchecked)
    except Exception as e:  # noqa: BLE001 -- doctor reports, never crashes
        print()
        print(f"  [!] could not check for old env names: {e}")
        return
    by_name: dict[str, set[str]] = {}
    for f in found:
        if f.value.strip() and f.m3_surface:
            by_name.setdefault(f.name, set()).add(f.place)
    if not by_name and not unchecked:
        return  # clean -- say nothing rather than add noise
    print()
    for u in sorted(set(unchecked)):
        print(f"  [i] could not check {u} for old env names; inspect it by hand")
    if not by_name:
        return
    print("  [!] old env names m3 does not read (delete them where they are set):")
    for name in sorted(by_name):
        print(f"        {name}: set in {', '.join(sorted(by_name[name]))}")
