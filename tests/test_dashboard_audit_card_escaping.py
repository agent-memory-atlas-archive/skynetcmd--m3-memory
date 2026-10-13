"""The audit card renders stored and path values through the encoder for their
context: a memory's title, content, type and user id are untrusted (any write path
can set them), and a memory id arrives in the URL path. Unescaped, a title in an
<h3>, content inside a <textarea>, or an id inside an inline onclick ran script in
the operator's browser.
"""

from __future__ import annotations

import sqlite3

import pytest

from conftest import create_full_main_schema, skip_reason

pytest.importorskip("fastapi", reason=skip_reason("dep", "dashboard needs the [dashboard] extra"))

_PAYLOAD = "<script>alert(1)</script>"


def _render(tmp_path, memory_id):
    import dashboard_server as D

    db_path = tmp_path / "audit.db"
    create_full_main_schema(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO memory_items (id, type, title, content, user_id, is_deleted) VALUES (?, ?, ?, ?, ?, 0)",
        (memory_id, f"t{_PAYLOAD}", f'x"{_PAYLOAD}', f"</textarea>{_PAYLOAD}", f"u{_PAYLOAD}"),
    )
    conn.commit()
    D.ph = lambda: "?"  # the card's SQL placeholder (SQLite connection here)
    try:
        return D.render_audit_card(memory_id, conn), D
    finally:
        conn.close()


def test_stored_values_are_escaped_in_the_card(tmp_path):
    out, _ = _render(tmp_path, "m-1")
    assert _PAYLOAD not in out, "a stored value reached the page as live markup"
    assert "&lt;script&gt;" in out
    assert "</textarea><script>" not in out


def test_a_hostile_memory_id_cannot_break_out_of_ids_urls_or_the_handler(tmp_path):
    hostile = "a'); alert(1); ('\"><img src=x onerror=alert(1)>"
    out, D = _render(tmp_path, hostile)
    assert "onerror=alert(1)>" not in out and "'); alert(1); ('" not in out
    dom_id = D._dom_id(hostile)
    assert f'id="card-{dom_id}"' in out and f"#card-{dom_id}" in out
    assert f"/api/audit/resolve/{D._url_id(hostile)}" in out
