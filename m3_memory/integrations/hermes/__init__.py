"""m3 as a Hermes Agent memory provider.

Hermes (>= 0.22) loads this module through the ``hermes_agent.memory_providers``
entry point (select it with ``memory.provider: m3``) and drives it through its
``agent.memory_provider.MemoryProvider`` interface:

- every turn: ``sync_turn`` hands the user and assistant text to m3's chat log
  (host agent ``hermes``); the cognitive loop extracts facts from it later;
- before a turn: ``prefetch`` returns what a background recall found for the
  previous message;
- three tools for the model: ``m3_profile``, ``m3_search``, ``m3_conclude``.

Calls run in-process through the shared integration client
(m3_memory.integrations._client): no server or proxy. Settings:
``integrations.hermes.user_id`` / ``agent_id`` in m3_config.toml, overridden by
``$HERMES_HOME/m3.json`` (written by ``hermes memory setup``).

Scheduled jobs and subagents (``agent_context`` other than "primary") get recall
only: Hermes asks providers not to write from those contexts.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for
from typing import Any, cast

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# The host_agent m3's chatlog records for turns Hermes hands over (a library
# host: no hook, no transcript; see chatlog_config.LIBRARY_HOST_AGENTS).
_HOST_AGENT = "hermes"
_WRITE_CONTEXTS = frozenset({"primary"})
_SEARCH_LIMIT = 50
_PREFETCH_WAIT_S = 3.0
_SHUTDOWN_WAIT_S = 5.0


def _load_config() -> dict:
    """The m3 settings, with any non-empty value in $HERMES_HOME/m3.json on top."""
    from hermes_constants import get_hermes_home

    from m3_memory import _config as _m3config

    config = {
        "user_id": _m3config.setting("integrations.hermes.user_id"),
        "agent_id": _m3config.setting("integrations.hermes.agent_id"),
    }
    path = get_hermes_home() / "m3.json"
    if not os.path.exists(path):
        return config
    try:
        overrides = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(
            "m3: %s is unreadable (%s: %s); using user_id=%r agent_id=%r. fix: repair or delete the file",
            path,
            type(e).__name__,
            e,
            config["user_id"],
            config["agent_id"],
        )
        return config
    config.update({k: v for k, v in overrides.items() if v not in (None, "")})
    return config


class _Breaker:
    """m3's circuit breaker (m3_core.context) for the service "hermes": after
    repeated failures Hermes stops calling m3 for the cooldown, so a broken
    install costs a few warnings, not one per turn."""

    SERVICE = "hermes"

    @staticmethod
    def _owner() -> Any:
        from m3_memory import _bin

        _bin.ensure_on_path()  # m3_core lives in the payload's bin/
        from m3_core import context

        return context

    def allows(self) -> bool:
        try:
            return bool(self._owner().circuit_allows(self.SERVICE))
        except Exception:  # noqa: BLE001 — no payload: the call itself reports why
            return True

    def succeeded(self) -> None:
        try:
            self._owner().circuit_succeeded(self.SERVICE)
        except Exception:  # noqa: BLE001
            pass

    def failed(self, what: str, err: BaseException) -> None:
        logger.warning("m3 %s failed: %s: %s. inspect: m3 doctor", what, type(err).__name__, err)
        try:
            self._owner().circuit_failed(self.SERVICE)
        except Exception:  # noqa: BLE001
            pass


_warned_unavailable: set[str] = set()


def _warn_unavailable_once(cause: str) -> None:
    """Hermes asks is_available() at every start and status check; say why m3
    is unusable once per cause, not each time."""
    if cause not in _warned_unavailable:
        _warned_unavailable.add(cause)
        logger.warning("m3 memory provider unavailable: %s", cause)


def _unavailable_cause() -> str | None:
    """Why m3 cannot run in Hermes' Python, with the fix; None when it can.
    Cheap: it locates m3's payload and never imports the catalog."""
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
            f"was not found (looked at: {found}). fix: {install} --force-reinstall, or set [paths] bin in m3_config.toml"
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


def _schema(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": props, "required": required},
    }


