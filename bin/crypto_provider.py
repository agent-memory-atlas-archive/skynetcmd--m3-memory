#!/usr/bin/env python3
"""
crypto_provider.py — Abstraction layer for FIPS-ready cryptography.
Supports standard Python crypto and wolfSSL/wolfCrypt backends.
"""

import ctypes
import hashlib
import logging
import os
import threading
import weakref

# The tier and every [crypto] setting come from crypto_policy, the security
# seam's rules (it loads no crypto library, so doctor can ask it even when this
# module fails closed). Re-exported under the names this module always used.
from crypto_policy import (  # noqa: F401  (re-exported API)
    FipsTransportError,
    fips_tier,
    transport_allowed,
)
from crypto_policy import crypto_setting as _crypto_setting  # noqa: F401
from crypto_policy import crypto_text as _crypto_text
from crypto_policy import fips_active as _fips_active
from crypto_policy import fips_mode as _fips_mode
from crypto_policy import fips_strict as _fips_strict
from crypto_policy import tls_validated_attested as _tls_validated_attested
from m3_core.seams import is_macos, is_windows

logger = logging.getLogger("m3-crypto")

# FIPS 140-3 Error Code Mapping
WOLF_ERROR_CODES = {
    -197: "FIPS mode not allowed for this algorithm",
    -200: "HMAC key length too short for FIPS",
    -203: "FIPS Integrity check failed (In-Core Hash mismatch)",
    -204: "FIPS state is in error",
}

# ── Approved-algorithm whitelist (the FIPS algorithm boundary) ───────────────
# The ONLY cryptographic primitives M3 uses for security. This is the
# authoritative list; the boundary is enforced two ways:
#   1. crypto_provider exposes only these as services (sha256, AES-256-GCM,
#      PBKDF2-HMAC-SHA256, TLS 1.3) — there's no API to ask it for a weak algo.
#   2. tests/test_fips_algorithm_whitelist.py statically scans bin/ + m3_memory/
#      and FAILS if any non-approved primitive (MD5, SHA-1-for-security, DES,
#      RC4, new Fernet) is used directly — Python imports can't be sandboxed, so
#      enforcement-by-test is the mechanism. Non-security hashes must pass
#      `usedforsecurity=False`; legacy Fernet is decrypt-only (migration).
FIPS_APPROVED_ALGORITHMS = frozenset(
    {
        "AES-256-GCM",  # authenticated encryption (vault)
        "SHA-256",
        "SHA-384",
        "SHA-512",
        "HMAC-SHA-256",
        "PBKDF2-HMAC-SHA256",  # key derivation
        "CTR-DRBG",  # via the validated module when present
        "TLS1.3",  # transport
    }
)

# Primitives that are NEVER approved for security use anywhere in M3.
FIPS_BLOCKED_ALGORITHMS = frozenset(
    {
        "MD5",
        "MD4",
        "MD2",
        "SHA-1",  # allowed ONLY with usedforsecurity=False (non-crypto)
        "DES",
        "3DES",
        "TripleDES",
        "RC4",
        "ARC4",
        "Blowfish",
        "RC2",
        "Fernet",  # AES-128-CBC; legacy decrypt-only, never new writes
    }
)

# ── FIPS 140-3 power-up Known-Answer-Test (KAT) vectors ──────────────────────
# In-process self-tests run at provider init (wolfSSL backend) to prove each
# approved primitive computes the documented answer before any real crypto is
# served. Vectors are canonical:
#   - SHA-256: NIST FIPS 180-4 example, SHA256("abc").
#   - PBKDF2-SHA256: RFC 7914 §11 (passwd / salt / 1 iter / 32-byte dk).
#   - AES-256-GCM: deterministic encrypt with a fixed key+nonce (round-trip +
#     known ciphertext||tag). The tag also exercises GCM authentication.
# A KAT mismatch under M3_FIPS_MODE is fatal — the module enters an error state
# and refuses to serve (FIPS 140-3 requirement).
_KAT_SHA256_INPUT = b"abc"
_KAT_SHA256_DIGEST = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"

_KAT_PBKDF2_PASSWORD = b"passwd"
_KAT_PBKDF2_SALT = b"salt"
_KAT_PBKDF2_ITERATIONS = 1
_KAT_PBKDF2_DKLEN = 32
_KAT_PBKDF2_KEY_HEX = "55ac046e56e3089fec1691c22544b605f94185216dde0465e68b9d57c20dacbc"

# Byte size of the opaque wolfCrypt `Aes` context we hand to wc_AesGcmSetKey /
# wc_AesGcmEncrypt / wc_AesGcmDecrypt.
#
# This MUST be >= sizeof(Aes) in the loaded build, and the struct is bigger than
# it looks: a wolfSSL compiled with AES-NI (the normal case on x86-64) carries an
# aligned hardware key schedule, and builds with async/devcrypto support add more
# still. 512 was too small — wc_AesGcmSetKey wrote past the end of the buffer and
# corrupted the heap. Nothing failed at the call site: every KAT returned 0 and
# computed the correct known answer, and the process then died 0xC0000005 in an
# unrelated garbage collection at interpreter shutdown, long after the crypto
# was done. That made `m3 setup` report the embedder "SKIPPED (not installed)"
# when it had installed correctly (2026-07-28). Verified by bisection: with this
# one value at 512 the process faults; at 1024 it exits 0, all else identical.
#
# 4096 is deliberate over-allocation. The context is stack-scoped and freed
# immediately, so the cost is nothing, whereas guessing tight against an opaque
# struct we do not control is exactly what caused this. If a future wolfSSL grows
# past this, the failure mode is silent heap corruption again — so do not trim it.
_WC_AES_CTX_BYTES = 4096
# The same opaque-struct rule for wc_Sha256 (streaming SHA-256). This build
# exports no wc_Sha256New/Delete, so the caller supplies the storage; the
# streaming KAT below proves the binding works before any real data is hashed.
_WC_SHA256_CTX_BYTES = 4096
_WC_HMAC_CTX_BYTES = 4096  # same rule for the opaque Hmac context
# The hash-type value wolfCrypt's PBKDF2 and HMAC calls take for SHA-256.
# MEASURED against wolfSSL 5.9.2: 6 gives the RFC 7914 / RFC 4231 answers; 2
# returns BAD_FUNC_ARG (-173) from both wc_PBKDF2 and wc_HmacSetKey. A mock
# ignores this argument, so only a real wolfSSL build (and the KATs) catch it.
_WC_SHA256_HASH_TYPE = 6
_WC_INVALID_DEVID = -2

# HMAC-SHA-256 KAT: RFC 4231 test case 1 (a 20-byte key, above the FIPS
# minimum HMAC key length, unlike test case 2's 4-byte "Jefe").
_KAT_HMAC_KEY = bytes([0x0B] * 20)
_KAT_HMAC_DATA = b"Hi There"
_KAT_HMAC_SHA256_HEX = "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"

_KAT_AESGCM_KEY = bytes(range(32))  # 256-bit key 00..1f
_KAT_AESGCM_NONCE = bytes(range(12))  # 96-bit nonce 00..0b
_KAT_AESGCM_PLAINTEXT = b"FIPS-KAT-AESGCM!"  # 16 bytes
_KAT_AESGCM_CT_TAG_HEX = "014b8648e8ae834fa000d2d8f6aa354c6279c93ba1d84effc8a3bae200a581a8"


# ── Tiered FIPS mode (see docs/FIPS_MODULE_BOUNDARY.md) ──────────────────────
# Three tiers so homelab/dev users can use wolfCrypt's performance + the
# hardened algorithm boundary WITHOUT a commercial FIPS license:
#
#   (neither set)            -> DEFAULT backend (Python cryptography/hashlib).
#   M3_FIPS_MODE=1           -> require the wolfSSL backend; FAIL-CLOSED if
#                               wolfSSL is ABSENT; but ACCEPT the open-source
#                               (non-FIPS) wolfCrypt build. The CMVP FIPS
#                               symbols (POST/entropy callbacks) are OPTIONAL
#                               here — missing them is a warning, not fatal.
#   M3_FIPS_MODE=1 +
#   M3_FIPS_STRICT=1         -> additionally REQUIRE the CMVP-validated FIPS
#                               build: FIPS symbols mandatory, POST must pass.
#                               A non-FIPS lib is refused with a clear message.
#
# Rationale: the validated wolfCrypt-FIPS module is commercial + NDA-gated and
# not freely downloadable; the open-source wolfSSL build is freely buildable but
# lacks the FIPS symbols. STRICT is the gate for true FIPS 140-3; plain FIPS_MODE
# is "use wolfCrypt, hardened, fail-closed-if-absent" for everyone else.


