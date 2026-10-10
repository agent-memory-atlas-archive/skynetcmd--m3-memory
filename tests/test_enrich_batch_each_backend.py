"""m3_enrich_batch's run-state bookkeeping works on EVERY backend.

It refused on PostgreSQL (require_sqlite_backend) and opened its state store
with a raw sqlite3.connect. It now shares enrichment_state.open_state_conn with
m3_enrich, and every statement takes its placeholder from the connection held.
The provider (Anthropic batch API) is not exercised here; the run row, its
resume notes, the stale-run reaper and the claim release are.
"""
from __future__ import annotations

import json
import uuid

import pytest


@pytest.fixture
def state(each_backend):
    """The state handle m3_enrich_batch holds for a run; runs it made are removed."""
    import enrichment_state as estate
    from m3_sdk import resolve_db_path

    with each_backend.connection():
        pass  # the sandbox materializes the migrated store on first touch through the seam
    conn = estate.open_state_conn(resolve_db_path(None) if each_backend.stores_databases_as_files else None)
    made: list[str] = []
    try:
        yield conn, made
    finally:
        from memory.backends.dialect import dialect_for_connection

        p = dialect_for_connection(conn).param()
        for rid in made:
            conn.execute(f"DELETE FROM enrichment_runs WHERE id = {p}", (rid,))
        conn.commit()
        conn.close()


class _Profile:
    name = "probe-profile"
    model = "probe-model"


def test_run_row_and_resume_notes(each_backend, state):
    import m3_enrich_batch as eb

    conn, made = state
    where = f"on {each_backend.name}. inspect: bin/m3_enrich_batch.py"
    run_id = eb._record_run_started(
        conn, profile=_Profile(), db_path=None, source_variant=f"src-{uuid.uuid4().hex[:6]}",
        target_variant="tgt", batch_id="b0", n_groups=3, slice_size=2, argv=["probe"])
    made.append(run_id)

    eb._record_batch_submitted(conn, run_id, slice_idx=1, batch_id="b1")
    eb._record_batch_submitted(conn, run_id, slice_idx=1, batch_id="b1")  # idempotent
    eb._record_batch_ingested(conn, run_id, batch_id="b0")
    notes = eb._read_run_notes(conn, run_id)
    assert notes["batches"] == [{"slice_idx": 0, "batch_id": "b0", "ingested": True},
                                {"slice_idx": 1, "batch_id": "b1", "ingested": False}], where

    eb._record_run_finished(conn, run_id, status="completed", n_success=2, n_failed=1,
                            n_empty=0, total_cost_usd=0.5, abort_reason=None, batch_id="b0,b1")
    from memory.backends.dialect import dialect_for_connection

    p = dialect_for_connection(conn).param()
    row = conn.execute(f"SELECT status, n_success, finished_at, notes FROM enrichment_runs WHERE id = {p}",
                       (run_id,)).fetchone()
    assert (row[0], row[1]) == ("completed", 2) and row[2] is not None, f"observed: {row!r} {where}"
    assert json.loads(row[3])["finished_status"] == "completed"


def test_a_stale_running_row_is_reaped(each_backend, state):
    import m3_enrich_batch as eb
    from memory.backends.dialect import dialect_for_connection

    conn, made = state
    p = dialect_for_connection(conn).param()
    run_id = eb._record_run_started(
        conn, profile=_Profile(), db_path=None, source_variant="s", target_variant="t",
        batch_id="b", n_groups=1, slice_size=1, argv=[])
    made.append(run_id)
    conn.execute(f"UPDATE enrichment_runs SET started_at = {p} WHERE id = {p}",
                 ("2000-01-01T00:00:00Z", run_id))
    conn.commit()
    assert eb._reap_stale_runs(conn, stale_after_minutes=60) >= 1
    row = conn.execute(f"SELECT status, abort_reason FROM enrichment_runs WHERE id = {p}",
                       (run_id,)).fetchone()
    assert tuple(row) == ("aborted", "stale_run_reaped"), f"observed: {row!r} on {each_backend.name}"


def test_a_missing_state_file_is_refused_not_created(tmp_path, monkeypatch):
    import enrichment_state as estate

    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    missing = tmp_path / "typo.db"
    with pytest.raises(ValueError, match="observed: no SQLite DB at"):
        estate.open_state_conn(missing)
    assert not missing.exists(), "observed: open_state_conn created an empty store"
