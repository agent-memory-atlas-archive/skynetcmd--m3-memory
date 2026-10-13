"""gdpr_forget is the only hard delete, and it takes two steps (user decision
2026-10-13): a plan that erases nothing, then a confirmation carrying the plan's
token and a compliance record. Every refusal erases nothing.
"""

from __future__ import annotations

import uuid

import pytest

from conftest import gdpr_erase

_RECORD = {"legal_basis": "consent withdrawn", "verified_by": "test operator"}


def _p():
    from memory.backends import dialect

    return dialect().param()


def _insert(backend, mid, user):
    p = _p()
    with backend.connection() as conn:
        conn.execute(
            f"INSERT INTO memory_items (id, type, title, content, user_id, is_deleted) "
            f"VALUES ({p}, 'note', 'gdpr probe', 'gdpr probe body', {p}, 0)",
            (mid, user),
        )
        conn.commit()


def _alive(backend, ids):
    p = _p()
    with backend.connection() as conn:
        return {
            r["id"]
            for r in conn.execute(
                f"SELECT id FROM memory_items WHERE id IN ({', '.join([p] * len(ids))})", list(ids)
            ).fetchall()
        }


@pytest.fixture
def subject(each_backend):
    """A fresh data subject owning two memories; whatever survives is removed after."""
    user = f"subject-{uuid.uuid4().hex[:8]}"
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    for mid in ids:
        _insert(each_backend, mid, user)
    yield each_backend, user, ids
    p = _p()
    with each_backend.connection() as conn:
        conn.execute(f"DELETE FROM memory_items WHERE user_id = {p}", (user,))
        conn.execute(f"DELETE FROM gdpr_requests WHERE subject_id = {p}", (user,))
        conn.commit()


def _forget(**kw):
    from memory_maintenance import gdpr_forget_impl

    return gdpr_forget_impl(**kw)


def test_the_plan_erases_nothing_and_returns_a_token(subject):
    backend, user, ids = subject
    plan = _forget(user_id=user)
    assert plan["step"] == "plan" and plan["erased"] is False
    assert plan["counts"]["memory_items"] == 2 and plan["token"]
    assert _alive(backend, ids) == set(ids), "the plan step erased something"


def test_confirm_without_the_compliance_record_is_refused(subject):
    backend, user, ids = subject
    plan = _forget(user_id=user)
    out = _forget(user_id=user, confirm=plan["token"], compliance={"legal_basis": "x"})
    assert isinstance(out, str) and "verified_by" in out and "Nothing was erased" in out
    assert _alive(backend, ids) == set(ids)


def test_a_bad_token_is_refused(subject):
    backend, user, ids = subject
    _forget(user_id=user)
    for bad in ("not-a-token", f"{uuid.uuid4()}.9999999999.0000000000000000"):
        out = _forget(user_id=user, confirm=bad, compliance=_RECORD)
        assert isinstance(out, str) and "Nothing was erased" in out, out
    assert _alive(backend, ids) == set(ids)


def test_an_expired_token_is_refused(subject):
    backend, user, ids = subject
    plan = _forget(user_id=user)
    req, _exp, digest = plan["token"].split(".")
    out = _forget(user_id=user, confirm=f"{req}.1.{digest}", compliance=_RECORD)
    assert isinstance(out, str) and "expired" in out
    assert _alive(backend, ids) == set(ids)


def test_a_plan_that_changed_is_refused(subject):
    """A memory added after the plan was not shown to the operator: refuse."""
    backend, user, ids = subject
    plan = _forget(user_id=user)
    late = str(uuid.uuid4())
    _insert(backend, late, user)
    out = _forget(user_id=user, confirm=plan["token"], compliance=_RECORD)
    assert isinstance(out, str) and "changed since the plan" in out
    assert _alive(backend, [*ids, late]) == {*ids, late}


def test_confirm_erases_and_the_token_cannot_be_reused(subject):
    backend, user, ids = subject
    plan = _forget(user_id=user)
    done = _forget(user_id=user, confirm=plan["token"], compliance=_RECORD)
    assert done["erased"] is True and done["items_erased"] == 2
    assert _alive(backend, ids) == set()
    again = _forget(user_id=user, confirm=plan["token"], compliance=_RECORD)
    assert isinstance(again, str) and "no open erasure plan" in again


def test_memory_ids_erase_exactly_those(subject):
    backend, _user, ids = subject
    gdpr_erase(memory_ids=[ids[0]])
    assert _alive(backend, ids) == {ids[1]}, "a per-item erasure touched another memory"


def test_exactly_one_target():
    assert "exactly one" in _forget()
    assert "exactly one" in _forget(user_id="u", memory_ids=["x"])