# ── Secure wolfSSL discovery (DLL-hijack / search-order hardening) ───────────
# A bare `ctypes.CDLL("wolfssl.dll")` delegates to the OS loader search order,
# which on Windows includes the CWD and %PATH%, and on Linux honours the
# attacker-influenceable LD_LIBRARY_PATH. For a CRYPTO library that is a
# privilege-to-weaken vector: drop a malicious wolfssl.dll earlier in the search
# path and you replace the crypto module. We therefore NEVER load by bare name.
# Instead we resolve to an ABSOLUTE path from an explicit, trusted precedence
# list that M3 controls, and load exactly that file.
#
# Precedence (first existing file wins):
#   1. M3_WOLFSSL_LIB env var — an explicit absolute path the operator pins.
#   2. M3's own lib dir: <config-root-parent>/.m3/lib (default ~/.m3/lib).
#   3. Trusted SYSTEM install locations (NOT the CWD, NOT %PATH%):
#        Linux/macOS: /usr/local/lib, /usr/lib, /lib, /opt/homebrew/lib
#        Windows:     %SystemRoot%\System32  (admin-only writable)
# An operator may additionally pin the expected SHA-256 via M3_WOLFSSL_SHA256;
# a mismatch is fatal (catches an in-place swap even at a trusted path).
def _m3_lib_dir() -> str:
    """M3's own trusted lib directory (~/.m3/lib by default).

    Owned by ``m3_memory._roots.lib_root`` (stdlib-only, so the crypto layer stays
    dependency-light): M3_LIB_DIR, else M3_CONFIG_ROOT's parent, else
    M3_MEMORY_ROOT, else ~/.m3 — then /lib.

    M3_LIB_DIR pins the DIRECTORY only; the per-OS filename is still chosen by
    _trusted_wolfssl_candidates(). That distinction matters: M3_WOLFSSL_LIB
    pins a full path verbatim, so using it to relocate the search would inject
    a host-specific filename (e.g. a Windows .dll) and defeat platform
    resolution. Needed because the decoupled roots are relocatable — anything
    that repoints M3_CONFIG_ROOT (a test sandbox, a container, a per-user
    install) silently moves the trusted crypto search away from the real
    installed library, which is FAIL-CLOSED under M3_FIPS_MODE=1.
    """
    from m3_core.seams import roots

    return roots.lib_root()


def _trusted_wolfssl_candidates() -> "list[str]":
    """Ordered list of ABSOLUTE candidate paths for the wolfSSL library.

    Only locations M3 controls or that require elevated privileges to write —
    deliberately excludes the CWD and PATH/LD_LIBRARY_PATH entries.
    """
    # Per-OS library filename(s). macOS uses .dylib (what the build helper
    # produces and installs); we ALSO accept .so there since a Linux-style build
    # or a Homebrew formula may produce it. Windows: wolfssl.dll.
    lib_names: tuple[str, ...]
    if is_windows():
        lib_names = ("wolfssl.dll",)
    elif is_macos():
        lib_names = ("libwolfssl.dylib", "libwolfssl.so")
    else:
        lib_names = ("libwolfssl.so",)

    candidates: list[str] = []
    # 1. Explicit operator pin (absolute path) — used verbatim, any filename.
    pinned = _crypto_text("crypto.wolfssl_lib")
    if pinned:
        candidates.append(os.path.abspath(os.path.expanduser(pinned)))
    # 2. M3's own lib dir — try every per-OS filename.
    m3lib = _m3_lib_dir()
    for nm in lib_names:
        candidates.append(os.path.join(m3lib, nm))
    # 3. Trusted system locations (admin/root-only writable).
    if is_windows():
        sysroot = os.environ.get("SystemRoot", r"C:\Windows")
        candidates.append(os.path.join(sysroot, "System32", "wolfssl.dll"))
    else:
        for d in ("/usr/local/lib", "/usr/lib", "/lib", "/opt/homebrew/lib"):
            for nm in lib_names:
                candidates.append(os.path.join(d, nm))
    return candidates


def _verify_lib_integrity(path: str) -> None:
    """If crypto.wolfssl_sha256 (M3_WOLFSSL_SHA256) is set, the loaded file's SHA-256 must match it.

    Self-pin / trust-on-first-use: M3 doesn't bundle wolfSSL, so the user BUILDS
    their own and pins the hash of THAT trusted build (there is no canonical
    vendor hash to compare against). This then detects any later tampering or
    in-place swap. Raises RuntimeError on mismatch. No-op when unset."""
    expected = _crypto_text("crypto.wolfssl_sha256").lower()
    if not expected:
        return
    # hashlib, not wolfCrypt: this checks the library BEFORE it is loaded, so
    # the library cannot vouch for itself.
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"FATAL: wolfSSL library integrity check failed; refusing to load. "
            f"observed: file {path} has SHA-256 {actual[:16]}…. "
            f"cause: crypto.wolfssl_sha256 (M3_WOLFSSL_SHA256)={expected[:16]}… (pinned hash) does not match — possible library was swapped or corrupted on disk. "
            f"inspect: re-verify the library binary (e.g., rebuild or restore from trusted source), then update crypto.wolfssl_sha256 (M3_WOLFSSL_SHA256) to the correct hash."
        )


def _resolve_wolfssl_path() -> "str | None":
    """Return the FIRST trusted candidate that exists on disk (absolute path),
    after verifying any integrity pin. None if none found."""
    for cand in _trusted_wolfssl_candidates():
        if os.path.isfile(cand):
            _verify_lib_integrity(cand)
            return cand
    return None


# Backend Selection
# DEFAULT: standard hashlib/cryptography
# WOLFSSL: wolfCrypt (FIPS-Ready)
BACKEND = (_crypto_text("crypto.backend") or "DEFAULT").upper()
if _fips_active():
    BACKEND = "WOLFSSL"


class _WolfSha256Stream:
    """Incremental SHA-256 on wolfCrypt, with hashlib's update()/hexdigest()
    shape. hexdigest() finalizes once and frees the context; a stream dropped
    unfinished is freed by its finalizer."""

    def __init__(self, lib) -> None:
        self._lib = lib
        self._ctx = ctypes.create_string_buffer(_WC_SHA256_CTX_BYTES)
        ret = lib.wc_InitSha256(self._ctx)
        if ret != 0:
            raise RuntimeError(f"wc_InitSha256 failed with code: {ret}")
        self._digest: str | None = None
        self._free = weakref.finalize(self, lib.wc_Sha256Free, self._ctx)

    def update(self, data: bytes) -> None:
        if self._digest is not None:
            raise RuntimeError("SHA-256 stream already finalized")
        ret = self._lib.wc_Sha256Update(self._ctx, data, len(data))
        if ret != 0:
            raise RuntimeError(f"wc_Sha256Update failed with code: {ret}")

    def hexdigest(self) -> str:
        if self._digest is None:
            out = ctypes.create_string_buffer(32)
            ret = self._lib.wc_Sha256Final(self._ctx, out)
            self._free()
            if ret != 0:
                raise RuntimeError(f"wc_Sha256Final failed with code: {ret}")
            self._digest = out.raw.hex()
        return self._digest


