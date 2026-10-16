# m3 Memory — FIPS 140-3 Module Boundary & Operating Guide

> **Status: FIPS 140-3 *deployment-ready*, not FIPS-validated.**
> m3 Memory implements no cryptography of its own and is **not itself a
> validated cryptographic module** — no application is. FIPS 140-3 validates a
> *cryptographic module* (e.g. wolfCrypt, OpenSSL FIPS, AWS-LC FIPS), not the
> application that calls it. This document states precisely what m3 does, where
> the cryptographic boundary is, and what each mode requires — so a security
> reviewer or federal customer can evaluate the deployment, not marketing.

---

## 1. What "deployment-ready" means here

m3 is FIPS-deployment-ready because it:

1. **Implements no custom cryptography.** All crypto is delegated to modules.
2. **Uses only FIPS-approved algorithms:** AES-256-GCM (authenticated
   encryption), SHA-256/384/512, HMAC-SHA-256, PBKDF2-HMAC-SHA256, and TLS 1.3.
   It blocks non-approved algorithms (MD5, SHA-1, DES, RC4, Fernet for new
   writes).
3. **Routes its own cryptography through one provider** (`bin/crypto_provider.py`):
   the secrets vault, key derivation, HMACs, the audit chain and every random
   key, IV, salt and token. A validated module (wolfCrypt FIPS) can serve all of it.
4. **Names every other path, and fails closed on it in FIPS mode.** Network
   transport (HTTPS, PostgreSQL TLS) is served by the platform's TLS library,
   not by the provider. §2a lists each path; under a FIPS tier m3 refuses a path
   the tier does not accept instead of using it silently.

`m3 doctor` prints this posture for the running install, path by path.

It is **not** "FIPS 140-3 compliant / certified / validated" — those terms have
specific NIST/CMVP regulatory meaning and apply to a validated module + its
certificate, which m3 (an application) cannot hold.

---

## 2. The cryptographic boundary

```
            ┌─────────────────────────── m3 application (OUTSIDE the boundary) ──┐
            │  memory store · search · chatlog · sync · audit-trail · vault API  │
            │                              │                                     │
            │                   crypto_provider.py  ◄── the only crypto entry    │
            └──────────────────────────────┼────────────────────────────────────┘
                                           ▼
            ┌──────────────── cryptographic module (INSIDE the boundary) ────────┐
            │  DEFAULT: Python `cryptography` (OpenSSL) + `hashlib`               │
            │  WOLFSSL: wolfCrypt  (open-source build, OR CMVP-validated FIPS)    │
            │  Services: sha256() · sha256_stream() · hmac_sha256() ·            │
            │            encrypt()/decrypt() (AES-256-GCM) · pbkdf2_sha256() ·    │
            │            get_ssl_context() (TLS 1.3)                              │
            └────────────────────────────────────────────────────────────────────┘
```

**Inside the boundary:** the crypto module (wolfCrypt or the Python
`cryptography`/OpenSSL stack) and the thin `crypto_provider.py` dispatch that
calls it. **Outside:** all m3 application logic, which only ever reaches crypto
through `crypto_provider`'s services.

**Services** (the crypto operations m3 exposes): `sha256`, `sha256_stream`
(incremental, for data too large to hold at once), `hmac_sha256`, `encrypt` /
`decrypt` (AES-256-GCM), `pbkdf2_sha256`, and `get_ssl_context` (TLS 1.3). Every
security use goes through them: the encrypted secrets vault (`auth_utils.py`),
key derivation, the tamper-evident audit hash chain (`audit_trail.py`), the
downloaded Rust-core wheel check, the config-parity key fingerprint and secret
HMACs, the vault-salt fingerprint, and the PostgreSQL store id (a DSN can carry a
password).

**Non-security hashes** (content, dedup and change-detection keys) call
`hashlib` directly with `usedforsecurity=False`, which declares them outside the
boundary. SHA-256 gives the same digest on every backend, so a node on wolfCrypt
and one on the default backend agree on every such key. One file is allowed a
direct digest without the flag: `install_wolfssl.py` verifies the wolfSSL
download before wolfCrypt exists. `tests/test_fips_algorithm_whitelist.py`
enforces all of this by static scan.

