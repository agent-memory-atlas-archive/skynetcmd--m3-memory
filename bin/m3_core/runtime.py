import logging
import os
import sys
import time
from datetime import UTC, datetime
from os import PathLike
from typing import IO, Literal, Union, cast

from m3_core.config import settings as _m3cfg

try:
    from dotenv import load_dotenv
except ImportError:

    def load_dotenv(
        dotenv_path: Union[str, "PathLike[str]"] | None = None,
        stream: IO[str] | None = None,
        verbose: bool = False,
        override: bool = False,
        interpolate: bool = True,
        encoding: str | None = "utf-8",
    ) -> bool:
        return False


M3_CORE_RS_DISABLE = os.environ.get("M3_CORE_RS_DISABLE", "0") == "1"

try:
    if M3_CORE_RS_DISABLE:
        raise ImportError
    from m3_core_rs import format_log
except ImportError:

    def format_log(event: str, *args, **kwargs) -> str:
        parts = [event]
        for a in args:
            if a is None or a == "":
                continue
            parts.append(str(a))
        for k, v in kwargs.items():
            if v is None:
                continue
            parts.append(f"{k}={v}")
        return " | ".join(parts)


logger = logging.getLogger("M3_SDK")


# ── Timestamp helpers (public, format-explicit) ────────────────────────────
# Shared so callers don't each redefine a private `_now_iso`. Three modules had
# drifted into TWO different formats (local vs UTC) under that one private name;
# these public, format-named functions replace them. A rename here is a normal
# public-API change with real call sites — not a silent break of `_`-private
# cross-module reach (the reason we don't expose `_`-prefixed helpers).