class CryptoProvider:
    # One-shot latch for the open-source-build notice. CLASS level, not instance:
    # several providers may be constructed in one process (pool workers, a
    # re-exec'd CLI, the bridge plus a sweeper), and the fact being reported is a
    # property of the LOADED LIBRARY, not of any one instance — so it is worth
    # saying once per process, never once per object.
    _non_fips_notice_emitted = False
    _tls_fallback_noted = False

    def __init__(self, backend="DEFAULT"):
        self.backend = backend
        self._initialized = False
        self._fips_validated = False  # True only when the CMVP FIPS module is active
        self._libwolf: ctypes.CDLL | None = None
        self._lib_path: str | None = None  # absolute path the lib was loaded from
        self._fips_cb_ref = None  # Keep reference to prevent GC
        self._wolf_rng = None  # wolfCrypt DRBG instance (WC_RNG*), when the backend is live
        self._rng_lock = threading.Lock()

        if self.backend == "WOLFSSL":
            self._initialize_wolfssl()

        # Fail-closed lockouts.
        # (a) Any FIPS tier requires the wolfSSL backend to be live — if wolfSSL
        #     is absent/failed, never silently fall back to Python crypto.
        if _fips_active() and (not self._initialized or self.backend != "WOLFSSL"):
            raise RuntimeError(
                "FATAL: FIPS mode enabled (crypto.fips_mode / crypto.fips_strict, env M3_FIPS_MODE / M3_FIPS_STRICT) but the "
                "wolfSSL/wolfCrypt backend failed to initialize. Terminating to "
                "prevent unsafe cryptographic fallback. Install wolfSSL (see "
                "docs/FIPS_MODULE_BOUNDARY.md), or unset the FIPS env vars. "
                "observed: FIPS mode is set and the wolfSSL backend did not initialize. "
                "inspect: crypto.wolfssl_lib (M3_WOLFSSL_LIB), crypto.fips_mode / crypto.fips_strict (M3_FIPS_MODE / M3_FIPS_STRICT)"
            )
        # (b) STRICT additionally requires the CMVP-VALIDATED module — refuse a
        #     non-FIPS (open-source) wolfSSL with a clear, actionable message.
        if _fips_strict() and not self._fips_validated:
            raise RuntimeError(
                "FATAL: crypto.fips_strict requires the CMVP-validated wolfCrypt "
                "FIPS module, but the loaded wolfSSL library lacks the FIPS "
                "symbols (POST/entropy) — you have the OPEN-SOURCE build, not the "
                "validated FIPS module. Use the commercial wolfSSL FIPS build for "
                "strict compliance, or use crypto.fips_mode alone (without crypto.fips_strict) to run "
                "on open-source wolfCrypt. See docs/FIPS_MODULE_BOUNDARY.md. "
                "observed: the loaded library has no FIPS POST/entropy symbols. "
                "inspect: crypto.wolfssl_lib names the validated FIPS build, or crypto.fips_strict"
            )

    def _initialize_wolfssl(self):
        """Foundational FIPS 140-3 initialization sequence."""
        try:
            # SECURE LOAD: resolve to a trusted ABSOLUTE path (M3-controlled or
            # admin-only locations) and load exactly that file. We never pass a
            # bare "wolfssl.dll"/"libwolfssl.so" to ctypes.CDLL, because that
            # delegates to the OS loader search order (Windows: app dir + CWD +
            # %PATH%; Linux: LD_LIBRARY_PATH + runpath) — a DLL-hijack vector
            # where an attacker drops a weaker/backdoored crypto lib earlier in
            # the path. See _resolve_wolfssl_path / _trusted_wolfssl_candidates.
            self._lib_path = _resolve_wolfssl_path()
            if self._lib_path is None:
                if _fips_active():
                    # Name the paths actually searched: m3's lib dir follows the
                    # config root, so a fixed "~/.m3/lib" here sent people to a
                    # folder the loader never looked in.
                    m3lib = _m3_lib_dir()
                    default_lib = os.path.abspath(os.path.expanduser(os.path.join("~", ".m3", "lib")))
                    moved = ""
                    if os.path.normcase(m3lib) != os.path.normcase(default_lib):
                        moved = (
                            f" m3's lib folder is {m3lib}, not {default_lib}, because M3_LIB_DIR or the "
                            "config root points elsewhere; set M3_LIB_DIR to the folder that holds wolfSSL."
                        )
                    raise RuntimeError(
                        "FATAL: FIPS mode enabled but no wolfSSL library was found "
                        "in any TRUSTED location. M3 refuses to search the CWD/PATH for a "
                        "crypto library. observed: searched "
                        + ", ".join(_trusted_wolfssl_candidates())
                        + "; none exists."
                        + moved
                        + f" fix: install wolfSSL in {m3lib} or pin its full path with crypto.wolfssl_lib "
                        "(M3_WOLFSSL_LIB), or unset crypto.fips_mode / crypto.fips_strict "
                        "(M3_FIPS_MODE / M3_FIPS_STRICT) (see docs/FIPS_MODULE_BOUNDARY.md)."
                    )
                logger.info("M3 Crypto: no wolfSSL in a trusted path; using DEFAULT backend.")
                self.backend = "DEFAULT"
                return
            try:
                # winmode=0 on Windows prevents the default flags from re-adding
                # the application/PATH search directories around the absolute path.
                if is_windows():
                    self._libwolf = ctypes.CDLL(self._lib_path, winmode=0)
                else:
                    self._libwolf = ctypes.CDLL(self._lib_path)
                logger.info(f"M3 Crypto: loaded wolfSSL from trusted path {self._lib_path}")
                # Initialise wolfCrypt before any service. The hash, cipher and KDF
                # calls happen to work without it; the RNG does not -- wc_rng_new
                # crashed with an access violation on an uninitialised library
                # (measured 2026-10-15). Idempotent (reference-counted) in wolfCrypt.
                if hasattr(self._libwolf, "wolfCrypt_Init"):
                    self._libwolf.wolfCrypt_Init.restype = ctypes.c_int
                    init_rc = self._libwolf.wolfCrypt_Init()
                    if init_rc != 0:
                        raise OSError(f"wolfCrypt_Init failed with code {init_rc}")
            except OSError as e:
                if _fips_active():
                    raise RuntimeError(
                        f"FATAL: FIPS mode enabled but wolfSSL library cannot be loaded. "
                        f"observed: OSError loading {self._lib_path}: {e}. "
                        f"possible: file permissions, corrupted library, or missing dependencies (e.g., ld-linux, MSVCRT). "
                        f"inspect: check file permissions (`ls -l {self._lib_path}`), library dependencies (`ldd {self._lib_path}` on Linux), or reinstall wolfSSL."
                    ) from e
                logger.info(f"M3 Crypto: wolfSSL at {self._lib_path} failed to load ({e}); using DEFAULT.")
                self.backend = "DEFAULT"
                return

            # Setup ctypes function signatures if library loaded successfully
            if hasattr(self._libwolf, "wc_AesGcmSetKey"):
                self._libwolf.wc_AesGcmSetKey.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint]
                self._libwolf.wc_AesGcmSetKey.restype = ctypes.c_int
            elif _fips_active():
                raise RuntimeError(
                    "FATAL: wc_AesGcmSetKey symbol not found in loaded wolfSSL library. "
                    "observed: the loaded library does not export wc_AesGcmSetKey. "
                    "inspect: crypto.wolfssl_lib (M3_WOLFSSL_LIB) names a wolfSSL build with AES-GCM enabled (docs/FIPS_MODULE_BOUNDARY.md)"
                )

            if hasattr(self._libwolf, "wc_AesGcmEncrypt"):
                self._libwolf.wc_AesGcmEncrypt.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_char_p,
                    ctypes.c_char_p,
                    ctypes.c_uint,
                    ctypes.c_char_p,
                    ctypes.c_uint,
                    ctypes.c_char_p,
                    ctypes.c_uint,
                    ctypes.c_char_p,
                    ctypes.c_uint,
                ]
                self._libwolf.wc_AesGcmEncrypt.restype = ctypes.c_int
            elif _fips_active():
                raise RuntimeError(
                    "FATAL: wc_AesGcmEncrypt symbol not found in loaded wolfSSL library. "
                    "observed: the loaded library does not export wc_AesGcmEncrypt. "
                    "inspect: crypto.wolfssl_lib (M3_WOLFSSL_LIB) names a wolfSSL build with AES-GCM enabled (docs/FIPS_MODULE_BOUNDARY.md)"
                )

            if hasattr(self._libwolf, "wc_AesGcmDecrypt"):
                self._libwolf.wc_AesGcmDecrypt.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_char_p,
                    ctypes.c_char_p,
                    ctypes.c_uint,
                    ctypes.c_char_p,
                    ctypes.c_uint,
                    ctypes.c_char_p,
                    ctypes.c_uint,
                    ctypes.c_char_p,
                    ctypes.c_uint,
                ]
                self._libwolf.wc_AesGcmDecrypt.restype = ctypes.c_int
            elif _fips_active():
                raise RuntimeError(
                    "FATAL: wc_AesGcmDecrypt symbol not found in loaded wolfSSL library. "
                    "observed: the loaded library does not export wc_AesGcmDecrypt. "
                    "inspect: crypto.wolfssl_lib (M3_WOLFSSL_LIB) names a wolfSSL build with AES-GCM enabled (docs/FIPS_MODULE_BOUNDARY.md)"
                )

            if hasattr(self._libwolf, "wc_Sha256Hash"):
                self._libwolf.wc_Sha256Hash.argtypes = [ctypes.c_char_p, ctypes.c_uint, ctypes.c_char_p]
                self._libwolf.wc_Sha256Hash.restype = ctypes.c_int
            elif _fips_active():
                raise RuntimeError(
                    "FATAL: wc_Sha256Hash symbol not found in loaded wolfSSL library. "
                    "observed: the loaded library does not export wc_Sha256Hash. "
                    "inspect: crypto.wolfssl_lib (M3_WOLFSSL_LIB) names a wolfSSL build with SHA-256 enabled (docs/FIPS_MODULE_BOUNDARY.md)"
                )

            if hasattr(self._libwolf, "wc_PBKDF2"):
                # wc_PBKDF2(out, passwd, pLen, salt, sLen, iterations, kLen, typeH)
                self._libwolf.wc_PBKDF2.argtypes = [
                    ctypes.c_char_p,
                    ctypes.c_char_p,
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                ]
                self._libwolf.wc_PBKDF2.restype = ctypes.c_int
            elif _fips_active():
                raise RuntimeError(
                    "FATAL: wc_PBKDF2 symbol not found in loaded wolfSSL library. "
                    "observed: the loaded library does not export wc_PBKDF2. "
                    "inspect: crypto.wolfssl_lib (M3_WOLFSSL_LIB) names a wolfSSL build with PBKDF2 enabled (docs/FIPS_MODULE_BOUNDARY.md)"
                )

            # Random bytes (keys, IVs, salts, tokens) come from wolfCrypt's DRBG,
            # so random generation sits inside the same module as the rest. One
            # RNG instance for the process; wolfCrypt's RNG is not thread-safe,
            # so random_bytes() serialises access.
            if all(hasattr(self._libwolf, s) for s in ("wc_rng_new", "wc_RNG_GenerateBlock", "wc_rng_free")):
                self._libwolf.wc_rng_new.argtypes = [ctypes.c_char_p, ctypes.c_uint, ctypes.c_void_p]
                self._libwolf.wc_rng_new.restype = ctypes.c_void_p
                self._libwolf.wc_RNG_GenerateBlock.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint]
                self._libwolf.wc_RNG_GenerateBlock.restype = ctypes.c_int
                self._libwolf.wc_rng_free.argtypes = [ctypes.c_void_p]
                self._libwolf.wc_rng_free.restype = None
                self._wolf_rng = self._libwolf.wc_rng_new(None, 0, None)
                if not self._wolf_rng and _fips_active():
                    raise RuntimeError(
                        "FATAL: wc_rng_new returned NULL. observed: wolfCrypt could not instantiate its DRBG. "
                        "inspect: the loaded wolfSSL build and the OS entropy source (docs/FIPS_MODULE_BOUNDARY.md)"
                    )
            elif _fips_active():
                raise RuntimeError(
                    "FATAL: wolfCrypt RNG symbols (wc_rng_new, wc_RNG_GenerateBlock) not found in loaded wolfSSL "
                    "library. inspect: crypto.wolfssl_lib (M3_WOLFSSL_LIB) names a wolfSSL build with the RNG enabled "
                    "(docs/FIPS_MODULE_BOUNDARY.md)"
                )

            # 1. Register FIPS Status Callback
            # wolfCrypt_SetCb_fips(myFipsCb)
            FIPS_CB_TYPE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_char_p)

            def myFipsCb(status, msg):
                msg_str = msg.decode("utf-8") if msg else "No message"
                err_desc = WOLF_ERROR_CODES.get(status, "Status OK" if status == 0 else "Unknown Status")
                logger.info(f"FIPS Status: {status} ({err_desc}) - {msg_str}")
                return 0

            self._fips_cb_ref = FIPS_CB_TYPE(myFipsCb)
            if hasattr(self._libwolf, "wolfCrypt_SetCb_fips"):
                self._libwolf.wolfCrypt_SetCb_fips(self._fips_cb_ref)

            # 2. Register Entropy Seed Callback (CMVP FIPS module only).
            # wc_SetSeed_Cb(wc_GenerateSeed)
            has_entropy_cb = hasattr(self._libwolf, "wc_SetSeed_Cb") and hasattr(self._libwolf, "wc_GenerateSeed")
            if has_entropy_cb:
                self._libwolf.wc_SetSeed_Cb(self._libwolf.wc_GenerateSeed)
                logger.info("M3 Crypto: Entropy seed callback registered.")

            # 3. Verify POST (Pre-Operational Self-Tests) — CMVP FIPS module only.
            # wolfCrypt_GetStatus_fips() returns 0 for success. The open-source
            # wolfSSL build lacks this symbol entirely (that's what makes it
            # "non-FIPS"); its absence is NOT fatal under plain M3_FIPS_MODE, but
            # IS the thing M3_FIPS_STRICT requires (enforced in __init__ via the
            # _fips_validated flag set below).
            has_post = hasattr(self._libwolf, "wolfCrypt_GetStatus_fips")
            if has_post:
                status = self._libwolf.wolfCrypt_GetStatus_fips()
                if status != 0:
                    error_msg = WOLF_ERROR_CODES.get(status, f"Error code {status}")
                    logger.error(f"M3 Crypto: wolfSSL FIPS POST failed: {error_msg}")
                    raise RuntimeError(f"FIPS POST failed with status {status}")

            # The loaded library is the CMVP-VALIDATED FIPS module only when it
            # exposes the FIPS service symbols AND its POST passed. This flag
            # gates M3_FIPS_STRICT in __init__.
            self._fips_validated = has_post and has_entropy_cb
            if not self._fips_validated:
                # SEVERITY BY ACTIONABILITY (§3: a warning that fires when nothing
                # is wrong trains you to ignore the one that matters).
                #
                # Under M3_FIPS_STRICT the open-source build IS a real problem —
                # strict REQUIRES the validated module, and __init__ refuses to
                # start on it. Keep that loud.
                #
                # Under plain M3_FIPS_MODE it is the DOCUMENTED, EXPECTED state:
                # hardened wolfCrypt is active and the open-source build is what
                # the installer ships (see install_wolfssl.py: "works with
                # M3_FIPS_MODE=1"). Warning on it announces a permanent, correct
                # configuration on EVERY process start — measured 2026-09-09:
                # 1,714 copies in sync_all.log alone, plus one on every CLI
                # invocation, drowning the lines that do need reading.
                #
                # So: INFO, and only ONCE per process. The fact stays available
                # (M3_DEBUG / -v, and `m3 doctor` reports the crypto tier in
                # full) without being repeated to someone who cannot act on it.
                if _fips_strict():
                    logger.warning(
                        f"crypto.fips_strict is on but wolfSSL is the open-source build (not CMVP-validated). "
                        f"observed: loaded library at {self._lib_path} lacks POST/entropy FIPS service symbols. "
                        f"cause: crypto.fips_strict requires the commercial wolfSSL FIPS module but the open-source build was installed instead. "
                        f"inspect: (1) verify which wolfSSL version is at {self._lib_path}; (2) purchase/obtain the CMVP-validated module; (3) rebuild/reinstall, or `m3 config unset crypto.fips_strict` to use open-source mode."
                    )
                elif _fips_mode() and not CryptoProvider._non_fips_notice_emitted:
                    CryptoProvider._non_fips_notice_emitted = True
                    logger.info(
                        "M3 Crypto: hardened wolfCrypt active (open-source build; "
                        "not the CMVP-validated FIPS module — expected under "
                        "M3_FIPS_MODE)."
                    )

            # 4. Application-level power-up Known-Answer-Tests. Proves THIS
            #    process's bindings compute the documented answers before any
            #    real crypto is served. Runs on BOTH the open-source and FIPS
            #    builds (the vectors are correct for any conforming wolfCrypt).
            #    A KAT failure is fatal under any FIPS tier (re-raised below).
            self._run_self_tests()

            self._initialized = True
            _tier = "CMVP-validated FIPS" if self._fips_validated else "open-source (non-FIPS)"
            logger.info(f"M3 Crypto: wolfSSL backend initialized ({_tier} wolfCrypt).")

        except Exception as e:
            if _fips_active():
                raise RuntimeError(
                    f"FATAL: FIPS mode is on (crypto.fips_mode / crypto.fips_strict), and the "
                    f"wolfSSL/wolfCrypt backend it requires failed to initialize: {e}. "
                    f"observed: {type(e).__name__} while loading the backend. "
                    f"inspect: crypto.wolfssl_lib (M3_WOLFSSL_LIB), crypto.fips_mode / crypto.fips_strict (M3_FIPS_MODE / M3_FIPS_STRICT) (docs/FIPS_MODULE_BOUNDARY.md)"
                ) from e
            logger.warning(f"M3 Crypto: wolfSSL FIPS initialization failed ({e}). Falling back to DEFAULT.")
            self.backend = "DEFAULT"
            self._initialized = False

    def _wolf_aes_gcm_encrypt_fixed_nonce(self, data: bytes, key: bytes, iv: bytes) -> bytes:
        """AES-256-GCM encrypt with a CALLER-SUPPLIED nonce — used ONLY by the
        power-up KAT so the ciphertext+tag is a deterministic known answer. The
        production encrypt path generates a fresh random nonce; nonce reuse is
        unsafe and this helper must never be used for real data."""
        if not self._libwolf:
            raise RuntimeError("wolfSSL library not loaded")
        out_buf = ctypes.create_string_buffer(len(data))
        tag_buf = ctypes.create_string_buffer(16)
        aes = ctypes.create_string_buffer(_WC_AES_CTX_BYTES)
        if self._libwolf.wc_AesGcmSetKey(aes, key, len(key)) != 0:
            raise RuntimeError("wc_AesGcmSetKey failed in KAT")
        ret = self._libwolf.wc_AesGcmEncrypt(aes, out_buf, data, len(data), iv, len(iv), tag_buf, 16, None, 0)
        if ret != 0:
            raise RuntimeError(f"wc_AesGcmEncrypt failed in KAT: {ret}")
        return iv + out_buf.raw + tag_buf.raw

    def _run_self_tests(self) -> None:
        """FIPS 140-3 power-up Known-Answer-Tests against the wolfCrypt path.

        Proves SHA-256, PBKDF2-SHA256, and AES-256-GCM each compute the
        documented answer before any real crypto is served. Raises on any
        mismatch — the caller (init) treats that as a fatal FIPS error. This is
        the application-level complement to wolfCrypt's own POST
        (wolfCrypt_GetStatus_fips): POST attests the module; these KATs attest
        that THIS process's bindings invoke it correctly.
        """
        # SHA-256 KAT
        got = self._wolf_sha256(_KAT_SHA256_INPUT)
        if got != _KAT_SHA256_DIGEST:
            raise RuntimeError(f"SHA-256 KAT failed: got {got}")

        # Streaming SHA-256 KAT: the same vector fed in two pieces, so a wrong
        # context size or a broken Update/Final binding cannot pass.
        if hasattr(self._libwolf, "wc_InitSha256"):
            s = _WolfSha256Stream(self._libwolf)
            s.update(_KAT_SHA256_INPUT[:1])
            s.update(_KAT_SHA256_INPUT[1:])
            got = s.hexdigest()
            if got != _KAT_SHA256_DIGEST:
                raise RuntimeError(f"streaming SHA-256 KAT failed: got {got}")

        # HMAC-SHA-256 KAT (only if the binding is present)
        if hasattr(self._libwolf, "wc_HmacSetKey"):
            got = self._wolf_hmac_sha256(_KAT_HMAC_KEY, _KAT_HMAC_DATA)
            if got != _KAT_HMAC_SHA256_HEX:
                raise RuntimeError(f"HMAC-SHA-256 KAT failed: got {got}")

        # PBKDF2-SHA256 KAT (only if the binding is present)
        if hasattr(self._libwolf, "wc_PBKDF2"):
            key = self._wolf_pbkdf2_sha256(
                _KAT_PBKDF2_PASSWORD,
                _KAT_PBKDF2_SALT,
                _KAT_PBKDF2_ITERATIONS,
                _KAT_PBKDF2_DKLEN,
            )
            if key.hex() != _KAT_PBKDF2_KEY_HEX:
                raise RuntimeError(f"PBKDF2-SHA256 KAT failed: got {key.hex()}")

        # AES-256-GCM KAT — known-answer (fixed nonce) + authenticated round-trip.
        token = self._wolf_aes_gcm_encrypt_fixed_nonce(_KAT_AESGCM_PLAINTEXT, _KAT_AESGCM_KEY, _KAT_AESGCM_NONCE)
        ct_tag = token[len(_KAT_AESGCM_NONCE) :]  # strip the prepended IV
        if ct_tag.hex() != _KAT_AESGCM_CT_TAG_HEX:
            raise RuntimeError(f"AES-256-GCM encrypt KAT failed: got {ct_tag.hex()}")
        # Decrypt must recover the plaintext (exercises GCM tag verification).
        back = self._wolf_aes_gcm_decrypt(token, _KAT_AESGCM_KEY)
        if back != _KAT_AESGCM_PLAINTEXT:
            raise RuntimeError("AES-256-GCM decrypt KAT failed: plaintext mismatch")

        # DRBG sanity (the validated module runs its own SP 800-90A health tests at
        # POST): two draws must have the requested length and differ.
        if self._wolf_rng:
            r1, r2 = self.random_bytes(32), self.random_bytes(32)
            if len(r1) != 32 or r1 == r2:
                raise RuntimeError("wolfCrypt DRBG self-test failed: repeated or short output")
        logger.info("M3 Crypto: FIPS power-up KATs passed (SHA-256, PBKDF2, AES-256-GCM, DRBG).")

    def _wolf_sha256(self, data: bytes) -> str:
        if not self._libwolf:
            raise RuntimeError("wolfSSL library not loaded")
        hash_buf = ctypes.create_string_buffer(32)
        ret = self._libwolf.wc_Sha256Hash(data, len(data), hash_buf)
        if ret != 0:
            raise RuntimeError(f"wc_Sha256Hash failed with code: {ret}")
        return hash_buf.raw.hex()

    def _wolf_aes_gcm_encrypt(self, data: bytes, key: bytes) -> bytes:
        if not self._libwolf:
            raise RuntimeError("wolfSSL library not loaded")

        iv = self.random_bytes(12)
        out_buf = ctypes.create_string_buffer(len(data))
        tag_buf = ctypes.create_string_buffer(16)

        aes = ctypes.create_string_buffer(_WC_AES_CTX_BYTES)
        ret_key = self._libwolf.wc_AesGcmSetKey(aes, key, len(key))
        if ret_key != 0:
            raise RuntimeError(f"wc_AesGcmSetKey failed with code: {ret_key}")

        ret_enc = self._libwolf.wc_AesGcmEncrypt(aes, out_buf, data, len(data), iv, len(iv), tag_buf, 16, None, 0)
        if ret_enc != 0:
            raise RuntimeError(f"wc_AesGcmEncrypt failed with code: {ret_enc}")

        return iv + out_buf.raw + tag_buf.raw

    def _wolf_aes_gcm_decrypt(self, token: bytes, key: bytes) -> bytes:
        if not self._libwolf:
            raise RuntimeError("wolfSSL library not loaded")

        if len(token) < 28:
            raise ValueError("Token too short for AES-GCM")

        iv = token[:12]
        tag = token[-16:]
        ciphertext = token[12:-16]

        out_buf = ctypes.create_string_buffer(len(ciphertext))

        aes = ctypes.create_string_buffer(_WC_AES_CTX_BYTES)
        ret_key = self._libwolf.wc_AesGcmSetKey(aes, key, len(key))
        if ret_key != 0:
            raise RuntimeError(f"wc_AesGcmSetKey failed with code: {ret_key}")

        ret_dec = self._libwolf.wc_AesGcmDecrypt(
            aes, out_buf, ciphertext, len(ciphertext), iv, len(iv), tag, 16, None, 0
        )
        if ret_dec != 0:
            raise RuntimeError(f"wc_AesGcmDecrypt failed with code: {ret_dec}")

        return out_buf.raw

    def pbkdf2_sha256(self, password: bytes, salt: bytes, iterations: int, length: int = 32) -> bytes:
        """Derive a key via PBKDF2-HMAC-SHA256.

        Routes key derivation through the FIPS boundary: under M3_FIPS_MODE the
        wolfCrypt path is mandatory and any miss/failure is fatal (never a silent
        fallback to the stdlib KDF). The DEFAULT backend uses the `cryptography`
        library's PBKDF2HMAC, which is byte-identical output for the same
        (password, salt, iterations, length) — so existing derived keys/secrets
        decrypt unchanged when not in FIPS mode.
        """
        self._require_fips_backend()

        if self.backend == "WOLFSSL" and self._initialized and self._libwolf:
            try:
                return self._wolf_pbkdf2_sha256(password, salt, iterations, length)
            except Exception as e:
                if _fips_active():
                    raise RuntimeError(
                        f"FIPS PBKDF2 derivation failed: {e}. Raising to prevent unsafe fallback."
                    ) from e
                logger.debug(f"M3 Crypto: wolfSSL PBKDF2 failed ({e}), using default fallback.")

        # Default Fallback: PBKDF2-HMAC-SHA256 via cryptography
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=length, salt=salt, iterations=iterations)
        return kdf.derive(password)

    def _wolf_pbkdf2_sha256(self, password: bytes, salt: bytes, iterations: int, length: int) -> bytes:
        """wolfCrypt PBKDF2-HMAC-SHA256 via wc_PBKDF2.

        Signature: int wc_PBKDF2(byte* out, const byte* passwd, int pLen,
                                 const byte* salt, int sLen, int iterations,
                                 int kLen, int typeH). typeH for SHA-256 is the
                                 wolfCrypt enum WC_SHA256 (== 2).
        """
        if not self._libwolf or not hasattr(self._libwolf, "wc_PBKDF2"):
            raise RuntimeError("wc_PBKDF2 not available in loaded wolfSSL library")
        # hashType: see _WC_SHA256_HASH_TYPE (measured; 2 is BAD_FUNC_ARG).
        out_buf = ctypes.create_string_buffer(length)
        ret = self._libwolf.wc_PBKDF2(
            out_buf,
            password,
            len(password),
            salt,
            len(salt),
            iterations,
            length,
            _WC_SHA256_HASH_TYPE,
        )
        if ret != 0:
            raise RuntimeError(f"wc_PBKDF2 failed with code: {ret}")
        return out_buf.raw[:length]

    def random_bytes(self, n: int) -> bytes:
        """``n`` cryptographically secure random bytes: keys, IVs, salts, tokens.

        From wolfCrypt's DRBG when that backend is live, so random generation is
        inside the same module as every other service; otherwise the OS CSPRNG
        (``os.urandom``). Under a FIPS tier the backend is required (the
        constructor fails closed), so this never quietly uses the OS source there.
        """
        if n < 0:
            raise ValueError(f"random_bytes: n must be >= 0; got {n}")
        # Keyed on what the draw needs (a loaded library with a DRBG), not on
        # _initialized: the power-up self-test draws before that flag is set.
        if self._libwolf is not None and self._wolf_rng:
            buf = ctypes.create_string_buffer(n)
            with self._rng_lock:
                ret = self._libwolf.wc_RNG_GenerateBlock(self._wolf_rng, buf, n)
            if ret != 0:
                raise RuntimeError(f"wc_RNG_GenerateBlock failed with code: {ret}")
            return buf.raw
        self._require_fips_backend()
        return os.urandom(n)

    def token_hex(self, nbytes: int = 32) -> str:
        """A random token of ``nbytes`` bytes as hex (``secrets.token_hex``'s shape)."""
        return self.random_bytes(nbytes).hex()

    def token_urlsafe(self, nbytes: int = 32) -> str:
        """A random URL-safe token of ``nbytes`` bytes (``secrets.token_urlsafe``'s shape)."""
        import base64

        return base64.urlsafe_b64encode(self.random_bytes(nbytes)).rstrip(b"=").decode("ascii")

    def _require_fips_backend(self) -> None:
        """Under crypto.fips_mode / crypto.fips_strict (M3_FIPS_MODE / M3_FIPS_STRICT) every service must run on an
        initialized wolfCrypt; there is no fallback to the default backend."""
        # getattr: an error path must not itself fail on a partly built provider.
        backend = getattr(self, "backend", None)
        initialized = getattr(self, "_initialized", False)
        if _fips_active() and (not initialized or backend != "WOLFSSL"):
            raise RuntimeError(
                "FIPS boundary violation: FIPS mode is set but wolfCrypt is not serving; "
                f"observed: backend={backend}, initialized={initialized}. "
                "inspect: `m3 doctor` (crypto) and the wolfSSL library at "
                f"{getattr(self, '_lib_path', None)}"
            )

    def sha256(self, data: bytes) -> str:
        """Returns the SHA-256 hex digest of the data."""
        self._require_fips_backend()

        if self.backend == "WOLFSSL" and self._initialized and self._libwolf:
            try:
                return self._wolf_sha256(data)
            except Exception as e:
                if _fips_active():
                    raise RuntimeError(f"FIPS SHA-256 Hashing Failed: {e}. Raising to prevent unsafe fallback.") from e
                logger.debug(f"M3 Crypto: wolfSSL SHA-256 hashing failed ({e}), using default fallback.")

        # Default Fallback
        return hashlib.sha256(data).hexdigest()

    def sha256_stream(self):
        """An incremental SHA-256 (update(bytes) / hexdigest()) for data too
        large to hold at once, on the same backend and with the same FIPS rules
        as sha256()."""
        self._require_fips_backend()
        if _fips_active() and not hasattr(self._libwolf, "wc_InitSha256"):
            raise RuntimeError(
                "FIPS boundary: this wolfSSL build has no streaming SHA-256 "
                "(wc_InitSha256); observed: the export is missing. "
                f"inspect: the library at {self._lib_path}"
            )
        if (
            self.backend == "WOLFSSL"
            and self._initialized
            and self._libwolf
            and hasattr(self._libwolf, "wc_InitSha256")
        ):
            try:
                return _WolfSha256Stream(self._libwolf)
            except Exception as e:
                if _fips_active():
                    raise RuntimeError(
                        f"FIPS SHA-256 stream init failed: {e}. Raising to prevent unsafe fallback."
                    ) from e
                logger.debug(f"M3 Crypto: wolfSSL SHA-256 stream failed ({e}), using default fallback.")
        return hashlib.sha256()

    def _wolf_hmac_sha256(self, key: bytes, data: bytes) -> str:
        if not self._libwolf:
            raise RuntimeError("wolfSSL library not loaded")
        ctx = ctypes.create_string_buffer(_WC_HMAC_CTX_BYTES)
        ret = self._libwolf.wc_HmacInit(ctx, None, _WC_INVALID_DEVID)
        if ret != 0:
            raise RuntimeError(f"wc_HmacInit failed with code: {ret}")
        try:
            for step, ret in (
                ("wc_HmacSetKey", self._libwolf.wc_HmacSetKey(ctx, _WC_SHA256_HASH_TYPE, key, len(key))),
                ("wc_HmacUpdate", self._libwolf.wc_HmacUpdate(ctx, data, len(data))),
            ):
                if ret != 0:
                    raise RuntimeError(
                        f"{step} failed with code: {ret} ({WOLF_ERROR_CODES.get(ret, 'see wolfSSL error codes')})"
                    )
            out = ctypes.create_string_buffer(32)
            ret = self._libwolf.wc_HmacFinal(ctx, out)
            if ret != 0:
                raise RuntimeError(f"wc_HmacFinal failed with code: {ret}")
            return out.raw.hex()
        finally:
            self._libwolf.wc_HmacFree(ctx)

    def hmac_sha256(self, key: bytes, data: bytes) -> str:
        """HMAC-SHA-256 hex digest, on the same backend and with the same FIPS
        rules as sha256()."""
        self._require_fips_backend()
        if (
            self.backend == "WOLFSSL"
            and self._initialized
            and self._libwolf
            and hasattr(self._libwolf, "wc_HmacSetKey")
        ):
            try:
                return self._wolf_hmac_sha256(key, data)
            except Exception as e:
                if _fips_active():
                    raise RuntimeError(f"FIPS HMAC-SHA-256 failed: {e}. Raising to prevent unsafe fallback.") from e
                logger.debug(f"M3 Crypto: wolfSSL HMAC-SHA-256 failed ({e}), using default fallback.")
        import hmac

        return hmac.new(key, data, hashlib.sha256).hexdigest()

    def encrypt(self, data: bytes, key: bytes) -> bytes:
        """Encrypts data using AES-256-GCM."""
        self._require_fips_backend()

        if self.backend == "WOLFSSL" and self._initialized and self._libwolf:
            try:
                return self._wolf_aes_gcm_encrypt(data, key)
            except Exception as e:
                if _fips_active():
                    raise RuntimeError(f"FIPS AES-GCM Encrypt Failed: {e}. Raising to prevent unsafe leakage.") from e
                logger.debug(f"M3 Crypto: wolfSSL AES-GCM encrypt failed ({e}), using default fallback.")

        # Default Fallback: AES-256-GCM via cryptography
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        aesgcm = AESGCM(key)
        nonce = self.random_bytes(12)
        ciphertext = aesgcm.encrypt(nonce, data, None)
        return nonce + ciphertext

    def decrypt(self, token: bytes, key: bytes) -> bytes:
        """Decrypts data using AES-256-GCM."""
        self._require_fips_backend()

        if self.backend == "WOLFSSL" and self._initialized and self._libwolf:
            try:
                return self._wolf_aes_gcm_decrypt(token, key)
            except Exception as e:
                if _fips_active():
                    raise RuntimeError(f"FIPS AES-GCM Decrypt Failed: {e}. Raising to prevent unsafe leakage.") from e
                logger.debug(f"M3 Crypto: wolfSSL AES-GCM decrypt failed ({e}), using default fallback.")

        # Default Fallback: AES-256-GCM via cryptography
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        aesgcm = AESGCM(key)
        nonce = token[:12]
        ciphertext = token[12:]
        return aesgcm.decrypt(nonce, ciphertext, None)

    def lock_key(self):
        """Mandatory FIPS 140-3 Key Access Management: LOCK."""
        if self.backend == "WOLFSSL" and self._initialized:
            if hasattr(self._libwolf, "PRIVATE_KEY_LOCK"):
                self._libwolf.PRIVATE_KEY_LOCK()
            logger.debug("M3 Crypto: Private key LOCKED.")

    def unlock_key(self):
        """Mandatory FIPS 140-3 Key Access Management: UNLOCK."""
        if self.backend == "WOLFSSL" and self._initialized:
            if hasattr(self._libwolf, "PRIVATE_KEY_UNLOCK"):
                self._libwolf.PRIVATE_KEY_UNLOCK()
            logger.debug("M3 Crypto: Private key UNLOCKED.")

    def get_ssl_context(self):
        """Returns a FIPS-hardened SSLContext restricted to TLS 1.3."""
        if self.backend == "WOLFSSL" and self._initialized:
            try:
                import wolfssl

                # Hardened wolfSSL context
                ctx = wolfssl.SSLContext(wolfssl.PROTOCOL_TLS_CLIENT)
                ctx.set_ciphers("TLS13-AES256-GCM-SHA384:TLS13-AES128-GCM-SHA256")
                ctx.options |= wolfssl.OP_NO_TLSv1 | wolfssl.OP_NO_TLSv1_1 | wolfssl.OP_NO_TLSv1_2
                logger.info("M3 Crypto: Created FIPS-hardened TLS 1.3 context (wolfSSL).")
                return ctx
            except (ImportError, AttributeError) as e:
                # The usual state: the `wolfssl` Python package is optional and
                # rarely installed. Said once, at info: it is a fact about which
                # library serves TLS, not a fault (section 3: no false alarms).
                if not CryptoProvider._tls_fallback_noted:
                    import ssl as _ssl

                    CryptoProvider._tls_fallback_noted = True
                    logger.info(
                        "M3 Crypto: TLS is served by Python's ssl (%s), restricted to TLS 1.3, not by wolfSSL "
                        "(the wolfssl package is not importable: %s). It is outside crypto_provider's module; "
                        "see docs/FIPS_MODULE_BOUNDARY.md.",
                        _ssl.OPENSSL_VERSION,
                        e,
                    )

        # Default Fallback (Standard Python ssl)
        import ssl

        ctx = ssl.create_default_context()
        # Enforce TLS 1.3 via minimum_version (modern API; OP_NO_TLS* deprecated in 3.10+)
        if hasattr(ssl, "TLSVersion"):
            ctx.minimum_version = ssl.TLSVersion.TLSv1_3
            ctx.maximum_version = ssl.TLSVersion.TLSv1_3
        else:
            # Fallback for older Python without TLSVersion enum
            if hasattr(ssl, "OP_NO_SSLv2"):
                ctx.options |= ssl.OP_NO_SSLv2  # noqa: E701
            if hasattr(ssl, "OP_NO_SSLv3"):
                ctx.options |= ssl.OP_NO_SSLv3  # noqa: E701
            if hasattr(ssl, "OP_NO_TLSv1"):
                ctx.options |= ssl.OP_NO_TLSv1  # noqa: E701
            if hasattr(ssl, "OP_NO_TLSv1_1"):
                ctx.options |= ssl.OP_NO_TLSv1_1  # noqa: E701
            if hasattr(ssl, "OP_NO_TLSv1_2"):
                ctx.options |= ssl.OP_NO_TLSv1_2  # noqa: E701

        # Restrict to FIPS-APPROVED suites (approved algorithms; the library itself is not a validated module)
        try:
            ctx.set_ciphers(
                "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256"
            )
        except ssl.SSLError:
            pass

        return ctx


