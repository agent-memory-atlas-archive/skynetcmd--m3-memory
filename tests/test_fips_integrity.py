"""
tests/test_fips_integrity.py — FIPS 140-3 boundary enforcement tests.

Exit criteria (Milestone 2):
  • All tests pass when M3_CRYPTO_BACKEND=DEFAULT (wolfSSL not required).
  • Under M3_FIPS_MODE=1, the provider either:
      a) Uses wolfSSL successfully (if the native library is present), OR
      b) Raises RuntimeError with a FATAL message (fail-closed enforcement).
  • SHA-256 and AES-256-GCM round-trips produce correct outputs on the
    DEFAULT backend (standard cryptography library).
  • No silent fallback from wolfSSL to stdlib under FIPS mode.
"""

import hashlib
import importlib
import json
import os
import subprocess
import sys
import textwrap

import pytest

from conftest import simulate_os

_BIN = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin"))
sys.path.insert(0, _BIN)


# ── [crypto] settings live in m3_config.toml ────────────────────────────────────
# m3 reads no crypto setting from the environment (M3_FIPS_MODE, M3_FIPS_STRICT,
# M3_CRYPTO_BACKEND, M3_WOLFSSL_LIB, M3_WOLFSSL_SHA256 are former names, not read).
# Tests write a [crypto] block into the SANDBOX's own config file -- not a new
# config root, because crypto_provider derives the trusted lib dir from the config
# root's parent -- and a subprocess inherits it through M3_CONFIG_ROOT.

_CRYPTO_KEY = {
    "M3_FIPS_MODE": "fips_mode",
    "M3_FIPS_STRICT": "fips_strict",
    "M3_CRYPTO_BACKEND": "backend",
    "M3_WOLFSSL_LIB": "wolfssl_lib",
    "M3_WOLFSSL_SHA256": "wolfssl_sha256",
}
_BEGIN, _END = "# >>> test crypto", "# <<< test crypto"
_CRYPTO_STATE: dict[str, object] = {}


def _cfg_path() -> str:
    return os.path.join(os.environ["M3_CONFIG_ROOT"], "m3_config.toml")


def _toml_value(key: str, value) -> str:
    if key in ("fips_mode", "fips_strict"):
        return "true" if str(value).strip().lower() in ("1", "true", "yes") else "false"
    return json.dumps(str(value))


def _crypto_block(values: dict) -> str:
    if not values:
        return ""
    body = "".join(f"{k} = {_toml_value(k, v)}\n" for k, v in values.items())
    return f"\n{_BEGIN}\n[crypto]\n{body}{_END}\n"


def _strip_crypto_block(text: str) -> str:
    i = text.find("\n" + _BEGIN)
    if i < 0:
        return text
    j = text.index(_END, i) + len(_END) + 1
    return text[:i] + text[j:]


def _write_crypto(values: dict) -> None:
    """Make ``values`` the [crypto] table of the sandbox's config file."""
    from m3_core.config import settings as st

    path = _cfg_path()
    with open(path, encoding="utf-8") as fh:
        text = _strip_crypto_block(fh.read())
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text + _crypto_block(values))
    st._reset_for_tests()


def _set_crypto(**values) -> None:
    """Merge [crypto] keys (None removes one) and rewrite the block."""
    for k, v in values.items():
        if v is None:
            _CRYPTO_STATE.pop(k, None)
        else:
            _CRYPTO_STATE[k] = v
    _write_crypto(dict(_CRYPTO_STATE))


def _clear_crypto() -> None:
    _CRYPTO_STATE.clear()
    if os.path.isfile(_cfg_path()):
        _write_crypto({})


def _crypto_from_env_names(env: dict) -> dict:
    return {_CRYPTO_KEY[k]: v for k, v in env.items() if k in _CRYPTO_KEY}


# ── Subprocess isolation for native-wolfSSL tests (#85) ─────────────────────────
#
# Loading crypto_provider under a wolfSSL backend initializes the native wolfSSL
# library via ctypes at import time (`provider = CryptoProvider(BACKEND)`). Once
# that DLL is loaded, any later importlib.reload() of crypto_provider re-inits it
# in the same process and, on Windows, segfaults the ENTIRE pytest run (Windows
# fatal exception: access violation, #85). A fresh interpreter loads it exactly
# once and exits cleanly, so every test that must load wolfSSL (real or the
# ctypes-mocked build) runs its assertions in a subprocess and we inspect the
# JSON verdict here. DEFAULT-backend tests don't touch native code and stay
# in-process. This mirrors conftest's native-embedder-disabled discipline.


