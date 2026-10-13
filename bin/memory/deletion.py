"""Deletion policy: m3 soft-deletes; GDPR erasure is the only hard delete.

User rule 2026-10-13: "GDPR is the only allowance for hard deletes." Every delete
path (memories, tasks, curation plans, the dashboard, LangGraph checkpoints)
marks rows deleted and keeps them recoverable. A caller that asks for a hard
delete gets a soft delete and this notice, permanently: the argument is never
removed and never destroys again. The one path that erases is `gdpr_forget`,
which takes two steps (a plan, then a confirmation with a compliance record).

tests/test_hard_delete_is_gdpr_only.py pins the allow-list of the SQL that
physically removes memory content.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

HARD_DELETE_NOTICE = (
    "hard delete is GDPR-only: this was a soft delete (kept and recoverable). "
    "To erase, use gdpr_forget: call it to get a plan and a token, then call it "
    "again with confirm=<token> and a compliance record (legal_basis, verified_by)."
)

_warned: set[str] = set()


def hard_delete_requested(caller: str) -> str:
    """Record that ``caller`` asked for a hard delete it will not get; return the
    notice for its result. Warns once per caller per process, so a loop over ids
    does not flood the log while the first request is still visible."""
    if caller not in _warned:
        _warned.add(caller)
        logger.warning(f"{caller}: hard=true requested; {HARD_DELETE_NOTICE}")
    return HARD_DELETE_NOTICE