# Global Instance
provider = CryptoProvider(BACKEND)


def get_sha256(data: bytes) -> str:
    return provider.sha256(data)


def get_hmac_sha256(key: bytes, data: bytes) -> str:
    return provider.hmac_sha256(key, data)


def get_random_bytes(n: int) -> bytes:
    """Secure random bytes through the boundary (wolfCrypt's DRBG when live)."""
    return provider.random_bytes(n)


def get_token_hex(nbytes: int = 32) -> str:
    return provider.token_hex(nbytes)


def get_token_urlsafe(nbytes: int = 32) -> str:
    return provider.token_urlsafe(nbytes)


def random_choice(seq):
    """A uniformly chosen element of ``seq`` (``secrets.choice``'s contract),
    drawn from the boundary's random source by rejection sampling: no modulo bias."""
    n = len(seq)
    if n == 0:
        raise IndexError("random_choice from an empty sequence")
    limit = (1 << 32) - ((1 << 32) % n)
    while True:
        v = int.from_bytes(provider.random_bytes(4), "big")
        if v < limit:
            return seq[v % n]


# ── Network transport under a FIPS tier ──────────────────────────────────────
# m3's own cryptography runs in the provider above. NETWORK TLS does not: Python's
# `ssl` (OpenSSL) serves every HTTPS client in the process, and libpq (OpenSSL)
# serves PostgreSQL. The rules, by tier (docs/FIPS_MODULE_BOUNDARY.md):
#
#   M3_FIPS_MODE   : no downgrade. A PostgreSQL connection that crosses a network
#                    must use sslmode=verify-full; open-source TLS libraries are
#                    accepted in this tier, as open-source wolfCrypt is.
#   M3_FIPS_STRICT : every path on a validated module. m3 cannot inspect Python's
#                    OpenSSL or libpq for a FIPS provider, so the operator attests
#                    it (crypto.tls_validated). Without that, TLS is refused: the
#                    guard below makes every TLS handshake in the process fail
#                    with this explanation, and PostgreSQL over a network is
#                    refused. psycopg2-binary bundles its own OpenSSL, which can
#                    never be the OS's validated one, so strict refuses it too.