class M3MemoryProvider(MemoryProvider):
    """m3 memory for Hermes: chat capture, hybrid recall and verbatim facts."""

    def __init__(self) -> None:
        self._client: Any = None
        self._client_lock = threading.Lock()
        self._user_id = "hermes-user"
        self._agent_id = "hermes"
        self._session_id = ""
        self._writes = True
        self._gate = _Breaker()
        # One writer keeps turns in order; recall runs on its own worker so a
        # slow search never delays a capture.
        self._writer: ThreadPoolExecutor | None = None
        self._reader: ThreadPoolExecutor | None = None
        self._pending: list[Future] = []
        self._recall: Future | None = None
        self._tools: dict[str, tuple[dict[str, Any], Callable[[dict], str]]] = {
            "m3_profile": (
                _schema(
                    "m3_profile",
                    "List what m3 knows about this user: preferences, decisions and project "
                    "facts stored earlier. Call it when a conversation starts.",
                    {},
                    [],
                ),
                self._tool_profile,
            ),
            "m3_search": (
                _schema(
                    "m3_search",
                    "Find memories relevant to a question, by meaning and by keyword, best matches first.",
                    {
                        "query": {"type": "string", "description": "The question or topic to look up."},
                        "top_k": {
                            "type": "integer",
                            "description": f"How many results (1-{_SEARCH_LIMIT}, default 10).",
                        },
                    },
                    ["query"],
                ),
                self._tool_search,
            ),
            "m3_conclude": (
                _schema(
                    "m3_conclude",
                    "Save one fact about the user exactly as written, such as a stated "
                    "preference, a correction or a decision.",
                    {"conclusion": {"type": "string", "description": "The fact, in one sentence."}},
                    ["conclusion"],
                ),
                self._tool_conclude,
            ),
        }

    # ── identity and availability ───────────────────────────────────────────
    @property
    def name(self) -> str:
        return "m3"

    def is_available(self) -> bool:
        cause = _unavailable_cause()
        if cause:
            _warn_unavailable_once(cause)
        return cause is None

    def unavailable_reason(self) -> str:
        """Why Hermes cannot use m3 here, with the fix ("" when available)."""
        return _unavailable_cause() or ""

    def get_status_config(self, provider_config):
        """What `hermes memory status` prints under "m3 config": Hermes shows only
        "not available", so the reason and its fix are added here."""
        shown = dict(provider_config) if isinstance(provider_config, dict) else {}
        cause = _unavailable_cause()
        if cause:
            shown["unavailable"] = cause
        return shown

    # ── configuration (hermes memory setup) ─────────────────────────────────
    def get_config_schema(self):
        return [
            {
                "key": "user_id",
                "description": "Whose memories this Hermes profile reads and writes",
                "default": "hermes-user",
            },
            {
                "key": "agent_id",
                "description": "The agent name m3 records on memories Hermes writes",
                "default": "hermes",
            },
        ]

    def save_config(self, values, hermes_home):
        from pathlib import Path

        path = Path(hermes_home) / "m3.json"
        merged: dict = {}
        if os.path.exists(path):
            # A file that does not parse is refused, not replaced: merging into
            # {} would drop every setting it held.
            try:
                merged = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise RuntimeError(
                    f"m3: {path} is unreadable ({type(e).__name__}: {e}); not overwriting it. "
                    "fix: repair or delete the file, then rerun `hermes memory setup`"
                ) from e
        merged.update(values)
        try:
            from utils import atomic_json_write  # Hermes' own writer
        except ImportError:
            path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        else:
            atomic_json_write(path, merged, mode=0o600)

    # ── session lifecycle ───────────────────────────────────────────────────
    def initialize(self, session_id: str, **kwargs) -> None:
        config = _load_config()
        # A gateway names the platform user; that wins over the configured one.
        self._user_id = kwargs.get("user_id") or config.get("user_id", "hermes-user")
        self._agent_id = config.get("agent_id", "hermes")
        self._session_id = session_id
        context = kwargs.get("agent_context") or "primary"
        self._writes = context in _WRITE_CONTEXTS
        if not self._writes:
            logger.info("m3: agent_context=%r: recall only, no memory or turn writes (Hermes contract)", context)

    def system_prompt_block(self) -> str:
        return (
            "# m3 Memory\n"
            f"Long-term memory is on for user {self._user_id}. Look things up with m3_search, "
            "save a fact the user states with m3_conclude, and list stored facts with m3_profile."
        )

    def shutdown(self) -> None:
        self.flush(_SHUTDOWN_WAIT_S)
        for pool in (self._writer, self._reader):
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
        self._writer = self._reader = None
        with self._client_lock:
            self._client = None

    def flush(self, timeout: float = _SHUTDOWN_WAIT_S) -> None:
        """Wait (up to ``timeout``) for queued writes and recall to finish."""
        waiting = [f for f in [*self._pending, self._recall] if f is not None]
        if waiting:
            wait_for(waiting, timeout=timeout)
        self._pending = [f for f in self._pending if not f.done()]

    # ── the m3 client and its three calls ───────────────────────────────────
    def _get_client(self):
        with self._client_lock:
            if self._client is None:
                cause = _unavailable_cause()
                if cause:
                    raise RuntimeError(cause)
                from m3_memory.integrations._client import M3Client

                self._client = M3Client(agent_id=self._agent_id)
            return self._client

    def _search(self, query: str, top_k: int) -> list[dict]:
        """Hybrid recall in this user's scope: [{"content", "score"}]."""
        rows = self._get_client()._tool(
            "memory_search_scored", query=query, user_id=self._user_id, k=top_k, scope="user"
        )
        return [{"content": item.get("content", ""), "score": score} for score, item in rows or []]

    def _profile(self) -> list[str]:
        """This user's stored facts: the filter-only listing (query="")."""
        rows = self._get_client()._tool(
            "memory_search_scored", query="", user_id=self._user_id, type_filter="user_fact", k=200, scope="user"
        )
        return [item.get("content", "") for _score, item in rows or [] if item.get("content")]

    def _conclude(self, content: str) -> None:
        # The scope and type _search and _profile read: a fact stored anywhere
        # else would be "stored" and never found.
        self._get_client()._tool("memory_write", content=content, user_id=self._user_id, type="user_fact", scope="user")

    # ── per turn: capture and recall ────────────────────────────────────────
    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """Queue the turn for m3's chat log; it returns at once."""
        if not self._writes or not self._gate.allows():
            return
        conversation_id = session_id or self._session_id
        turns = [("user", user_content), ("assistant", assistant_content)]

        def write() -> None:
            try:
                self._get_client().write_turns(
                    turns, conversation_id=conversation_id, host_agent=_HOST_AGENT, user_id=self._user_id
                )
            except Exception as e:  # noqa: BLE001 — a capture failure must not break the turn; said with its cause
                self._gate.failed(f"capture of a turn of conversation {conversation_id}", e)
            else:
                self._gate.succeeded()

        if self._writer is None:
            self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="m3-capture")
        self._pending = [f for f in self._pending if not f.done()]
        self._pending.append(self._writer.submit(write))

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Start a recall for ``query``; the next prefetch() returns it."""
        if not self._gate.allows():
            return

        def recall() -> str:
            try:
                lines = [r["content"] for r in self._search(query, 5) if r.get("content")]
            except Exception as e:  # noqa: BLE001 — recall must never break a turn; said, and counted
                self._gate.failed("recall (this turn has no m3 context)", e)
                return ""
            self._gate.succeeded()
            return "\n".join(f"- {line}" for line in lines)

        if self._reader is None:
            self._reader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="m3-recall")
        self._recall = self._reader.submit(recall)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        recall, self._recall = self._recall, None
        if recall is None:
            return ""
        try:
            found = recall.result(timeout=_PREFETCH_WAIT_S)
        except Exception:  # noqa: BLE001 — a slow or failed recall leaves this turn without m3 context
            return ""
        return f"## m3 Memory\n{found}" if found else ""

    # ── tools ───────────────────────────────────────────────────────────────
    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [schema for schema, _handler in self._tools.values()]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        entry = self._tools.get(tool_name)
        if entry is None:
            return cast("str", tool_error(f"Unknown tool: {tool_name}"))
        if not self._gate.allows():
            return json.dumps({"error": "m3 is paused after repeated failures and retries shortly. inspect: m3 doctor"})
        try:
            self._get_client()
        except Exception as e:  # noqa: BLE001 — returned to the model as the tool's error
            return cast("str", tool_error(str(e)))
        return entry[1](args)

    def _call(self, what: str, fn: Callable[[], str]) -> str:
        try:
            out = fn()
        except Exception as e:  # noqa: BLE001 — returned to the model as the tool's error
            self._gate.failed(what, e)
            return cast("str", tool_error(f"{what} failed: {e}"))
        self._gate.succeeded()
        return out

    def _tool_profile(self, args: dict) -> str:
        def run() -> str:
            facts = self._profile()
            if not facts:
                return json.dumps({"result": "No memories stored yet."})
            return json.dumps({"result": "\n".join(facts), "count": len(facts)})

        return self._call("profile", run)

    def _tool_search(self, args: dict) -> str:
        query = args.get("query", "")
        if not query:
            return cast("str", tool_error("Missing required parameter: query"))
        top_k = max(1, min(int(args.get("top_k", 10)), _SEARCH_LIMIT))

        def run() -> str:
            hits = self._search(query, top_k)
            if not hits:
                return json.dumps({"result": "No relevant memories found."})
            items = [{"memory": h.get("content", ""), "score": h.get("score", 0)} for h in hits]
            return json.dumps({"results": items, "count": len(items)})

        return self._call("search", run)

    def _tool_conclude(self, args: dict) -> str:
        fact = args.get("conclusion", "")
        if not fact:
            return cast("str", tool_error("Missing required parameter: conclusion"))
        if not self._writes:
            return json.dumps({"result": "Not stored: this Hermes context (cron or subagent) is recall-only."})

        def run() -> str:
            self._conclude(fact)
            return json.dumps({"result": "Fact stored."})

        return self._call("store", run)


def register(ctx) -> None:
    """Hermes' plugin entry point: register m3 as a memory provider."""
    ctx.register_memory_provider(M3MemoryProvider())
