"""Who is calling m3 right now: the ONE owner of caller identity for auditing.

The tool dispatcher knows every call's authenticated agent id, but it injects it
only into tools that opt in, so a read path could not say who read a memory (the
read-access audit log, GDPR Art. 30). The dispatcher
binds the caller here around every tool call; readers ask ``current_caller()``.

A ContextVar, so concurrent calls do not see each other's identity, and
``asyncio.to_thread`` (which copies the context) carries it into sync tools.
Outside any dispatch the caller is ``"direct"`` (an import, a script, a test).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_CALLER: ContextVar[str] = ContextVar("m3_caller", default="direct")


def current_caller() -> str:
    """The agent id of the tool call in progress, or ``"direct"``."""
    return _CALLER.get()


@contextmanager
def as_caller(agent_id: str | None) -> Iterator[None]:
    """Bind the caller for the duration of one tool call. An empty id (the
    LLM-facing m3_call path, whose identity the anti-spoofing guard withholds)
    is recorded as ``"unattributed"``, never as a blank."""
    token = _CALLER.set(agent_id or "unattributed")
    try:
        yield
    finally:
        _CALLER.reset(token)