def _psycopg2_is_binary_wheel() -> bool:
    from importlib import metadata

    try:
        metadata.distribution("psycopg2-binary")
        return True
    except metadata.PackageNotFoundError:
        return False


_STRICT_TLS_REFUSAL = (
    "crypto.fips_strict (M3_FIPS_STRICT) refuses network TLS: m3 cannot confirm that this process's TLS library (Python's ssl, "
    "OpenSSL {openssl}) is a FIPS 140-3 validated module. observed: crypto.tls_validated is not set. "
    "remedy: run on a FIPS-validated OpenSSL provider (e.g. an OS FIPS mode) and set crypto.tls_validated = true "
    "in m3_config.toml, or carry the traffic over a validated transport (docs/FIPS_MODULE_BOUNDARY.md, profiles)."
)


def _install_strict_tls_guard() -> None:
    """Under strict with no attestation, make every TLS handshake in this
    process refuse with the reason. Plain HTTP (local services) is unaffected."""
    import ssl

    if getattr(ssl.SSLContext, "_m3_fips_guard", False):
        return
    message = _STRICT_TLS_REFUSAL.format(openssl=ssl.OPENSSL_VERSION)

    def refuse(self, *a, **k):
        raise FipsTransportError(message)

    ssl.SSLContext.wrap_socket = refuse  # type: ignore[method-assign]
    ssl.SSLContext.wrap_bio = refuse  # type: ignore[method-assign]
    ssl.SSLContext._m3_fips_guard = True  # type: ignore[attr-defined]
    logger.error("M3 Crypto: %s", message)