**Roles:** m3 has no privileged crypto-operator role of its own — it is a
*crypto user*. The operator configures the mode (below) via environment; the
validated module enforces its own role separation per its CMVP certificate.

---

## 2a. Every cryptographic path

| Path | Served by | Validated when |
|---|---|---|
| At rest: secrets vault, key derivation, HMACs, audit chain | `crypto_provider`: wolfCrypt (or Python `cryptography` with FIPS off) | the CMVP-validated wolfCrypt FIPS module is loaded (`M3_FIPS_STRICT`) |
| Random numbers: keys, IVs, salts, tokens | `crypto_provider`: wolfCrypt's DRBG (the OS CSPRNG with FIPS off) | as above |
| HTTPS from m3 | Python's `ssl` module, which is **OpenSSL** (the optional `wolfssl` Python package is rarely installed) | Python's OpenSSL runs a validated FIPS provider, e.g. an OS FIPS mode, and the operator attests it (`crypto.tls_validated`) |
| PostgreSQL (primary store, warehouse) | **libpq**, with the OpenSSL it is linked to; `psycopg2-binary` bundles its own | `psycopg2` is built from source against a validated OS OpenSSL, attested as above |
| Shared embed server's listener for other machines (`m3-embed-server`, `remote_listen`; TLS 1.3, AES-GCM, P-256/P-384, bearer token stored as its SHA-256) | rustls on **aws-lc-rs** | the `fips` build of m3-embed-server links AWS-LC's FIPS module; under `crypto.fips_strict` the server refuses this listener from any other build. Its loopback listener is plaintext and stays on this machine |
| Clients of that listener (`embed.fallback_url` = `https://…`) | `crypto_provider`'s TLS 1.3 context (Python's `ssl`), trusting `embed.fallback_ca` when set | as HTTPS from m3 above |
| Agent doorbell (`m3-dispatchd`) | none: local IPC only (Unix socket, Windows named pipe), access decided by the OS | nothing to validate |

PostgreSQL's libpq has no wolfSSL backend (its TLS support is OpenSSL-based), so
database TLS cannot run on wolfCrypt. A validated deployment uses a validated
OpenSSL for it, or carries the traffic over a validated transport (profile B).

### Transport rules by tier

| Tier | HTTPS | PostgreSQL over a network | PostgreSQL on a local socket or loopback |
|---|---|---|---|
| off | platform defaults | as the DSN says | as the DSN says |
| `M3_FIPS_MODE` | TLS 1.3, approved suites, where m3 builds the context | **`sslmode=verify-full` required**; open-source TLS libraries accepted, as open-source wolfCrypt is | allowed: it crosses no network |
| `M3_FIPS_STRICT` | **refused** unless `crypto.tls_validated = true`: every TLS handshake in the process fails with the reason | `verify-full`, `crypto.tls_validated = true`, and **no `psycopg2-binary`** | allowed |

`crypto.tls_validated` is an operator attestation in `m3_config.toml` (no
environment variable): m3 cannot inspect Python's OpenSSL or libpq for a FIPS
provider, so whoever configured the validated library says so.

"Over a network" is judged on the address libpq dials: `hostaddr` when set,
else `host`. A self-signed server certificate whose name does not resolve on
the client works with both: `host=<name in the certificate>
hostaddr=<address> sslmode=verify-full sslrootcert=<certificate>`.

Under either tier, warehouse sync from a PostgreSQL primary skips the
`postgres_fdw` fast path and uses the bridge: the FDW link is opened by the
primary server's libpq, where m3's transport check does not run.

### Deployment profiles

- **Homelab and small teams (FIPS-ready, not validated).** Recommended: Tailscale
  between machines, especially when a central PostgreSQL or warehouse is reached
  from outside one LAN, **plus `sslmode=verify-full` on every PostgreSQL DSN**.
  Tailscale authenticates devices; the inner TLS protects the data. Tailscale
  is a recommendation, never a requirement: many organisations block VPN clients.
