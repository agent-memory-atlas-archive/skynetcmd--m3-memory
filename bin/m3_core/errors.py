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

_CODE_DEFECTS: tuple[type[BaseException], ...] = (TypeError, AttributeError, NameError)


def is_defect(exc: BaseException) -> bool:
    if isinstance(exc, _CODE_DEFECTS):
        return True
    try:
        from memory.backends import active_backend

        return bool(active_backend().is_query_defect(exc))
    except Exception:  # noqa: BLE001 -- classifying inside an except block must not raise;
        return False   # without a resolvable backend there is no driver error to classify


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
    logger.warning("%s -- a defect, not an expected miss; observed: %s: %s. inspect: the "
                   "query or the code at this call site", text, type(exc).__name__, exc)