def _run_fips_subprocess(body: str, env: dict) -> dict:
    """Execute `body` in a fresh interpreter; return the dict it prints via
    `_emit(**kv)`. ``env`` names [crypto] settings by their former env names
    (M3_FIPS_MODE, ...): they are written into the sandbox's config file, which
    the child reads through M3_CONFIG_ROOT. `_install_mock(...)` and `_bin_mock()`
    are available to the snippet for the ctypes-mocked wolfSSL build; the snippet
    changes crypto settings with `_set_child_crypto(**keys)`. A crash or a
    missing verdict surfaces as a readable test failure — never a crash of the
    shared test run (that's the whole point)."""
    crypto = _crypto_from_env_names(env)
    _write_crypto(crypto)
    prelude = textwrap.dedent(f"""
        import ctypes, hashlib, importlib, json, os, sys, tempfile
        sys.path.insert(0, {_BIN!r})

        def _emit(**kv):
            print("__FIPS__" + json.dumps(kv))

        _CHILD_CRYPTO = json.loads({json.dumps(json.dumps(crypto))})
        _CFG = os.path.join(os.environ["M3_CONFIG_ROOT"], "m3_config.toml")
        _BEGIN, _END = {_BEGIN!r}, {_END!r}

        def _write_child_crypto(values):
            with open(_CFG, encoding="utf-8") as fh:
                text = fh.read()
            i = text.find("\\n" + _BEGIN)
            if i >= 0:
                text = text[:i] + text[text.index(_END, i) + len(_END) + 1:]
            if values:
                def tv(k, v):
                    if k in ("fips_mode", "fips_strict"):
                        return "true" if str(v).strip().lower() in ("1", "true", "yes") else "false"
                    return json.dumps(str(v))
                body = "".join(f"{{k}} = {{tv(k, v)}}\\n" for k, v in values.items())
                text += f"\\n{{_BEGIN}}\\n[crypto]\\n{{body}}{{_END}}\\n"
            with open(_CFG, "w", encoding="utf-8", newline="\\n") as fh:
                fh.write(text)
            if "m3_core.config.settings" in sys.modules:
                sys.modules["m3_core.config.settings"]._reset_for_tests()

        def _set_child_crypto(**kv):
            _CHILD_CRYPTO.update(kv)
            _write_child_crypto(_CHILD_CRYPTO)

        def _bin_mock():
            # Load MockLibWolf from bin/test_fips_integrity.py by path (the tests/
            # file of the same name shadows it on sys.path). That bin module
            # transitively imports auth_utils -> crypto_provider at module scope;
            # under M3_FIPS_MODE=1 with no wolfSSL that import fails closed BEFORE
            # we can install the mock. So load it with FIPS mode temporarily off
            # (we only need the MockLibWolf class), then restore the env.
            import importlib.util as _ilu
            _write_child_crypto({{}})  # FIPS off while the mock module imports
            try:
                p = os.path.join({_BIN!r}, "test_fips_integrity.py")
                spec = _ilu.spec_from_file_location("_bin_fips_mock", p)
                m = _ilu.module_from_spec(spec); spec.loader.exec_module(m)
                return m.MockLibWolf
            finally:
                _write_child_crypto(_CHILD_CRYPTO)
                # crypto_provider was imported (DEFAULT backend) while FIPS was
                # off; drop it so _install_mock re-imports it fresh under FIPS +
                # the mock CDLL.
                sys.modules.pop("crypto_provider", None)

        def _install_mock(*, break_sha=False, non_fips=False):
            # Order matters: build the mock + pin the fake lib + patch ctypes.CDLL
            # BEFORE importing crypto_provider, so its module-level FIPS init sees
            # the mock. Returns the freshly-imported crypto_provider module.
            MockLibWolf = _bin_mock()
            mock = MockLibWolf()
            if break_sha:
                def _bad_sha(in_buf, sz, out_buf):
                    ctypes.memmove(out_buf, b"\\x00" * 32, 32); return 0
                mock.wc_Sha256Hash.func = _bad_sha
            if non_fips:
                for sym in ("wolfCrypt_GetStatus_fips", "wc_SetSeed_Cb", "wolfCrypt_SetCb_fips"):
                    if hasattr(mock, sym):
                        delattr(mock, sym)
            real_cdll = ctypes.CDLL
            def _mock_cdll(name, *a, **k):
                if "wolfssl" in str(name).lower():
                    return mock
                return real_cdll(name, *a, **k)
            fake = os.path.join(tempfile.mkdtemp(prefix="m3wolf-"), "wolfssl.dll")
            with open(fake, "wb") as f:
                f.write(b"mock-wolfssl-placeholder")
            _set_child_crypto(wolfssl_lib=fake)
            ctypes.CDLL = _mock_cdll
            sys.modules.pop("crypto_provider", None)  # ensure a fresh FIPS init
            # crypto_provider reads [crypto] through the config registry, whose
            # snapshot was cached by _bin_mock's import with FIPS off.
            from m3_core.config import settings as _st
            _st._reset_for_tests()
            import crypto_provider
            return crypto_provider
    """)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", prelude + textwrap.dedent(body)],
            capture_output=True,
            text=True,
            env={**os.environ, **{k: str(v) for k, v in env.items() if k not in _CRYPTO_KEY}},
            timeout=120,
        )
    finally:
        _clear_crypto()
    marker = "__FIPS__"
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith(marker)), None)
    assert line is not None, (
        f"FIPS subprocess produced no verdict (rc={proc.returncode}).\n"
        f"stdout:\n{proc.stdout[-3000:]}\nstderr:\n{proc.stderr[-2000:]}"
    )
    return json.loads(line[len(marker) :])