def _pg_verify_full_remedy(role) -> str:
    """How to make a role's DSN acceptable to FIPS tiers, without echoing the DSN
    (it holds a password). Shape and steps: docs/TROUBLESHOOTING.md."""
    from m3_core.secret_store import pg_secret_name

    name = pg_secret_name(role)
    return (
        f"store the {role.value} DSN with sslmode=verify-full and sslrootcert=<the server's CA file> "
        f"(when the certificate names a host other than the one you dial, host=<name in the certificate> "
        f"plus hostaddr=<address>): `m3 secret set {name} --keyring --stdin`. "
        "Getting the CA and the full steps: docs/TROUBLESHOOTING.md, 'FIPS mode refuses the PostgreSQL connection'"
    )


def require_pg_transport(dsn: str) -> None:
    """Refuse a PostgreSQL connection the active FIPS tier does not allow.
    Called by m3_core.pgconn before every connect; a no-op outside FIPS tiers."""
    if not _fips_active():
        return
    from m3_core.dsn import parse_pg_dsn

    parts = parse_pg_dsn(dsn)
    if parts is None or parts.is_local():
        return  # libpq rejects a bad DSN itself; a local socket crosses no network
    if parts.sslmode != "verify-full":
        raise FipsTransportError(
            "FIPS mode refuses a PostgreSQL connection that can fall back to plaintext or skip the server's "
            f"certificate. observed: host {parts.host}, sslmode={parts.sslmode or 'unset (libpq default: prefer)'}. "
            "remedy: add sslmode=verify-full (and the server's CA as sslrootcert) to the DSN; store it with "
            "`m3 secret set <M3_CDW_PG_URL or M3_PRIMARY_PG_URL> --keyring --stdin`. Steps, including how to get "
            "the CA: docs/TROUBLESHOOTING.md, 'FIPS mode refuses the PostgreSQL connection'."
        )
    if _fips_strict():
        if not _tls_validated_attested():
            import ssl

            raise FipsTransportError(_STRICT_TLS_REFUSAL.format(openssl=ssl.OPENSSL_VERSION))
        if _psycopg2_is_binary_wheel():
            raise FipsTransportError(
                "crypto.fips_strict (M3_FIPS_STRICT) refuses psycopg2-binary: it bundles its own OpenSSL, which cannot be the "
                "validated OS provider crypto.tls_validated attests. remedy: install psycopg2 from source "
                "against the OS OpenSSL (pip install --no-binary psycopg2 psycopg2) and remove psycopg2-binary."
            )