- **Validated, profile A (validated TLS at the application).** wolfCrypt FIPS
  for the provider (`M3_FIPS_STRICT`), an OS FIPS OpenSSL provider for HTTPS and
  libpq (`psycopg2` from source), and `crypto.tls_validated = true`. Works over
  any network, Tailscale included: Tailscale's own FedRAMP guidance treats
  traffic wrapped in FIPS-validated TLS as the compliant layer.
- **Validated, profile B (validated mesh).** wolfCrypt FIPS for the provider,
  and wolfSSL's wolfScale (Tailscale built on wolfGuard and wolfCrypt FIPS)
  carrying traffic between machines, so the mesh is the validated transport
  layer. wolfScale is a wolfSSL product; licence, platforms and the Headscale
  control server are wolfSSL's to confirm.

m3 is the same software in every profile; only the modules underneath change.

---

## 3. The three modes (tiers)

| Env | Backend | wolfSSL required? | CMVP-validated module required? | Use case |
|---|---|---|---|---|
| *(neither set)* | Python `cryptography`/OpenSSL + `hashlib` | no | no | Default — runs everywhere |
| **`M3_FIPS_MODE=1`** | **wolfCrypt** | **yes** (fail-closed if absent) | **no** — accepts open-source build | Homelab / dev: real wolfCrypt + hardened boundary, **no commercial license needed** |
| **`M3_FIPS_MODE=1`** + **`M3_FIPS_STRICT=1`** | **wolfCrypt (validated)** | yes | **yes** — refuses non-FIPS build | True FIPS 140-3: requires the commercial wolfSSL FIPS module |

### Why two tiers?

The **CMVP-validated wolfCrypt FIPS module** is a **commercial, NDA-gated**
product — it is not a free download, and its validation only covers the exact,
unmodified vendor binary. The **open-source wolfSSL** build is freely buildable
(GPLv2 or commercial) but **lacks the FIPS service symbols** (POST, entropy
callbacks) and therefore is not the validated module.

So:

- **`M3_FIPS_MODE`** lets homelab/dev users get wolfCrypt's performance and the
  hardened algorithm boundary on the open-source build, without paying for the
  FIPS license. It still **fails closed** if wolfCrypt is entirely absent, still
  runs the power-up Known-Answer-Tests, and still blocks non-approved algorithms.
- **`M3_FIPS_STRICT`** is the gate for *actual* FIPS 140-3 compliance: it
  requires the validated module (FIPS symbols present + POST passes) and
  refuses the open-source build with a clear, actionable error.

---

## 4. Power-up self-tests (KATs)

When the wolfCrypt backend initializes, m3 runs in-process **Known-Answer-Tests**
against canonical vectors before serving any crypto:

- **SHA-256** — NIST FIPS 180-4 example, `SHA256("abc")`.
- **PBKDF2-HMAC-SHA256** — RFC 7914 §11 vector.
- **AES-256-GCM** — fixed key+nonce known-answer (ciphertext‖tag) plus an
  authenticated decrypt round-trip.

A KAT mismatch is **fatal** under any FIPS tier (the module enters an error
state and refuses to serve). These complement the validated module's own POST
(`wolfCrypt_GetStatus_fips`): POST attests the module; the KATs attest that *this
process's bindings invoke it correctly*.

---

## 5. Getting wolfSSL: download, build, place

> m3 does **not** bundle or redistribute wolfSSL — it is GPLv2 / commercial, and
> m3 is Apache-2.0. You build it **yourself, from the official source**, on your
> own machine. m3 only automates (or documents) the steps; it never ships or
> hosts the binary.

### Option A — the helper (recommended)

