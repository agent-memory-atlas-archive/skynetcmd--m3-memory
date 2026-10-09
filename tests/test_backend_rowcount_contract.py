"""Seam contract: after UPDATE/DELETE, cursor.rowcount counts rows MATCHED.

About 40 call sites outside memory/backends read rowcount after a plain UPDATE
or DELETE to mean "the row existed" (fences, re-encrypting a vault row,
deletes). SQLite and psycopg report matched rows; MySQL/MariaDB report rows
CHANGED by default, so an UPDATE that writes a value equal to the current one
reads 0 there and every such caller would misreport. The contract is owned by
StorageBackend.connection() (base.py) and checked here for every shipped
backend, so a new backend fails this test instead of 40 call sites.
"""
from __future__ import annotations

import uuid


def test_an_update_to_the_same_value_counts_the_matched_row(each_backend):
    from memory.backends import dialect

    p = dialect().param()
    service = f"M3_ROWCOUNT_PROBE_{uuid.uuid4().hex[:8].upper()}"
    with each_backend.connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO synchronized_secrets (service_name, encrypted_value, version, "
                f"origin_device, updated_at) VALUES ({p}, {p}, {p}, {p}, {p})",
                (service, "same", 1, "test", "2026-01-01T00:00:00Z"))
            conn.commit()

            cur.execute(f"UPDATE synchronized_secrets SET encrypted_value = {p} "
                        f"WHERE service_name = {p}", ("same", service))
            assert cur.rowcount == 1, f"{each_backend.name}: unchanged UPDATE reported {cur.rowcount}"

            cur.execute(f"UPDATE synchronized_secrets SET encrypted_value = {p} "
                        f"WHERE service_name = {p}", ("x", service + "_ABSENT"))
            assert cur.rowcount == 0, f"{each_backend.name}: UPDATE of no row reported {cur.rowcount}"
            conn.commit()
        finally:
            cur.execute(f"DELETE FROM synchronized_secrets WHERE service_name = {p}", (service,))
            deleted = cur.rowcount
            conn.commit()
    assert deleted == 1, f"{each_backend.name}: DELETE of one row reported {deleted}"