if _fips_strict() and not _tls_validated_attested():
    _install_strict_tls_guard()


def active_crypto_status() -> dict:
    """Read-only summary of the live crypto backend for `m3 doctor`.

    Returns {backend, fips_mode, fips_strict, fips_validated, lib_path,
    integrity_pinned, summary}. Best-effort; never raises.
    """
    lib_path = getattr(provider, "_lib_path", None)
    # Compute the loaded library's SHA-256 so the operator can self-pin it via
    # M3_WOLFSSL_SHA256 (they build their own wolfSSL, so this IS the trusted
    # hash to pin). Best-effort — never let doctor fail on a read error.
    lib_sha256 = None
    if lib_path:
        try:
            _h = hashlib.sha256()
            with open(lib_path, "rb") as _f:
                for _chunk in iter(lambda: _f.read(1 << 20), b""):
                    _h.update(_chunk)
            lib_sha256 = _h.hexdigest()
        except OSError:
            lib_sha256 = None
    out = {
        "backend": getattr(provider, "backend", "DEFAULT"),
        "fips_mode": _fips_mode(),
        "fips_strict": _fips_strict(),
        "fips_validated": getattr(provider, "_fips_validated", False),
        "lib_path": lib_path,
        "lib_sha256": lib_sha256,
        "integrity_pinned": bool(_crypto_text("crypto.wolfssl_sha256")),
        "summary": "",
    }
    if out["backend"] != "WOLFSSL":
        out["summary"] = (
            "DEFAULT (Python cryptography/hashlib). Approved algorithms only; "
            "no wolfCrypt loaded. `m3 config set crypto.fips_mode true` routes through wolfCrypt."
        )
    elif out["fips_validated"]:
        out["summary"] = f"wolfCrypt CMVP-validated FIPS module — loaded from {out['lib_path']}" + (
            " (integrity-pinned)" if out["integrity_pinned"] else ""
        )
    else:
        out["summary"] = (
            f"wolfCrypt OPEN-SOURCE (non-FIPS) build — loaded from {out['lib_path']}"
            + (" (integrity-pinned)" if out["integrity_pinned"] else "")
            + ". Hardened + fail-closed, but NOT the CMVP-validated module "
            "(M3_FIPS_STRICT would refuse this)."
        )
    return out


