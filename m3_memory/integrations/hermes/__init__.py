"""m3-memory plugin — MemoryProvider interface.

Hybrid FTS5 + vector recall with MMR diversity rerank, a bitemporal model, KG
supersession, and async Observer/Reflector fact extraction — exposed to Hermes
Agent through the same MemoryProvider ABC the mem0 plugin uses.

Patterned on plugins/memory/mem0/__init__.py (threaded prefetch, circuit
breaker, three tools, register(ctx) entrypoint).

Config: the m3 settings integrations.hermes.user_id / agent_id (default
hermes-user / hermes), overridden by $HERMES_HOME/m3.json.

Install: m3-memory installed into Hermes' own Python environment (>= 3.12)
registers this provider through the ``hermes_agent.memory_providers`` entry
point; select it with ``memory.provider: m3``. Calls run in-process through the
shared integration client (m3_memory.integrations._client); no server or proxy.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from typing import Any, cast

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# The host_agent m3's chatlog records for turns Hermes hands over (a library
# host: no hook, no transcript; see chatlog_config.LIBRARY_HOST_AGENTS).
_HOST_AGENT = "hermes"
# Hermes passes agent_context="cron" / "subagent" for scheduled jobs and
# delegated subagents, and its contract is that providers skip writes there.
_WRITE_CONTEXTS = frozenset({"primary"})

# Circuit breaker — same policy as the mem0 provider.
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECS = 120


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _load_config() -> dict:
    """Env vars with $HERMES_HOME/m3.json overrides (mem0 pattern)."""
    from hermes_constants import get_hermes_home

    from m3_memory import _config as _m3config

    config = {
        "user_id": _m3config.setting("integrations.hermes.user_id"),
        "agent_id": _m3config.setting("integrations.hermes.agent_id"),
    }

    config_path = get_hermes_home() / "m3.json"
    if os.path.exists(config_path):
        try:
            file_cfg = json.loads(config_path.read_text(encoding="utf-8"))
            config.update({k: v for k, v in file_cfg.items() if v is not None and v != ""})
        except (OSError, ValueError) as e:
            logger.warning(
                "m3: %s is unreadable (%s: %s); using user_id=%r agent_id=%r. fix: repair or delete the file",
                config_path,
                type(e).__name__,
                e,
                config["user_id"],
                config["agent_id"],
            )

    return config


# ---------------------------------------------------------------------------
# Tool schemas — flat shape, matching the mem0 provider exactly.
# ---------------------------------------------------------------------------

PROFILE_SCHEMA = {
    "name": "m3_profile",
    "description": (
        "Retrieve stored stable facts about the user — preferences, decisions, "
        "project context. Use at conversation start."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

SEARCH_SCHEMA = {
    "name": "m3_search",
    "description": (
        "Hybrid semantic + keyword search over long-term memory (FTS5 + vector "
        "+ MMR diversity rerank). Returns facts ranked by relevance."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "top_k": {"type": "integer", "description": "Max results (default: 10, max: 50)."},
        },
        "required": ["query"],
    },
}

CONCLUDE_SCHEMA = {
    "name": "m3_conclude",
    "description": (
        "Store a durable fact about the user, verbatim (no LLM re-extraction). "
        "Use for explicit preferences, corrections, or decisions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conclusion": {"type": "string", "description": "The fact to store."},
        },
        "required": ["conclusion"],
    },
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------


_warned_unavailable: set[str] = set()


def _warn_unavailable_once(cause: str) -> None:
    """Log why m3 is unusable, once per distinct cause per process: Hermes calls
    is_available() at every startup and on each status check, and would otherwise
    say only "not available"."""
    if cause not in _warned_unavailable:
        _warned_unavailable.add(cause)
        logger.warning("m3 memory provider unavailable: %s", cause)


class M3MemoryProvider(MemoryProvider):
    """m3-memory: async extraction (Observer/Reflector), hybrid recall, KG
    supersession, exposed through the MemoryProvider ABC."""

    def __init__(self):
        self._config = None
        self._client = None
        self._client_lock = threading.Lock()
        self._user_id = "hermes-user"
        self._agent_id = "hermes"
        self._session_id = ""
        self._writes = True
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread = None
        self._sync_thread = None
        # Circuit breaker state
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

    @property
    def name(self) -> str:
        return "m3"

    def is_available(self) -> bool:
        # Hermes activates a provider only when this is True, and runs it for
        # every provider at startup, so it must stay cheap: it locates m3's
        # payload (m3_memory._bin) and never imports the catalog (that pulls
        # memory_core and the embedder load path). A pip install keeps m3's bin/
        # off sys.path until the client puts it there, so probing for
        # `mcp_tool_catalog` on the path reported a working install unavailable.
        cause = self._unavailable_cause()
        if cause:
            _warn_unavailable_once(cause)
        return cause is None

    def unavailable_reason(self) -> str:
        """Why Hermes cannot use m3 here, with the fix ("" when available)."""
        return self._unavailable_cause() or ""

    def get_status_config(self, provider_config):
        """What `hermes memory status` prints under "m3 config". Hermes 0.22 shows
        only "not available" for a provider and never asks why, so the reason
        and its fix ride along here."""
        shown = dict(provider_config) if isinstance(provider_config, dict) else {}
        cause = self._unavailable_cause()
        if cause:
            shown["unavailable"] = cause
        return shown

    @staticmethod
    def _unavailable_cause() -> str | None:
        install = f'"{sys.executable}" -m pip install m3-memory'
        try:
            from m3_memory import _bin
        except Exception as e:  # noqa: BLE001 — any failure here means "not usable", said with its cause
            return (
                f"m3-memory is not importable in Hermes' Python ({sys.executable}, Python "
                f"{sys.version.split()[0]}): {type(e).__name__}: {e}. m3 needs Python 3.12 or newer. "
                f"fix: {install}"
            )
        found = _bin.bin_dir()
        if found is None or not os.path.isfile(os.path.join(str(found), "mcp_tool_catalog.py")):
            return (
                f"m3-memory is installed in {sys.executable} but its payload (bin/mcp_tool_catalog.py) "
                f"was not found (looked at: {found}). fix: {install} --force-reinstall, or set M3_PATH_BIN"
            )
        from m3_memory.integrations._client import shadowing_memory_package

        shadow = shadowing_memory_package()
        if shadow:
            return (
                f"another top-level 'memory' package ({shadow}) shadows m3's, so m3 cannot import "
                "(Hermes Agent before 0.22 put its plugins/ folder on the path). fix: upgrade Hermes "
                "Agent to 0.22 or newer"
            )
        return None

    def save_config(self, values, hermes_home):
        import json as _json
        from pathlib import Path

        config_path = Path(hermes_home) / "m3.json"
        existing = {}
        if os.path.exists(config_path):
            # A file that does not parse is refused, not replaced: merging into
            # {} would silently drop every setting it held.
            try:
                existing = _json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise RuntimeError(
                    f"m3: {config_path} is unreadable ({type(e).__name__}: {e}); not overwriting it. "
                    "fix: repair or delete the file, then rerun `hermes memory setup`"
                ) from e
        existing.update(values)
        try:
            from utils import atomic_json_write  # Hermes' own writer
        except ImportError:
            config_path.write_text(_json.dumps(existing, indent=2), encoding="utf-8")
        else:
            atomic_json_write(config_path, existing, mode=0o600)

    def get_config_schema(self):
        return [
            {"key": "user_id", "description": "User identifier", "default": "hermes-user"},
            {"key": "agent_id", "description": "Agent identifier", "default": "hermes"},
        ]

    def _get_client(self):
        """Thread-safe lazy M3Client."""
        with self._client_lock:
            if self._client is not None:
                return self._client
            cause = self._unavailable_cause()
            if cause:
                raise RuntimeError(cause)
            from m3_memory.integrations._client import M3Client

            self._client = M3Client(agent_id=self._agent_id)
            return self._client

    # ── circuit breaker (verbatim from mem0) ────────────────────────────
    def _is_breaker_open(self) -> bool:
        if self._consecutive_failures < _BREAKER_THRESHOLD:
            return False
        if time.monotonic() >= self._breaker_open_until:
            self._consecutive_failures = 0
            return False
        return True

    def _record_success(self):
        self._consecutive_failures = 0

    def _record_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= _BREAKER_THRESHOLD:
            self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECS
            logger.warning(
                "m3 circuit breaker tripped after %d consecutive failures. Pausing calls for %ds.",
                self._consecutive_failures,
                _BREAKER_COOLDOWN_SECS,
            )

    # ── m3 operations (structured rows, through the shared client) ─────────────
    def _search(self, query: str, top_k: int) -> list[dict]:
        """Hybrid recall in this user's scope → [{"content", "score"}]."""
        rows = self._get_client()._tool(
            "memory_search_scored", query=query, user_id=self._user_id, k=top_k, scope="user"
        )
        return [{"content": it.get("content", ""), "score": s} for s, it in rows or []]

    def _profile(self) -> list[str]:
        """This user's stable facts: the documented filter-only listing (query="")."""
        rows = self._get_client()._tool(
            "memory_search_scored", query="", user_id=self._user_id, type_filter="user_fact", k=200, scope="user"
        )
        return [it.get("content", "") for _s, it in rows or [] if it.get("content")]

    def _conclude(self, content: str) -> None:
        # Scope + type match what _search/_profile read, or the fact is stored
        # where recall never looks ("Fact stored", then nothing found).
        self._get_client()._tool("memory_write", content=content, user_id=self._user_id, type="user_fact", scope="user")

    def initialize(self, session_id: str, **kwargs) -> None:
        self._config = _load_config()
        # gateway-provided user_id wins for per-user scoping (mem0 behavior)
        self._user_id = kwargs.get("user_id") or self._config.get("user_id", "hermes-user")
        self._agent_id = self._config.get("agent_id", "hermes")
        self._session_id = session_id
        context = kwargs.get("agent_context") or "primary"
        self._writes = context in _WRITE_CONTEXTS
        if not self._writes:
            logger.info("m3: agent_context=%r: recall only, no memory or turn writes (Hermes contract)", context)

    def system_prompt_block(self) -> str:
        return (
            "# m3 Memory\n"
            f"Active. User: {self._user_id}.\n"
            "Use m3_search to find memories, m3_conclude to store a verbatim "
            "fact, m3_profile for a full overview."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=3.0)
        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""
        if not result:
            return ""
        return f"## m3 Memory\n{result}"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if self._is_breaker_open():
            return

        def _run():
            try:
                results = self._search(query, 5)
                if results:
                    lines = [r.get("content", "") for r in results if r.get("content")]
                    with self._prefetch_lock:
                        self._prefetch_result = "\n".join(f"- {ln}" for ln in lines)
                self._record_success()
            except Exception as e:  # noqa: BLE001 — recall must never break a turn; said, and counted by the breaker
                self._record_failure()
                logger.warning(
                    "m3 prefetch failed (%d consecutive): %s: %s; this turn has no m3 context",
                    self._consecutive_failures,
                    type(e).__name__,
                    e,
                )

        self._prefetch_thread = threading.Thread(target=_run, daemon=True, name="m3-prefetch")
        self._prefetch_thread.start()

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """Enqueue the turn into m3's chatlog; Observer/Reflector extract +
        supersede async (cheaper per-turn than mem0's synchronous add())."""
        if not self._writes or self._is_breaker_open():
            return
        conversation_id = session_id or self._session_id

        def _sync():
            try:
                self._get_client().write_turns(
                    [("user", user_content), ("assistant", assistant_content)],
                    conversation_id=conversation_id,
                    host_agent=_HOST_AGENT,
                    user_id=self._user_id,
                )
                self._record_success()
            except Exception as e:  # noqa: BLE001 — a capture failure must not break the turn; said with its cause
                self._record_failure()
                logger.warning(
                    "m3 did not capture a turn of conversation %s (%d consecutive failures): %s: %s",
                    conversation_id,
                    self._consecutive_failures,
                    type(e).__name__,
                    e,
                )

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

        self._sync_thread = threading.Thread(target=_sync, daemon=True, name="m3-sync")
        self._sync_thread.start()

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [PROFILE_SCHEMA, SEARCH_SCHEMA, CONCLUDE_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if self._is_breaker_open():
            return json.dumps(
                {"error": "m3 temporarily unavailable (multiple consecutive failures). Will retry automatically."}
            )

        try:
            self._get_client()
        except Exception as e:  # noqa: BLE001 — returned to the model as the tool's error
            return cast("str", tool_error(str(e)))

        if tool_name == "m3_profile":
            try:
                lines = self._profile()
                self._record_success()
                if not lines:
                    return json.dumps({"result": "No memories stored yet."})
                return json.dumps({"result": "\n".join(lines), "count": len(lines)})
            except Exception as e:
                self._record_failure()
                return cast("str", tool_error(f"Failed to fetch profile: {e}"))

        elif tool_name == "m3_search":
            query = args.get("query", "")
            if not query:
                return cast("str", tool_error("Missing required parameter: query"))
            top_k = min(int(args.get("top_k", 10)), 50)
            try:
                results = self._search(query, top_k)
                self._record_success()
                if not results:
                    return json.dumps({"result": "No relevant memories found."})
                items = [{"memory": r.get("content", ""), "score": r.get("score", 0)} for r in results]
                return json.dumps({"results": items, "count": len(items)})
            except Exception as e:
                self._record_failure()
                return cast("str", tool_error(f"Search failed: {e}"))

        elif tool_name == "m3_conclude":
            conclusion = args.get("conclusion", "")
            if not conclusion:
                return cast("str", tool_error("Missing required parameter: conclusion"))
            if not self._writes:
                return json.dumps({"result": "Not stored: this Hermes context (cron or subagent) is recall-only."})
            try:
                self._conclude(conclusion)
                self._record_success()
                return json.dumps({"result": "Fact stored."})
            except Exception as e:
                self._record_failure()
                return cast("str", tool_error(f"Failed to store: {e}"))

        return cast("str", tool_error(f"Unknown tool: {tool_name}"))

    def shutdown(self) -> None:
        for t in (self._prefetch_thread, self._sync_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        with self._client_lock:
            self._client = None


def register(ctx) -> None:
    """Register m3 as a memory provider plugin."""
    ctx.register_memory_provider(M3MemoryProvider())
