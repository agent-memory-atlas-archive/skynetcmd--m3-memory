"""FIPS algorithm whitelist enforcement (Phase 3).

Python imports can't be truly sandboxed, so the FIPS algorithm boundary is
enforced by STATIC SCAN: this test fails if any non-approved cryptographic
primitive is used directly in shipped code (bin/ + m3_memory/).

Allowed exceptions (deliberate, narrow):
  - SHA-1 / MD5 with `usedforsecurity=False` — a non-cryptographic hash (cache
    key, change-detection). Python's flag tells FIPS-mode OpenSSL it's not a
    security hash.
  - Legacy Fernet on the DECRYPT path only (migration of pre-AES-GCM secrets);
    new writes use AES-256-GCM. We allow `Fernet(...).decrypt` but flag a bare
    `Fernet(...).encrypt`.
  - Test files and the m3-redact fixtures (which embed fake secrets on purpose).
"""

from __future__ import annotations

import ast
import os

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCAN_DIRS = [os.path.join(REPO, "bin"), os.path.join(REPO, "m3_memory")]

# Files/dirs exempt from the scan (tests + the m3-redact fixtures embed fake
# secrets on purpose; crypto_provider declares the BLOCKED list as constants).
_EXEMPT_SUBSTR = (
    os.sep + "test" + os.sep,
    os.sep + "tests" + os.sep,
    "test_",
    "redact",
    "crypto_provider.py",  # declares FIPS_BLOCKED_ALGORITHMS (string names)
)

# Hash constructors that are non-approved for SECURITY use. Allowed only when
# the call passes usedforsecurity=False (a non-cryptographic hash).
_WEAK_HASH_NAMES = {"md5", "sha1", "md4", "md2"}
# Cipher/primitive names blocked entirely (no security exception).
_BLOCKED_CIPHER_NAMES = {
    "TripleDES",
    "Blowfish",
    "ARC4",
    "RC4",
    "RC2",
    "DES",
    "DES3",
    "CAST5",
    "IDEA",
}

# Approved hashes called straight from hashlib bypass crypto_provider, so under
# M3_FIPS_MODE they run outside wolfCrypt. Allowed only as a declared
# non-security hash (usedforsecurity=False); a security use calls the provider
# (get_sha256 / sha256_stream / get_hmac_sha256).
_HASHLIB_DIGESTS = {
    "sha224",
    "sha256",
    "sha384",
    "sha512",
    "sha3_224",
    "sha3_256",
    "sha3_384",
    "sha3_512",
    "shake_128",
    "shake_256",
    "blake2b",
    "blake2s",
    "new",
    "file_digest",
}
# Keyed primitives: always a security use, so always through the provider.
_KEYED = {("hmac", "new"), ("hmac", "digest"), ("hmac", "HMAC"), ("hashlib", "pbkdf2_hmac"), ("hashlib", "scrypt")}
# Files allowed a direct digest, each with the reason it cannot use the provider.
_DIRECT_DIGEST_ALLOWED = {
    "bin/install_wolfssl.py": "verifies the wolfSSL download before wolfCrypt exists",
}


def _module_aliases(tree: ast.AST, module: str) -> tuple[set[str], dict[str, str]]:
    """Names `module` is bound to, and names imported from it (local -> real)."""
    aliases, from_names = set(), {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            aliases.update(a.asname or a.name for a in n.names if a.name == module)
        elif isinstance(n, ast.ImportFrom) and n.module == module:
            from_names.update({a.asname or a.name: a.name for a in n.names})
    return aliases, from_names


def _qualified(node: ast.Call, module: str, aliases: set[str], from_names: dict[str, str]) -> str:
    """The `module` function this call reaches, or ''."""
    f = node.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in aliases:
        return f.attr
    if isinstance(f, ast.Name) and f.id in from_names:
        return from_names[f.id]
    return ""


def _iter_py_files(scan_dirs, repo):
    for d in scan_dirs:
        for root, _dirs, files in os.walk(d):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(root, fn)
                rel = os.path.relpath(path, repo)
                # Exempt by the path RELATIVE to the scan root, so the (possibly
                # 'test_'-containing) absolute tmp path in unit tests doesn't
                # spuriously exempt a planted file.
                relscan = os.path.relpath(path, d)
                if any(s in relscan for s in _EXEMPT_SUBSTR) or any(s in fn for s in _EXEMPT_SUBSTR):
                    continue
                yield rel, path


def _call_name(node: ast.Call) -> str:
    """Last attribute/name of a call target, e.g. hashlib.md5 -> 'md5'."""
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _has_usedforsecurity_false(node: ast.Call) -> bool:
    for kw in node.keywords:
        if kw.arg == "usedforsecurity" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
            return True
    return False


def _scan(scan_dirs=None, repo=None):
    """AST-scan shipped code for non-approved crypto primitives.

    Returns [(file, lineno, why)]. AST (not regex) so it ignores comments and
    string literals and correctly handles multi-line calls."""
    scan_dirs = scan_dirs or SCAN_DIRS
    repo = repo or REPO
    violations = []
    for rel, path in _iter_py_files(scan_dirs, repo):
        try:
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=path)
        except (OSError, SyntaxError):
            continue
        rel_posix = rel.replace(os.sep, "/")
        mods = {m: _module_aliases(tree, m) for m in ("hashlib", "hmac")}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            digest = _qualified(node, "hashlib", *mods["hashlib"])
            if (
                digest in _HASHLIB_DIGESTS
                and not _has_usedforsecurity_false(node)
                and rel_posix not in _DIRECT_DIGEST_ALLOWED
            ):
                violations.append(
                    (
                        rel,
                        node.lineno,
                        f"hashlib.{digest}() outside crypto_provider "
                        "(security use: call the provider; otherwise "
                        "mark usedforsecurity=False)",
                    )
                )
            for mod in ("hashlib", "hmac"):
                fn = _qualified(node, mod, *mods[mod])
                if (mod, fn) in _KEYED:
                    violations.append(
                        (
                            rel,
                            node.lineno,
                            f"{mod}.{fn}() outside crypto_provider (keyed: use get_hmac_sha256 / pbkdf2_sha256)",
                        )
                    )
            name = _call_name(node)
            if name in _WEAK_HASH_NAMES and not _has_usedforsecurity_false(node):
                violations.append(
                    (rel, node.lineno, f"{name}() for security use (mark usedforsecurity=False if non-crypto)")
                )
            elif name in _BLOCKED_CIPHER_NAMES:
                violations.append((rel, node.lineno, f"{name} (blocked cipher)"))
            # Flag Fernet(...).encrypt(...) — new writes must use AES-256-GCM.
            # Fernet construction itself is allowed (legacy decrypt path).
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "encrypt"
                and isinstance(node.func.value, ast.Call)
                and _call_name(node.func.value) == "Fernet"
            ):
                violations.append((rel, node.lineno, "Fernet.encrypt (non-approved; use AES-256-GCM)"))
    return violations