def crypto_paths_report() -> dict:
    """Every cryptographic path m3 uses, what serves it, and whether the active
    FIPS tier accepts it -- for `m3 doctor`. Read-only; never raises.

    Returns {"tier": "off"|"mode"|"strict", "rows": [{path, served_by, validated,
    ok, note}], "os_fips_mode": bool|None}. `validated` is True/False when m3
    can tell, None when only the operator can (an attestation). `ok` is False
    only when the active tier refuses the path.
    """
    import ssl

    tier = fips_tier()
    attested = _tls_validated_attested()
    rows: list[dict[str, object]] = []

    backend = getattr(provider, "backend", "DEFAULT")
    validated_module = bool(getattr(provider, "_fips_validated", False))
    rows.append(
        {
            "path": "at rest: secrets vault, key derivation, HMACs, audit chain",
            "served_by": "wolfCrypt" if backend == "WOLFSSL" else "Python cryptography (OpenSSL)",
            "validated": validated_module,
            "ok": True,  # the constructor already fails closed for each tier
            "note": ""
            if validated_module
            else "open-source build; crypto.fips_strict (M3_FIPS_STRICT) needs the validated module",
        }
    )
    rows.append(
        {
            "path": "random numbers: keys, IVs, salts, tokens",
            "served_by": "wolfCrypt DRBG" if getattr(provider, "_wolf_rng", None) else "OS CSPRNG (os.urandom)",
            "validated": validated_module if getattr(provider, "_wolf_rng", None) else False,
            "ok": True,
            "note": "",
        }
    )
    rows.append(
        {
            "path": "HTTPS from m3 (Python ssl)",
            "served_by": ssl.OPENSSL_VERSION,
            "validated": True if attested else None,
            "ok": tier != "strict" or attested,
            "note": "attested by crypto.tls_validated"
            if attested
            else ("refused: set crypto.tls_validated on a validated OpenSSL" if tier == "strict" else ""),
        }
    )
    pg_lib = "OpenSSL bundled in psycopg2-binary" if _psycopg2_is_binary_wheel() else "libpq linked to the OS OpenSSL"
    try:
        from m3_core.dsn import parse_pg_dsn
        from m3_core.secret_store import PgRole, resolve_pg_dsn

        for role in (PgRole.PRIMARY, PgRole.WAREHOUSE):
            parts = parse_pg_dsn(resolve_pg_dsn(role))
            if parts is None:
                continue
            local = parts.is_local()
            mode_ok = local or parts.sslmode == "verify-full"
            strict_ok = local or (mode_ok and attested and not _psycopg2_is_binary_wheel())
            ok = True if tier == "off" else (strict_ok if tier == "strict" else mode_ok)
            note = (
                "local connection, no network TLS"
                if local
                else f"sslmode={parts.sslmode or 'unset (libpq default: prefer)'}"
                + ("" if mode_ok else "; FIPS tiers require verify-full")
            )
            row: dict[str, object] = {
                "path": f"PostgreSQL ({role.value})",
                "served_by": pg_lib,
                "validated": None if local else (True if attested and not _psycopg2_is_binary_wheel() else False),
                "ok": ok,
                "note": note,
            }
            if not ok and not mode_ok:
                row["remedy"] = _pg_verify_full_remedy(role)
            rows.append(row)
    except Exception as e:  # noqa: BLE001 — the report is best-effort
        rows.append(
            {"path": "PostgreSQL", "served_by": pg_lib, "validated": None, "ok": True, "note": f"not read ({e})"}
        )
    rows.append(
        {
            "path": "Rust core HTTP client (remote HTTPS embedder only)",
            "served_by": "rustls (ring)",
            "validated": False,
            "ok": True,
            "note": "used only for an https:// embedder URL; to be removed from the core",
        }
    )
    try:
        from m3_core.seams import os_fips_mode

        osf = os_fips_mode()
    except Exception:  # noqa: BLE001
        osf = None
    return {"tier": tier, "rows": rows, "os_fips_mode": osf}