def iso_local_timestamp() -> str:
    """Local wall-clock ISO-8601 to the second, no timezone suffix
    (e.g. '2026-07-20T14:31:05'). Human-readable stamp for non-ordering-critical
    metadata — coordination/registry files, the HALT semaphore. NOT for
    cross-host ordering; use iso_utc_timestamp for that."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def utc_iso(
    timespec: Literal["seconds", "milliseconds", "microseconds"] = "seconds", *, at: datetime | None = None
) -> str:
    """The current UTC time (or ``at``, any aware datetime) as ISO-8601 with a
    trailing 'Z', FIXED WIDTH.

    THE single owner of this format; do not hand-roll ``strftime`` copies.
    ``seconds`` (the default) is '2026-07-20T18:31:05Z', byte-identical to the
    ``strftime("%Y-%m-%dT%H:%M:%SZ")`` the stored columns already hold, so new
    rows order correctly against old ones. These stamps are compared as STRINGS,
    which is why the width must never vary: bare ``isoformat()`` drops the
    fraction when it is zero, and '...:05Z' then sorts after '...:05.000001Z'.
    Pick one precision per column and keep it.

    Never ``strftime("...%M:%fZ")``: in Python ``%f`` is microseconds, not
    SQLite's seconds-with-millis, and the seconds vanish (plan item R7).
    """
    when = datetime.now(UTC) if at is None else at.astimezone(UTC)
    return when.isoformat(timespec=timespec).removesuffix("+00:00") + "Z"


def local_and_utc(value: object) -> str:
    """A time for a person: local wall clock with its zone, then UTC in parens,
    e.g. ``2026-07-19 14:32:01 EDT (2026-07-19T18:32:01Z)``. THE owner of that
    display (doctor and the dashboard carried copies).

    ``value``: a datetime, an epoch number, or an ISO-8601 string; a naive value
    is UTC (SQLite stores wall-clock UTC). "—" for None or ""; an unparseable
    value comes back stringified, never raised, since a report line must not
    crash on an odd timestamp. Now: ``local_and_utc(datetime.now(UTC))``."""
    if value is None or value == "":
        return "—"
    dt: datetime
    try:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(float(value), UTC)
        else:
            dt = datetime.fromisoformat(str(value).strip())  # reads a trailing "Z" (3.11+)
    except (ValueError, OSError, OverflowError):
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone()
    # A compact zone: Windows' %Z is the long name ("Eastern Daylight Time"),
    # shortened to its initials; the numeric offset when %Z is empty.
    raw_tz = local.strftime("%Z")
    tzname = "".join(w[0] for w in raw_tz.split() if w).upper() if raw_tz and " " in raw_tz else raw_tz
    tzname = tzname or local.strftime("%z")
    return f"{local.strftime('%Y-%m-%d %H:%M:%S')} {tzname} ({dt.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')})"


def iso_utc_timestamp() -> str:
    """UTC ISO-8601 with microseconds and a trailing 'Z'
    (e.g. '2026-07-20T18:31:05.123456Z'), always six fraction digits. Use when
    the value is compared or ordered across hosts (audit / db-repair
    timestamps). Kept for its callers; it is ``utc_iso("microseconds")``."""
    return utc_iso("microseconds")


def ensure_utf8() -> None:
    """Guarantee the current process runs in Python UTF-8 mode.

    On Windows both stdio AND open() default to the legacy cp1252 code page, so
    any non-cp1252 character (em-dashes, arrows, box-drawing, emoji) crashes with
    UnicodeEncodeError on print or UnicodeDecodeError on a no-encoding open().
    True UTF-8 mode (PEP 540) fixes both, but the interpreter reads it only at
    startup — so we set PYTHONUTF8 and re-exec once with -X utf8.

    Shared canonical implementation: called from every m3 entry process that
    isn't guaranteed to inherit UTF-8 mode — the m3 CLI (m3_memory.cli) and the
    standalone MCP→OpenAI proxy (bin/mcp_proxy.py, the OpenClaw path, launched
    directly as `python bin/mcp_proxy.py` so it never flows through the CLI).

    Safety: no-op if already in UTF-8 mode; an env sentinel bounds the re-exec to
    exactly once so it cannot loop; sys.orig_argv reconstructs the launch
    faithfully (so -m / file-path forms survive).

    KNOWN LIMITATION: inline `python -c "<code>"` launches can mangle on re-exec
    because the OS re-quotes the program string; not a supported m3 entry path.
    Set PYTHONUTF8=1 in the env to bypass (then this short-circuits).
    """
    if sys.flags.utf8_mode:
        return
    if os.environ.get("_M3_UTF8_REEXEC") == "1":
        return
    os.environ["PYTHONUTF8"] = "1"
    os.environ["_M3_UTF8_REEXEC"] = "1"
    orig = list(getattr(sys, "orig_argv", [sys.executable, *sys.argv])) or [sys.executable, *sys.argv]
    try:
        os.execv(sys.executable, [orig[0], "-X", "utf8", *orig[1:]])
    except OSError:
        # Re-exec failed (exotic launcher / permissions). Caller's stdio
        # reconfigure (if any) still handles the common print path.
        pass


# The local LLM base URL + read timeout, for bridges that import them from here
# instead of redefining them. Registry keys llm.lmstudio_base (former env name, not read:
# LM_STUDIO_BASE) and llm.read_timeout_s (former env names, not read: M3_LM_READ_TIMEOUT,
# LM_READ_TIMEOUT). Bound once at import: they are public module constants
# (m3_sdk re-exports both); a reader that must follow an edited file calls
# m3_core.config.settings.setting() itself.
LM_STUDIO_BASE: str = str(_m3cfg.setting("llm.lmstudio_base"))
LM_READ_TIMEOUT: float = float(_m3cfg.setting("llm.read_timeout_s"))


class StructuredLogger:
    """Renders structured log lines as `event | k=v | k=v` for grep-friendly output."""

    def format(self, event: str, *args, **kwargs) -> str:
        return cast("str", format_log(event, *args, **kwargs))

    def log(self, event: str, *args, **kwargs) -> None:
        """Helper to format and print a structured log line to stderr."""
        print(self.format(event, *args, **kwargs), file=sys.stderr)
