"""Log a swallowed exception at the level it deserves: the one owner.

Many m3 paths are best-effort (enrichment, emitters, caches) and swallow their
failures so the main operation proceeds. A swallowed EXPECTED miss belongs at
DEBUG; a swallowed DEFECT does not. On 2026-10-10 a '?' bind on PostgreSQL made
the embed cache and the conversation emitters fail on every call, swallowed at
DEBUG, so nothing showed. ``log_swallowed`` keeps the quiet path exactly as it
was and makes a defect a WARNING, so the expected case gains no noise.

A defect is a Python programming error (TypeError, AttributeError, NameError) or
a driver error the active backend calls a wrong query
(``StorageBackend.is_query_defect``: the database seam owns the driver classes).

    except Exception as e:
        log_swallowed(logger, e, "window chunk query failed: %s", e)
"""

from __future__ import annotations

import logging
import sys

_CODE_DEFECTS: tuple[type[BaseException], ...] = (TypeError, AttributeError, NameError)


def is_defect(exc: BaseException) -> bool:
    if isinstance(exc, _CODE_DEFECTS):
        return True
    # No imports here: this runs inside except handlers on any thread, and an
    # import raced the main thread's re-import of memory.backends (a module-lock
    # deadlock, 2026-10-10). A driver error can only come from a backend that
    # already exists, so the instantiated ones are enough.
    selector = sys.modules.get("memory.backends.selector")
    if selector is None:
        return False
    try:
        return any(b.is_query_defect(exc) for b in selector.loaded_backends())
    except Exception:  # noqa: BLE001 -- classifying inside an except block must not raise
        return False


_http_fault_warned: set[tuple[str, str]] = set()


def _http_fault_shape(exc: BaseException) -> tuple[str, str] | None:
    """(url, observed) for an HTTP-client fault (httpx status/transport error), else None.

    Duck-typed on the httpx shape so this module imports nothing (see is_defect).
    """
    if not type(exc).__module__.startswith("httpx"):
        return None
    response = getattr(exc, "response", None)
    try:
        request = getattr(exc, "request", None)
    except RuntimeError:  # httpx raises when a transport error has no request bound
        request = None
    url = str(getattr(request, "url", "") or "")
    if response is not None and hasattr(response, "status_code"):
        return url, f"HTTP {response.status_code}"
    return url, type(exc).__name__


def describe_http_failure(exc: BaseException, method: str = "", body_chars: int = 0) -> str:
    """One line for an HTTP-client failure: ``[METHOD ]scheme://host/path -> HTTP 401: <body>``
    or ``-> ConnectError: <msg>``. The URL never carries its query (``?key=``),
    and an HTTPStatusError's own text (which embeds the full URL) is not used.
    A non-HTTP exception is ``Type: message``."""
    shape = _http_fault_shape(exc)
    if shape is None:
        return f"{type(exc).__name__}: {str(exc) or '(no message)'}"
    raw_url, observed = shape
    from m3_core.secret_store import loggable_url, redacted_snippet  # lazy: never on this module's import path

    where = f"{method + ' ' if method else ''}{loggable_url(raw_url) if raw_url else '(url unknown)'}"
    response = getattr(exc, "response", None)
    if response is not None and hasattr(response, "status_code"):
        body = ""
        if body_chars:
            try:
                # A server can echo the request's Authorization back: redacted.
                body = redacted_snippet(response.text, body_chars) or "(empty body)"
            except Exception:  # noqa: BLE001 -- an undecodable body is still reported by status
                body = "(undecodable body)"
        return f"{where} -> {observed}" + (
            f": {body}" if body else f" {getattr(response, 'reason_phrase', '')}".rstrip()
        )
    return f"{where} -> {observed}: {redacted_snippet(exc, 300) or '(no message)'}"


def log_http_fault(logger: logging.Logger, exc: BaseException, what: str, fallback: str) -> None:
    """Report a swallowed HTTP failure of a best-effort LLM call.

    log_swallowed files an HTTPStatusError / connect error under "expected miss"
    (DEBUG), so an endpoint answering 401 or 500 on every call was invisible.
    Here such a fault is a WARNING once per (URL, status) -- with the URL as
    scheme://host/path only and the fallback taken -- then DEBUG for repeats.
    Anything else goes to log_swallowed unchanged.
    """
    shape = _http_fault_shape(exc)
    if shape is None:
        log_swallowed(logger, exc, "%s failed: %s: %s; %s", what, type(exc).__name__, exc, fallback)
        return
    raw_url, observed = shape
    from m3_core.secret_store import loggable_url  # lazy: never on the import path of this module

    url = loggable_url(raw_url) if raw_url else "(url unknown)"
    # str(HTTPStatusError) embeds the FULL url (query included); use the reason
    # phrase instead. Transport errors' text carries no URL.
    response = getattr(exc, "response", None)
    detail = (getattr(response, "reason_phrase", "") if response is not None else str(exc)) or "(no message)"
    if (url, observed) in _http_fault_warned:
        logger.debug("%s failed again: %s from %s; %s", what, observed, url, fallback)
        return
    _http_fault_warned.add((url, observed))
    logger.warning(
        "%s failed: %s from %s (%s); %s. Repeats of this URL+status are logged at DEBUG. "
        "inspect: the LLM endpoint and its key (m3 doctor)",
        what,
        observed,
        url,
        detail,
        fallback,
    )


def log_swallowed(logger: logging.Logger, exc: BaseException, msg: str, *args: object) -> None:
    """Report an exception the caller deliberately swallows: ``msg``/``args`` at
    DEBUG for an expected miss (unchanged from a plain ``logger.debug``), and
    the same text as a WARNING, with what was observed, for a defect."""
    if not is_defect(exc):
        logger.debug(msg, *args)
        return
    try:
        text = msg % args if args else msg
    except (TypeError, ValueError):
        text = f"{msg} {args!r}"
    logger.warning(
        "%s -- a defect, not an expected miss; observed: %s: %s. inspect: the query or the code at this call site",
        text,
        type(exc).__name__,
        exc,
    )
