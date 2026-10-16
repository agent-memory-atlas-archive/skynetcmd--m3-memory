"""FIPS posture beyond the provider's own services (docs/FIPS_MODULE_BOUNDARY.md).

Pinned here:
  * random bytes for keys, IVs, salts and tokens come from the provider, and no
    other module draws from os.urandom / secrets (a static ratchet);
  * every PostgreSQL connection goes through m3_core.pgconn, which applies the
    tier's transport rule: verify-full on a network under M3_FIPS_MODE, plus an
    attested validated TLS library (and no psycopg2-binary) under M3_FIPS_STRICT;
  * under M3_FIPS_STRICT with no attestation, every TLS handshake refuses;
  * the doctor's per-path report says which paths a tier refuses.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import ssl

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _cp():
    return importlib.import_module("crypto_provider")


# ── random generation ─────────────────────────────────────────────────────────


def test_random_bytes_and_tokens_come_from_the_provider():
    cp = _cp()
    a, b = cp.get_random_bytes(32), cp.get_random_bytes(32)
    assert len(a) == 32 and a != b
    assert len(cp.get_token_hex(16)) == 32
    assert len(cp.get_token_urlsafe(32)) >= 43
    assert {cp.random_choice("ab") for _ in range(200)} == {"a", "b"}
    with pytest.raises(IndexError):
        cp.random_choice("")
    with pytest.raises(ValueError):
        cp.provider.random_bytes(-1)


# A random draw for security belongs to crypto_provider. Each exemption says why.
_RANDOM_ALLOWED = {
    "bin/crypto_provider.py": "the owner",
    "bin/test_fips_integrity.py": "a self-test that feeds the provider an independent random key",
}
_RANDOM_CALLS = {
    ("os", "urandom"),
    ("secrets", "token_hex"),
    ("secrets", "token_urlsafe"),
    ("secrets", "token_bytes"),
    ("secrets", "choice"),
    ("secrets", "randbits"),
    ("secrets", "randbelow"),
}


def _calls(path: pathlib.Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        ):
            yield (node.func.value.id, node.func.attr), node.lineno


def _sources():
    for top in ("bin", "m3_memory"):
        for p in sorted((ROOT / top).rglob("*.py")):
            yield p.relative_to(ROOT).as_posix(), p


def test_no_random_draw_outside_the_provider():
    hits = [
        f"{rel}:{ln} {mod}.{fn}"
        for rel, p in _sources()
        if rel not in _RANDOM_ALLOWED
        for (mod, fn), ln in _calls(p)
        if (mod, fn) in _RANDOM_CALLS
    ]
    assert not hits, "draw security randomness through crypto_provider (get_random_bytes, get_token_*): " + ", ".join(
        hits
    )


def test_no_postgres_connection_outside_pgconn():
    hits = []
    for rel, p in _sources():
        if rel == "bin/m3_core/pgconn.py":
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
                owner = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
                if (owner == "psycopg2" and name == "connect") or name == "ThreadedConnectionPool":
                    hits.append(f"{rel}:{node.lineno}")
    assert not hits, "open PostgreSQL through m3_core.pgconn (the FIPS transport check): " + ", ".join(hits)


# ── PostgreSQL transport rule ─────────────────────────────────────────────────


@pytest.fixture
def tier(monkeypatch):
    cp = _cp()

    def set_tier(name, attested=False, binary=False):
        monkeypatch.setattr(cp, "_fips_mode", lambda: name in ("mode", "strict"))
        monkeypatch.setattr(cp, "_fips_strict", lambda: name == "strict")
        monkeypatch.setattr(cp, "_tls_validated_attested", lambda: attested)
        monkeypatch.setattr(cp, "_psycopg2_is_binary_wheel", lambda: binary)
        return cp

    return set_tier


NET = "postgresql://u:p@db.example.org:5432/db"


def test_outside_fips_any_dsn_is_allowed(tier):
    tier("off").require_pg_transport(NET)


def test_fips_mode_requires_verify_full_on_a_network(tier):
    cp = tier("mode")
    with pytest.raises(cp.FipsTransportError, match="verify-full"):
        cp.require_pg_transport(NET)
    with pytest.raises(cp.FipsTransportError):
        cp.require_pg_transport(NET + "?sslmode=require")
    cp.require_pg_transport(NET + "?sslmode=verify-full")
    cp.require_pg_transport("host=/run/postgresql dbname=x")
    cp.require_pg_transport("postgresql://u@127.0.0.1/db")


def test_strict_needs_the_attestation_and_no_binary_wheel(tier):
    good = NET + "?sslmode=verify-full"
    cp = tier("strict")
    with pytest.raises(cp.FipsTransportError, match="tls_validated"):
        cp.require_pg_transport(good)
    cp = tier("strict", attested=True, binary=True)
    with pytest.raises(cp.FipsTransportError, match="psycopg2-binary"):
        cp.require_pg_transport(good)
    tier("strict", attested=True, binary=False).require_pg_transport(good)


def test_pg_connect_checks_before_connecting(tier, monkeypatch):
    import psycopg2
    from m3_core import pgconn

    tier("mode")
    called = []
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: called.append(a))
    with pytest.raises(_cp().FipsTransportError):
        pgconn.pg_connect(NET)
    assert not called, "the connection was attempted before the transport check"


# ── strict TLS guard ──────────────────────────────────────────────────────────


def test_the_strict_guard_refuses_every_tls_handshake():
    cp = _cp()
    saved = (ssl.SSLContext.wrap_socket, ssl.SSLContext.wrap_bio)
    try:
        cp._install_strict_tls_guard()
        ctx = ssl.create_default_context()
        with pytest.raises(cp.FipsTransportError, match="tls_validated"):
            ctx.wrap_bio(ssl.MemoryBIO(), ssl.MemoryBIO(), server_hostname="example.org")
    finally:
        ssl.SSLContext.wrap_socket, ssl.SSLContext.wrap_bio = saved
        if hasattr(ssl.SSLContext, "_m3_fips_guard"):
            del ssl.SSLContext._m3_fips_guard


# ── the doctor's report ───────────────────────────────────────────────────────


def test_the_report_marks_a_refused_path_and_the_doctor_counts_it(tier, monkeypatch, capsys):
    from m3_core import secret_store

    monkeypatch.setattr(secret_store, "resolve_pg_dsn", lambda role: NET if role.value == "warehouse" else "")
    cp = tier("mode")
    rows = {r["path"]: r for r in cp.crypto_paths_report()["rows"]}
    assert rows["PostgreSQL (warehouse)"]["ok"] is False
    from doctor import fips_probe

    assert fips_probe.run(brief=True) == 1
    tier("off")
    assert fips_probe.run(brief=True) == 0, "outside FIPS tiers the report never fails the doctor"