def test_no_nonapproved_primitives_in_shipped_code():
    violations = _scan()
    if violations:
        msg = "Non-approved cryptographic primitives in shipped code:\n" + "\n".join(
            f"  {f}:{ln}: {why}" for f, ln, why in violations
        )
        msg += (
            "\n\nFIPS algorithm boundary (crypto_provider.FIPS_APPROVED_ALGORITHMS): "
            "AES-256-GCM, SHA-256/384/512, HMAC-SHA-256, PBKDF2-HMAC-SHA256, TLS1.3.\n"
            "Non-security hashes must pass usedforsecurity=False; legacy Fernet is "
            "decrypt-only."
        )
        raise AssertionError(msg)


def test_whitelist_constants_present():
    import sys

    sys.path.insert(0, os.path.join(REPO, "bin"))
    import crypto_provider as cp

    assert "AES-256-GCM" in cp.FIPS_APPROVED_ALGORITHMS
    assert "PBKDF2-HMAC-SHA256" in cp.FIPS_APPROVED_ALGORITHMS
    assert "MD5" in cp.FIPS_BLOCKED_ALGORITHMS
    assert "Fernet" in cp.FIPS_BLOCKED_ALGORITHMS


def test_scanner_catches_a_planted_violation(tmp_path):
    """Sanity: the scanner actually fires on real violations (and the AST
    handles multi-line + ignores comments/strings + honors usedforsecurity).

    NOTE: the scan dir must not contain an exempt substring (e.g. 'test_'); the
    pytest tmp path can, so we scan a dedicated 'scanroot/src' subtree.
    """
    bad = tmp_path / "scanroot" / "src"
    bad.mkdir(parents=True)
    (bad / "evil.py").write_text(
        "import hashlib\n"
        "x = hashlib.md5(b'secret').hexdigest()\n"  # VIOLATION
        "y = hashlib.sha1(b'x', usedforsecurity=False)\n"  # OK (non-crypto)
        "# hashlib.md5(this is a comment) -> ignored\n"  # OK (comment)
        "z = 'TripleDES is just a string here'\n"  # OK (string literal)
    )
    v = _scan(scan_dirs=[str(bad)], repo=str(tmp_path))
    whys = " ".join(w for _f, _ln, w in v)
    assert "md5" in whys.lower(), v
    assert "sha1" not in whys.lower(), f"flagged a usedforsecurity=False hash: {v}"
    assert "TripleDES" not in whys, f"flagged a string literal: {v}"
    assert len(v) == 1, f"expected exactly the md5 violation, got {v}"


def test_scanner_routes_security_digests_to_the_provider(tmp_path):
    """A direct hashlib SHA-2 call, under any import spelling, needs
    usedforsecurity=False; keyed primitives always go through the provider."""
    src = tmp_path / "scanroot" / "src"
    src.mkdir(parents=True)
    (src / "uses.py").write_text(
        "import hashlib\n"
        "import hashlib as _h\n"
        "import hmac\n"
        "from hashlib import sha256\n"
        "a = hashlib.sha256(b'k')\n"  # 5 VIOLATION
        "b = _h.sha512(b'k')\n"  # 6 VIOLATION
        "c = sha256(b'k')\n"  # 7 VIOLATION
        "d = hmac.new(b'k' * 16, b'm', hashlib.sha256)\n"  # 8 VIOLATION
        "e = hashlib.pbkdf2_hmac('sha256', b'p', b's', 1)\n"  # 9 VIOLATION
        "f = hashlib.sha256(b'x', usedforsecurity=False)\n"  # OK
        "g = hmac.compare_digest(b'a', b'b')\n"  # OK (compare only)
        "class P:\n"
        "    def sha256(self, d): return d\n"
        "h = P().sha256(b'x')\n"  # OK (not hashlib)
    )
    v = _scan(scan_dirs=[str(src)], repo=str(tmp_path))
    assert sorted(ln for _f, ln, _w in v) == [5, 6, 7, 8, 9], v