# The crypto env as it was when this module was imported -- i.e. the state
# every test in the session inherits. Captured at import because os.environ
# is mid-restore during fixture finalizers and cannot be trusted there.
_SESSION_CRYPTO_ENV = {k: os.environ.get(k) for k in ("M3_FIPS_MODE", "M3_FIPS_STRICT")}


# ── Helpers ────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _restore_crypto_provider():
    """Undo `_reload_provider`'s module mutation after every test in this file.

    `_reload_provider` calls `importlib.reload(crypto_provider)` to re-evaluate
    module-level env knobs. monkeypatch restores the ENV VARS at teardown, but
    nothing restored the MODULE: the last reload's state (typically
    M3_CRYPTO_BACKEND=DEFAULT) leaked into every test that ran afterwards in the
    same process.

    That is a cross-file leak, not a local quirk. `crypto_provider` sits under
    the embed path, so a later test importing `memory.embed` could bind a
    provider object reloaded under this file's env rather than its own —
    reproduced 2026-09-07: `test_zero_lag_write.py::test_write_defers_and_is_
    fast_without_embedder` passes alone and FAILS when this file runs first
    (that test's own docstring already documents being sensitive to exactly this
    class of module eviction). The failure blames the victim, so bisecting to
    the cause costs real time.

    ── WHY THIS FIXTURE BUILDS ITS OWN ENVIRONMENT ──────────────────────────

    An earlier version reloaded "under the pristine environment monkeypatch has
    by then restored". That premise was FALSE, and measuring it is what found
    this: at the moment an autouse fixture's teardown runs, monkeypatch has NOT
    yet undone the test's `setenv` calls. Probed directly --
    `test_lib_dir_pin_outranks_config_root` enters teardown with M3_LIB_DIR
    still pointing at its own empty tmp `pinned/` directory.

    So the repair reload ran under the POLLUTED env, found no wolfSSL library,
    and -- because the whole thing was wrapped in `except Exception: pass` --
    failed silently, leaving exactly the broken module it existed to prevent:
    `backend=DEFAULT, _initialized=False` while M3_FIPS_MODE=1 is still live.
    Every later test that hits the crypto boundary then dies with "FIPS boundary
    violation", blaming a victim with no connection to the cause. Measured
    2026-09-17: a 2-test repro (this file, then
    `test_langchain_store.py::test_search_returns_scored_items`) reproduces it.

    The fix is to stop DEPENDING on teardown ordering and set the crypto env
    explicitly, which is also what makes the repair deterministic:

      * M3_CRYPTO_BACKEND is CLEARED, so the reload re-derives the real backend
        rather than inheriting this file's DEFAULT.
      * M3_LIB_DIR is re-pinned to the real installed library, mirroring what
        the sandbox fixture in conftest.py does. That fixture uses
        `if not os.environ.get("M3_LIB_DIR")`, so once a test has set the var
        the sandbox will NOT re-pin it -- this fixture must.

    ⚠ The reload is NOT wrapped in a bare `except: pass`. Under M3_FIPS_MODE=1
    a failed reload is fail-closed by design, and swallowing it is the §3
    violation that hid this bug: the alarm was silenced at precisely the point
    it was supposed to fire. A repair that cannot repair must say so HERE,
    where the cause is, not 40 tests later somewhere else.
    """
    yield

    # Build the env the reload needs, rather than assuming teardown order.
    #
    # _RESTORE_KEYS is every env var that feeds crypto_provider's module-level
    # init. All of them must be set to the state the NEXT test will see -- not
    # the state this test left, and not whatever monkeypatch has restored so
    # far, because at finalizer time that restore is still in progress.
    # Derived from crypto_provider, not guessed -- these are every M3_* var the
    # module reads at init. Grep to re-derive if that set changes:
    #   grep -oE 'M3_[A-Z0-9_]+' bin/crypto_provider.py | sort -u
    # (M3_CONFIG_ROOT / M3_MEMORY_ROOT are also read, but conftest's sandbox owns
    # those and M3_LIB_DIR outranks both for library discovery.)
    _RESTORE_KEYS = (
        "M3_CRYPTO_BACKEND",
        "M3_LIB_DIR",
        "M3_FIPS_MODE",
        "M3_FIPS_STRICT",
        "M3_WOLFSSL_LIB",
        "M3_WOLFSSL_SHA256",
    )
    _saved = {k: os.environ.get(k) for k in _RESTORE_KEYS}

    # The reload must run under the SESSION's FIPS state, which is what the next
    # test inherits. Measured: at finalizer time M3_FIPS_MODE reads None even on
    # a box that exported M3_FIPS_MODE=1, because monkeypatch has not undone
    # `_reload_provider`'s delenv yet. Reloading under that transient None built
    # a legitimate DEFAULT provider -- then monkeypatch restored FIPS_MODE=1
    # around it, leaving a DEFAULT/uninitialized provider live under FIPS. Every
    # later crypto call then fails the boundary check. Read the session values
    # captured at import, never os.environ, for exactly that reason.
    for _k in ("M3_FIPS_MODE", "M3_FIPS_STRICT"):
        _sess = _SESSION_CRYPTO_ENV.get(_k)
        if _sess is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _sess

    # Both wolfSSL pins must go. M3_WOLFSSL_LIB pins a full path verbatim, so a
    # test's fake .dll would survive; M3_WOLFSSL_SHA256 pins an expected digest,
    # and the integrity tests deliberately set a WRONG one ("00"*32). Leaving
    # that behind makes the repair reload verify the REAL library against a bogus
    # hash and fail closed -- which is correct behaviour reacting to leftover
    # test state, i.e. the same class of bug this fixture exists to prevent.
    os.environ.pop("M3_WOLFSSL_LIB", None)
    os.environ.pop("M3_WOLFSSL_SHA256", None)
    os.environ.pop("M3_CRYPTO_BACKEND", None)
    # The settings that matter are the [crypto] block this test wrote into the
    # sandbox's config file: drop it so the repair reload builds the provider the
    # next test gets (the registry default, DEFAULT backend).
    _clear_crypto()

    # Re-pin the real library dir. conftest's sandbox only sets M3_LIB_DIR when
    # it is unset, so once a test has set it the sandbox will not re-pin it.
    _real_lib = os.path.join(os.path.expanduser("~"), ".m3", "lib")
    if os.path.isdir(_real_lib):
        os.environ["M3_LIB_DIR"] = _real_lib
    try:
        import crypto_provider

        importlib.reload(crypto_provider)
    finally:
        # Restore what we touched; monkeypatch owns everything else.
        for _k, _v in _saved.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v