m3 ships a build-from-source helper that clones the **official** wolfSSL repo,
builds the shared library with exactly the features m3 uses (AES-GCM, SHA-256,
PBKDF2), installs it to `~/.m3/lib/` (where m3's secure loader finds it — §below),
and prints the SHA-256 to self-pin:

```bash
m3 fips install-wolfssl            # clone + build + install to ~/.m3/lib
#   --ref v5.9.2-stable            # pin a specific wolfSSL release tag
#   --dest <dir>                   # install elsewhere (then set M3_WOLFSSL_LIB)
#   --print-sha                    # also print just the SHA-256
```

It is equivalent to `python bin/install_wolfssl.py` (auditable, self-contained).

**Prerequisites** (a C toolchain + git):

| OS | Install |
|---|---|
| Debian/Ubuntu | `sudo apt install git autoconf automake libtool make gcc` |
| Fedora/RHEL | `sudo dnf install git autoconf automake libtool make gcc` |
| macOS | `xcode-select --install` (clang+make+git) **plus** a build system: the smallest is **cmake** — `brew install cmake`, or a standalone [CMake.app](https://cmake.org/download/) if you don't use Homebrew. (Or the autotools trio: `brew install autoconf automake libtool`.) The helper auto-finds Homebrew / CMake.app even when they're off your shell PATH. |
| Windows | Git, **CMake**, and **Visual Studio Build Tools** (C++ workload) |

*Optional:* if **ninja** is on PATH it is auto-detected and used as the cmake
generator, giving a true percentage progress bar during the build (and a slightly
faster compile). It is never required or auto-installed — without it the build
uses the platform default generator and shows a spinner. This keeps air-gapped
installs dependency-free: add ninja to the same build environment that already has
cmake + a compiler if you want the bar.

The result is the **open-source (non-FIPS) build** — works with `M3_FIPS_MODE=1`.
It is **not** the CMVP-validated FIPS module (`M3_FIPS_STRICT` requires that;
obtain it via wolfSSL's commercial channel — §3).

### Option B — build by hand

If you'd rather run the steps yourself (or audit them):

```bash
git clone --branch v5.9.2-stable --depth 1 https://github.com/wolfSSL/wolfssl.git
cd wolfssl
# Linux/macOS (autotools):
./autogen.sh && ./configure --enable-aesgcm --enable-sha256 --enable-pwdbased \
  --disable-examples --disable-crypttests && make -j
#   -> src/.libs/libwolfssl.so   (or .dylib on macOS)
# Windows (CMake + Visual Studio):
cmake -G "Visual Studio 17 2022" -A x64 -DBUILD_SHARED_LIBS=ON \
  -DWOLFSSL_AESGCM=yes -DWOLFSSL_PWDBASED=yes . && cmake --build . --config Release
#   -> Release\wolfssl.dll
```

> wolfSSL release tags are suffixed `-stable` (e.g. `v5.9.2-stable`).

### Where to place it

Copy the built library to **`~/.m3/lib/`** (m3's loader checks there
automatically), or to any path and point `M3_WOLFSSL_LIB` at it:

```bash
mkdir -p ~/.m3/lib
cp src/.libs/libwolfssl.so ~/.m3/lib/            # Linux
cp src/.libs/libwolfssl.dylib ~/.m3/lib/         # macOS
#  Windows: copy Release\wolfssl.dll  %USERPROFILE%\.m3\lib\
```

Then enable FIPS mode and verify with `m3 doctor` (the **crypto (FIPS)** section
shows the loaded path + SHA-256 to self-pin).

**Where m3 looks (secure, search-order-hardened).** m3 NEVER loads the crypto
library by bare name — that would delegate to the OS loader search order
(Windows: app dir + **CWD** + `%PATH%`; Linux: `LD_LIBRARY_PATH` + runpath),
a DLL-hijack vector where an attacker drops a weaker/backdoored `wolfssl.dll`
earlier in the path. Instead m3 resolves a **trusted absolute path** from this
precedence and loads exactly that file:

1. **`M3_WOLFSSL_LIB`** — an explicit absolute path you pin (strongest). Used
   **verbatim**, filename and all.
2. **`M3_LIB_DIR`/** — a directory you pin. m3 still appends the **per-OS
   filename** (`wolfssl.dll` / `libwolfssl.so` / `libwolfssl.dylib`), so unlike
   the pin above it stays correct across the three OSes. Use it when you only
   need to *relocate* the search.
3. **`~/.m3/lib/`** (m3's own lib dir; honours the decoupled roots — it is
   derived from `M3_CONFIG_ROOT`'s parent).
4. **Trusted system dirs only** — `/usr/local/lib`, `/usr/lib`, `/lib`,
   `/opt/homebrew/lib` (Unix) or `%SystemRoot%\System32` (Windows). The **CWD
   and `%PATH%`/`LD_LIBRARY_PATH` are deliberately excluded.**

> ⚠ **Relocating `M3_CONFIG_ROOT` moves the crypto search with it.** Step 3 is
> derived from that root, so a test sandbox, container, or per-user install that
> repoints it will look for wolfSSL somewhere the library was never installed.
> With `M3_FIPS_MODE=1` that is **fail-closed**: the process aborts rather than
> falling back. Pin `M3_LIB_DIR` at the real install directory in those setups.
> `m3 fips install-wolfssl` follows the same precedence, so the installer and
> the loader always agree on the location.

**Integrity pin (optional, strongest) — you pin *your own* build.** Because m3
does **not** bundle wolfSSL, you build it yourself (§5). So the pin is a
**self-pin / trust-on-first-use** model, not a check against some vendor hash:
right after you build (or obtain) and *trust* the library, compute ITS SHA-256
and pin that value. m3 then verifies the file against your pin on every load and
**refuses a mismatch** — catching any later tampering or in-place swap, even at
a trusted path. There is no canonical hash to compare to; the trust anchor is
the build you produced.

```bash
# After building/obtaining the library you trust, compute and pin ITS hash:
sha256sum ~/.m3/lib/libwolfssl.so        # Linux/macOS
#  -> use the value from YOUR build
export M3_WOLFSSL_SHA256=<that hash>
# Windows (PowerShell):
#   (Get-FileHash $env:USERPROFILE\.m3\lib\wolfssl.dll -Algorithm SHA256).Hash
```

Re-pin whenever you intentionally rebuild/upgrade wolfSSL (the new build has a
new hash — expected). A mismatch you did *not* expect means the library
changed underneath you: investigate before proceeding.

m3 also verifies the loaded library exposes the core crypto symbols
(`wc_AesGcmSetKey/Encrypt/Decrypt`, `wc_Sha256Hash`, `wc_PBKDF2`).
Run `m3 doctor` — the **crypto (FIPS)** section prints the exact absolute path
the library was loaded from, so you can confirm it's the trusted one.

**Validated FIPS build (for `M3_FIPS_STRICT`):** obtain the CMVP-validated
wolfCrypt FIPS module through wolfSSL's commercial channel (it ships the FIPS
service symbols + certificate). Build/configure it per wolfSSL's FIPS guidance.

---

## 6. Known limitations (stated plainly, not papered over)

FIPS-readiness in a Python application has real boundaries. m3 does not pretend
otherwise:

- **Key zeroization is best-effort.** CPython `bytes`/`str` are immutable and
  garbage-collected; key material in Python objects cannot be reliably wiped
  from memory. True zeroization requires keys to remain resident in the
  cryptographic module and never materialize in Python — a deeper redesign than
  m3 currently does. m3 minimizes key lifetime but **does not claim guaranteed
  zeroization** of Python-side key buffers.
- **Random-number generation scope.** m3's security-critical randomness (nonces,
  salts, tokens) uses the OS CSPRNG (`os.urandom` via `secrets`), which is a
  FIPS-grade *entropy source* but is the OS DRBG, not necessarily wolfCrypt's
  CTR-DRBG. Non-security identifiers (`uuid4` row/session IDs) are not routed
  through a FIPS DRBG by design. Strict deployments needing wolfCrypt-DRBG-only
  randomness should treat this as an open item.
- **The validation is wolfCrypt's, not m3's.** m3 uses a validated module "in an
  approved manner"; it carries no certificate of its own. Your deployment's
  assessor evaluates the combined system.

---

## 7. Verifying your deployment

```bash
m3 doctor          # shows the active crypto backend / FIPS tier
python bin/test_fips_integrity.py   # exercises the crypto abstraction + KATs
```

A correctly configured strict deployment reports the validated wolfCrypt module
active and the power-up KATs passing. A `M3_FIPS_MODE` (non-strict) deployment
reports wolfCrypt active but **not** validated — which is expected and correct
for the open-source build.

*See also: [`crypto_provider.py`](../bin/crypto_provider.py),
[`FIPS_COMPLIANCE.md`](FIPS_COMPLIANCE.md),
[`DESIGN_PHILOSOPHIES.md`](DESIGN_PHILOSOPHIES.md) §6.*
