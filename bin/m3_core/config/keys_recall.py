"""Registry table [recall]: the session-recall menu offered at session start.

The loop's summarize_sessions pass writes one ``session_summary`` memory per
quiet (or compacted / exited) chat session; ``session_brief`` turns the recent
ones into a numbered menu. Settings only, no environment names (m3 settings are
never read from the environment), and all are read at call time, so a changed
file applies to the next session and the next loop sweep with no restart.
"""

from __future__ import annotations

from .registry import ConfigValueError, Key, in_range, parse_bool, parse_int, parse_str


def _int(name: str, default: int, doc: str, lo: int) -> Key[int]:
    return Key[int](name=name, parse=parse_int, default=default, validate=in_range(lo=lo), doc=doc)


def _summary_profile(value: str) -> None:
    # "local", "session", or an SLM profile name (config/slm/<name>.yaml). The
    # name's existence is checked where it is used (the summary pass names the
    # missing file and the fix); here only an empty value is refused.
    if not value.strip():
        raise ConfigValueError("recall.summary_profile is empty; use local, session, or an SLM profile name")


RECALL: list[Key] = [
    Key[bool](
        name="recall.enabled",
        parse=parse_bool,
        default=True,
        doc="Offer the recent-sessions menu at session start (session_brief and the session-start hook).",
    ),
    _int(
        "recall.window_days",
        2,
        "How many days back a session may have ended and still be offered in the menu.",
        lo=1,
    ),
    _int(
        "recall.same_agent_sessions",
        3,
        "Menu lines for the current agent's own recent sessions (0: none).",
        lo=0,
    ),
    _int(
        "recall.other_agents",
        3,
        "Other agents offered in the menu, one line each with that agent's most recent session (0: none).",
        lo=0,
    ),
    _int(
        "recall.active_minutes",
        10,
        "A session whose last turn is newer than this is marked (active): its summary is partial.",
        lo=0,
    ),
    _int(
        "recall.quiet_minutes",
        20,
        "The loop summarizes a session once it has been quiet this long (a compaction or exit sooner).",
        lo=1,
    ),
    Key[str](
        name="recall.summary_profile",
        parse=parse_str,
        default="local",
        validate=_summary_profile,
        doc="Who writes the loop's session summaries: local (the session_summary SLM profile; the text "
        "stays on this machine), session (the session's own model when m3 holds that provider's API key, "
        "else local), or an SLM profile name. Anything but local sends the session's tail to that provider.",
    ),
]