def _reset_crypto_settings():
    from m3_core.config import settings as st

    st._reset_for_tests()


def _set_crypto_env(monkeypatch, name, value):
    """A [crypto] setting (named by its former env name) set after the provider
    loaded: written into the config file; the snapshot is rebuilt for the next read."""
    _set_crypto(**{_CRYPTO_KEY[name]: value})


def _reload_provider(monkeypatch, backend=None, fips_mode=None, strict=None):
    """Reload crypto_provider with specific env knobs (DEFAULT backend only — the
    native-wolfSSL path is exercised via _run_fips_subprocess to avoid the #85
    in-process reload segfault)."""
    _CRYPTO_STATE.clear()
    _set_crypto(backend=backend, fips_mode=fips_mode, fips_strict=strict)

    # Reload so module-level BACKEND and provider re-evaluate the settings
    # (_set_crypto rebuilt the registry snapshot).
    import crypto_provider

    importlib.reload(crypto_provider)
    return crypto_provider


# ── DEFAULT backend: correctness ───────────────────────────────────────────────


class TestDefaultBackend:
    """Standard library path — always available, no wolfSSL required."""

    def test_sha256_known_vector(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        data = b"abc"
        expected = hashlib.sha256(data).hexdigest()
        assert cp.provider.sha256(data) == expected

    def test_sha256_empty_input(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        expected = hashlib.sha256(b"").hexdigest()
        assert cp.provider.sha256(b"") == expected

    def test_encrypt_decrypt_roundtrip(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        key = os.urandom(32)  # AES-256
        plaintext = b"M3 FIPS test payload"
        token = cp.provider.encrypt(plaintext, key)
        recovered = cp.provider.decrypt(token, key)
        assert recovered == plaintext or recovered.rstrip(b"\x00") == plaintext

    def test_encrypt_produces_nonce_prefix(self, monkeypatch):
        """Ciphertext must be at least 12 (nonce) + 1 (data) + 16 (tag) bytes."""
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        key = os.urandom(32)
        token = cp.provider.encrypt(b"x", key)
        assert len(token) >= 29  # 12 nonce + 1 byte + 16 tag

    def test_wrong_key_raises(self, monkeypatch):
        """Decryption with a different key must fail (authentication tag mismatch)."""
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        key1 = os.urandom(32)
        key2 = os.urandom(32)
        token = cp.provider.encrypt(b"secret", key1)
        with pytest.raises(Exception):
            cp.provider.decrypt(token, key2)

    def test_backend_attribute(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        assert cp.provider.backend == "DEFAULT"

    def test_get_sha256_convenience(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        data = b"hello world"
        assert cp.get_sha256(data) == hashlib.sha256(data).hexdigest()


# ── FIPS mode fail-closed enforcement ─────────────────────────────────────────


class TestFIPSEnforcement:
    """Validate that M3_FIPS_MODE=1 enforces fail-closed policy."""

    def test_fips_mode_sets_backend_to_wolfssl(self):
        """The module must select WOLFSSL when M3_FIPS_MODE=1. Isolated (#85):
        either wolfSSL loads (BACKEND==WOLFSSL) or it is absent and the module
        fails closed with a RuntimeError mentioning FIPS — both accepted."""
        v = _run_fips_subprocess(
            """
            try:
                import crypto_provider
                _emit(outcome="loaded", backend=crypto_provider.BACKEND)
            except RuntimeError as exc:
                _emit(outcome="raised", msg=str(exc))
            """,
            env={"M3_FIPS_MODE": "1"},
        )
        if v["outcome"] == "loaded":
            assert v["backend"] == "WOLFSSL"
        else:
            assert "FIPS" in v["msg"] or "FATAL" in v["msg"]

    def _assert_fips_boundary(self, primitive: str):
        """Under M3_FIPS_MODE=1 a primitive must NOT silently use stdlib: either
        the module fails closed at import (wolfSSL absent), or wolfSSL loads and a
        stub DEFAULT-backend provider raises 'FIPS boundary violation'. Isolated
        in a subprocess (#85)."""
        v = _run_fips_subprocess(
            f"""
            try:
                import crypto_provider
            except RuntimeError as exc:
                _emit(outcome="raised", msg=str(exc)); sys.exit(0)
            prov = crypto_provider.CryptoProvider.__new__(crypto_provider.CryptoProvider)
            prov.backend = "DEFAULT"; prov._initialized = False
            prov._libwolf = None; prov._fips_cb_ref = None
            try:
                if {primitive!r} == "sha256":
                    prov.sha256(b"test")
                elif {primitive!r} == "encrypt":
                    prov.encrypt(b"data", os.urandom(32))
                else:
                    prov.decrypt(b"\\x00" * 29, os.urandom(32))
                _emit(outcome="no_raise")
            except RuntimeError as exc:
                _emit(outcome="boundary", msg=str(exc))
            """,
            env={"M3_FIPS_MODE": "1"},
        )
        if v["outcome"] == "raised":
            assert "FIPS" in v["msg"] or "FATAL" in v["msg"]
        else:
            assert v["outcome"] == "boundary", f"{primitive} must not silently use stdlib under FIPS mode"
            assert "FIPS boundary violation" in v["msg"]

    def test_fips_mode_without_wolfssl_raises_on_sha256(self):
        """Under M3_FIPS_MODE=1, sha256() must NOT silently use hashlib."""
        self._assert_fips_boundary("sha256")

    def test_fips_mode_without_wolfssl_raises_on_encrypt(self):
        """Under M3_FIPS_MODE=1, encrypt() must NOT silently use stdlib."""
        self._assert_fips_boundary("encrypt")

    def test_fips_mode_without_wolfssl_raises_on_decrypt(self):
        """Under M3_FIPS_MODE=1, decrypt() must NOT silently use stdlib."""
        self._assert_fips_boundary("decrypt")

    def test_non_fips_mode_allows_stdlib(self, monkeypatch):
        """Without M3_FIPS_MODE, sha256 must succeed using stdlib fallback."""
        cp = _reload_provider(monkeypatch, backend="DEFAULT", fips_mode=None)
        result = cp.provider.sha256(b"plaintext")
        assert result == hashlib.sha256(b"plaintext").hexdigest()


# ── TLS context ────────────────────────────────────────────────────────────────


class TestTLSContext:
    """The SSL context from the DEFAULT backend must enforce TLS 1.3."""

    def test_default_ssl_context_tls13_minimum(self, monkeypatch):
        import ssl

        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        ctx = cp.provider.get_ssl_context()
        assert isinstance(ctx, ssl.SSLContext)
        # TLS 1.3 minimum version (Python 3.7+ with openssl 1.1.1+)
        if hasattr(ssl, "TLSVersion"):
            assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_3


# ── Error code table ───────────────────────────────────────────────────────────


class TestErrorCodeTable:
    def test_known_fips_error_codes_present(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        assert -203 in cp.WOLF_ERROR_CODES  # FIPS Integrity check failed
        assert -204 in cp.WOLF_ERROR_CODES  # FIPS state in error


# ── Power-up Known-Answer-Tests (Phase 2) ──────────────────────────────────────
# The MockLibWolf install + reload that these used in-process now lives in the
# subprocess prelude's `_install_mock()` (see _run_fips_subprocess) — the ctypes
# CDLL patch + native provider init runs in a fresh process, so it can never
# corrupt the shared test run (#85).


class TestPowerUpKATs:
    def test_kats_pass_against_correct_backend(self):
        v = _run_fips_subprocess(
            """
            cp = _install_mock()
            p = cp.CryptoProvider("WOLFSSL")
            p._run_self_tests()  # idempotent re-run must not raise
            _emit(initialized=p._initialized, backend=p.backend)
            """,
            env={"M3_FIPS_MODE": "1"},
        )
        assert v["initialized"] is True
        assert v["backend"] == "WOLFSSL"

    def test_kat_vectors_are_canonical(self, monkeypatch):
        """Lock the published KAT constants so a future edit can't silently
        weaken them (SHA-256('abc') is the NIST FIPS 180-4 example)."""
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        assert cp._KAT_SHA256_DIGEST == ("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        assert cp._KAT_SHA256_INPUT == b"abc"
        assert len(bytes.fromhex(cp._KAT_PBKDF2_KEY_HEX)) == 32
        assert len(cp._KAT_AESGCM_KEY) == 32
        assert len(cp._KAT_AESGCM_NONCE) == 12

    def test_broken_backend_fatally_aborts_under_fips(self):
        """A primitive that computes the WRONG answer must abort init under
        M3_FIPS_MODE=1 — the FIPS power-up-self-test failure contract. With the
        broken mock installed before import, the module-level
        `provider = CryptoProvider(BACKEND)` raises during import itself; a direct
        construction raises identically. Assert either path raises."""
        v = _run_fips_subprocess(
            """
            raised = False
            try:
                cp = _install_mock(break_sha=True)
                cp.CryptoProvider("WOLFSSL")
            except RuntimeError:
                raised = True
            _emit(raised=raised)
            """,
            env={"M3_FIPS_MODE": "1"},
        )
        assert v["raised"] is True

    def test_self_tests_method_exists(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        assert hasattr(cp.CryptoProvider, "_run_self_tests")


# ── Tiered FIPS mode: M3_FIPS_MODE vs M3_FIPS_STRICT (Phase 4) ──────────────────


class TestTieredFipsMode:
    """M3_FIPS_MODE accepts the open-source (non-FIPS) wolfCrypt build for
    homelab/dev use; M3_FIPS_STRICT additionally requires the CMVP-validated
    FIPS module and refuses a non-FIPS lib with a clear message."""

    def test_fips_mode_accepts_non_fips_wolfssl(self):
        """Homelab tier: M3_FIPS_MODE=1 + open-source wolfSSL (no FIPS symbols)
        -> initializes, runs real wolfCrypt, but flags itself non-validated."""
        v = _run_fips_subprocess(
            """
            cp = _install_mock(non_fips=True)
            p = cp.CryptoProvider("WOLFSSL")
            _emit(initialized=p._initialized, backend=p.backend,
                  validated=p._fips_validated,
                  sha_ok=(p.sha256(b"abc") == hashlib.sha256(b"abc").hexdigest()))
            """,
            env={"M3_FIPS_MODE": "1"},
        )
        assert v["initialized"] is True
        assert v["backend"] == "WOLFSSL"
        assert v["validated"] is False  # open-source build is NOT validated
        assert v["sha_ok"] is True  # real wolfCrypt crypto still works

    def test_fips_strict_refuses_non_fips_wolfssl(self):
        """Strict tier: M3_FIPS_STRICT=1 + open-source wolfSSL -> FATAL, with a
        message naming the open-source-vs-validated distinction."""
        v = _run_fips_subprocess(
            """
            msg = None
            try:
                cp = _install_mock(non_fips=True)
                cp.CryptoProvider("WOLFSSL")
            except RuntimeError as exc:
                msg = str(exc)
            _emit(msg=msg)
            """,
            env={"M3_FIPS_MODE": "1", "M3_FIPS_STRICT": "1"},
        )
        assert v["msg"] is not None, "strict mode must refuse a non-FIPS build"
        assert "OPEN-SOURCE" in v["msg"] or "validated" in v["msg"].lower()

    def test_fips_strict_accepts_validated_wolfssl(self):
        """Strict tier with the FIPS symbols present (mock simulates validated)
        -> initializes and reports validated."""
        v = _run_fips_subprocess(
            """
            cp = _install_mock(non_fips=False)
            p = cp.CryptoProvider("WOLFSSL")
            _emit(initialized=p._initialized, validated=p._fips_validated)
            """,
            env={"M3_FIPS_MODE": "1", "M3_FIPS_STRICT": "1"},
        )
        assert v["initialized"] is True
        assert v["validated"] is True

    def test_fips_validated_flag_exists(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        p = cp.CryptoProvider("DEFAULT")
        assert hasattr(p, "_fips_validated")
        assert p._fips_validated is False  # DEFAULT backend is never validated


# ── Secure wolfSSL discovery: DLL-hijack hardening (Phase 5) ────────────────────


class TestSecureWolfsslDiscovery:
    """The crypto lib must load ONLY from trusted absolute paths M3 controls —
    never a bare name (which would let the OS search the CWD / %PATH% /
    LD_LIBRARY_PATH and enable a weaker-DLL injection)."""

    def test_candidates_are_all_absolute(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        for c in cp._trusted_wolfssl_candidates():
            assert os.path.isabs(c), f"non-absolute candidate (search-order risk): {c}"

    def test_macos_checks_dylib_in_m3_lib(self, monkeypatch):
        """macOS regression: the build helper installs libwolfssl.dylib to
        ~/.m3/lib, so the loader MUST check .dylib there (not only .so)."""
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        simulate_os(monkeypatch, "Darwin")
        m3lib = cp._m3_lib_dir()
        cands = cp._trusted_wolfssl_candidates()
        assert os.path.join(m3lib, "libwolfssl.dylib") in cands
        assert os.path.join(m3lib, "libwolfssl.so") in cands  # also accept .so

    def test_windows_uses_dll(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        simulate_os(monkeypatch, "Windows")
        cands = cp._trusted_wolfssl_candidates()
        assert all(c.endswith("wolfssl.dll") for c in cands), cands

    def test_linux_uses_so_only(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        simulate_os(monkeypatch, "Linux")
        cands = cp._trusted_wolfssl_candidates()
        assert all(c.endswith("libwolfssl.so") for c in cands), cands

    def test_candidates_exclude_cwd_and_path(self, monkeypatch):
        """The trusted list must not include the current dir or PATH entries."""
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        cands = cp._trusted_wolfssl_candidates()
        cwd = os.path.abspath(os.getcwd())
        for c in cands:
            assert os.path.dirname(os.path.abspath(c)) != cwd, "CWD is a hijack vector"
        # No candidate should come from a %PATH%/LD_LIBRARY_PATH dir.
        path_dirs = (os.environ.get("PATH", "") + os.pathsep + os.environ.get("LD_LIBRARY_PATH", "")).split(os.pathsep)
        path_dirs = {os.path.abspath(d) for d in path_dirs if d}
        for c in cands:
            assert os.path.dirname(os.path.abspath(c)) not in path_dirs

    def test_lib_dir_pin_relocates_search_but_keeps_per_os_filename(self, monkeypatch, tmp_path):
        """M3_LIB_DIR pins the DIRECTORY; the filename stays platform-derived.

        This is the difference from M3_WOLFSSL_LIB (used verbatim, any name).
        Pinning the directory lets a relocated M3_CONFIG_ROOT — a test sandbox,
        a container, a per-user install — still reach the real installed
        library without hard-coding a host-specific filename.
        """
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        monkeypatch.delenv("M3_WOLFSSL_LIB", raising=False)
        monkeypatch.setenv("M3_LIB_DIR", str(tmp_path))
        simulate_os(monkeypatch, "Linux")

        cands = cp._trusted_wolfssl_candidates()
        from_pin = [c for c in cands if os.path.dirname(c) == str(tmp_path)]
        assert from_pin, cands
        assert all(c.endswith("libwolfssl.so") for c in from_pin), from_pin

    def test_lib_dir_pin_outranks_config_root(self, monkeypatch, tmp_path):
        """M3_LIB_DIR wins over the M3_CONFIG_ROOT-derived default."""
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path / "elsewhere" / "config"))
        monkeypatch.setenv("M3_LIB_DIR", str(tmp_path / "pinned"))
        assert cp._m3_lib_dir() == str(tmp_path / "pinned")

    def test_explicit_pin_takes_precedence(self, monkeypatch, tmp_path):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        pinned = tmp_path / "wolfssl.dll"
        pinned.write_bytes(b"x")
        _set_crypto_env(monkeypatch, "M3_WOLFSSL_LIB", str(pinned))
        assert cp._trusted_wolfssl_candidates()[0] == os.path.abspath(str(pinned))
        assert cp._resolve_wolfssl_path() == str(pinned)

    def test_resolve_returns_none_when_no_trusted_lib(self, monkeypatch, tmp_path):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        monkeypatch.delenv("M3_WOLFSSL_LIB", raising=False)
        # M3_LIB_DIR outranks both roots (and the sandbox fixture sets it to the
        # real ~/.m3/lib so FIPS boxes can run the suite) — clear it too.
        monkeypatch.delenv("M3_LIB_DIR", raising=False)
        # Point M3 roots at an empty temp area so ~/.m3/lib resolves nowhere real.
        # BOTH roots must move: _m3_lib_dir() prefers M3_CONFIG_ROOT's parent over
        # M3_MEMORY_ROOT, so on a dev box that has a real ~/.m3/lib/wolfssl.dll,
        # redirecting only M3_MEMORY_ROOT leaves the real lib discoverable.
        empty = str(tmp_path / "m3empty")  # pytest reaps it; mkdtemp left one per run
        monkeypatch.setenv("M3_MEMORY_ROOT", empty)
        monkeypatch.setenv("M3_CONFIG_ROOT", os.path.join(empty, "config"))
        # System dirs won't have wolfssl on a test box; expect None.
        assert cp._resolve_wolfssl_path() is None

    def test_integrity_pin_rejects_mismatch(self, monkeypatch, tmp_path):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        lib = tmp_path / "wolfssl.dll"
        lib.write_bytes(b"the-real-bytes")
        _set_crypto_env(monkeypatch, "M3_WOLFSSL_LIB", str(lib))
        _set_crypto_env(monkeypatch, "M3_WOLFSSL_SHA256", "00" * 32)  # wrong
        with pytest.raises(RuntimeError, match="integrity check failed"):
            cp._resolve_wolfssl_path()

    def test_integrity_pin_accepts_match(self, monkeypatch, tmp_path):
        import hashlib

        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        lib = tmp_path / "wolfssl.dll"
        data = b"the-real-bytes"
        lib.write_bytes(data)
        _set_crypto_env(monkeypatch, "M3_WOLFSSL_LIB", str(lib))
        _set_crypto_env(monkeypatch, "M3_WOLFSSL_SHA256", hashlib.sha256(data).hexdigest())
        assert cp._resolve_wolfssl_path() == str(lib)

    def test_fips_mode_fatal_when_no_trusted_lib(self):
        """FIPS mode + no wolfSSL in any trusted path -> FATAL (never silently
        falls back, never searches CWD/PATH). Isolated (#85): constructing the
        WOLFSSL provider touches the native loader."""
        v = _run_fips_subprocess(
            """
            # M3_LIB_DIR outranks the config root — clear it or the real lib is found.
            os.environ.pop("M3_LIB_DIR", None)
            # Move the config root (the trusted lib dir derives from its parent) so a
            # real ~/.m3/lib/wolfssl.dll on a dev box can't be discovered; its new
            # m3_config.toml carries FIPS mode and no wolfssl_lib pin.
            empty = tempfile.mkdtemp(prefix="m3empty-")
            os.makedirs(os.path.join(empty, "config"))
            with open(os.path.join(empty, "config", "m3_config.toml"), "w", encoding="utf-8") as fh:
                fh.write("[crypto]\\nfips_mode = true\\n")
            os.environ["M3_CONFIG_ROOT"] = os.path.join(empty, "config")
            # Under FIPS mode with no trusted lib, fail-closed can fire either at
            # module import (the module-level provider init) OR at explicit
            # construction — accept a RuntimeError from either point.
            msg = None
            try:
                import crypto_provider as cp
                cp.CryptoProvider("WOLFSSL")
            except RuntimeError as exc:
                msg = str(exc)
            _emit(msg=msg, empty=empty)
            """,
            env={"M3_FIPS_MODE": "1"},
        )
        assert v["msg"] is not None, "FIPS mode with no trusted lib must be FATAL"
        assert "TRUSTED" in v["msg"] or "trusted" in v["msg"]
        # It names the folder it searched (the moved roots' lib), not a fixed
        # ~/.m3/lib that it never looked in.
        assert os.path.join(v["empty"], "lib") in v["msg"], v["msg"]
        assert "~/.m3/lib" not in v["msg"], v["msg"]
        assert "M3_LIB_DIR" in v["msg"], v["msg"]
