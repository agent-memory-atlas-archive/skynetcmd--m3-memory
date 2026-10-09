"""crypto_provider's streaming SHA-256 and HMAC-SHA-256 services.

Both exist so that security uses (verifying a downloaded wheel, a keyed digest
of a secret) stay inside the FIPS boundary instead of calling hashlib/hmac
directly. Each must give the standard answer on BOTH backends; on wolfCrypt the
check runs in a child process that must also EXIT cleanly, since an undersized
opaque context corrupts the heap and shows up only at shutdown (see
test_fips_aes_context_size.py).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"

_CHILD = """
import hashlib, hmac, os, sys
sys.path.insert(0, {bin!r})
import crypto_provider as c
data = os.urandom(3_000_000)
s = c.provider.sha256_stream()
for i in range(0, len(data), 65536):
    s.update(data[i:i + 65536])
assert s.hexdigest() == hashlib.sha256(data).hexdigest(), "stream digest"
key = os.urandom(32)
assert c.get_hmac_sha256(key, data) == hmac.new(key, data, hashlib.sha256).hexdigest(), "hmac"
print(type(s).__name__, c.provider.backend)
"""


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("M3_FIPS_MODE", "M3_FIPS_STRICT", "M3_CRYPTO_BACKEND", "M3_WOLFSSL_LIB")}
    env.update(extra)
    return env


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", _CHILD.format(bin=str(_BIN))],
                          capture_output=True, text=True, env=env, timeout=120)


def test_default_backend_matches_the_standard_answers():
    r = _run(_env())
    assert r.returncode == 0, r.stderr[-600:]
    assert r.stdout.split()[-1] == "DEFAULT", r.stdout


def test_wolfcrypt_backend_matches_the_standard_answers_and_exits_cleanly():
    probe = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, {str(_BIN)!r}); import crypto_provider as c;"
         "print(c._resolve_wolfssl_path() or '')"],
        capture_output=True, text=True, env=_env(), timeout=60)
    lib = probe.stdout.strip()
    if not lib:
        pytest.skip("wolfSSL is not installed on this host")
    r = _run(_env(M3_FIPS_MODE="1", M3_WOLFSSL_LIB=lib))
    assert r.returncode == 0, (
        f"FIPS-mode child exited {r.returncode}; stderr: {r.stderr[-600:]}")
    assert r.stdout.split() == ["_WolfSha256Stream", "WOLFSSL"], r.stdout


def test_opaque_contexts_are_generously_sized():
    sys.path.insert(0, str(_BIN))
    import crypto_provider as c

    # Same rule as the Aes context: over-allocate a struct we cannot sizeof().
    assert c._WC_SHA256_CTX_BYTES >= 1024
    assert c._WC_HMAC_CTX_BYTES >= 1024
