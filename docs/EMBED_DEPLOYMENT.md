# Embed Deployment Guide

> Status: 2026-05-16. Architecture landed in waves 9.0-9.6 of the Project
> Oxidation pass. CUDA bench numbers (wave 9.7) land in
> `reports/bench_optimized.md` once the post-wave-9 run completes.

This document is operator-facing: build, install, configure, and troubleshoot
the m3 embedder stack on a single host. For setting details, cross-reference
`docs/CONFIG_REFERENCE.md` (`[embed]` tables; `docs/ENVIRONMENT_VARIABLES.md`
maps each legacy env name to its key) — entries are not duplicated here. For the input-side recipe (what text actually gets embedded — cascade,
anchor augmentation, variants, model-tag namespacing, pooling/BOS triplet)
see `docs/EMBED_INPUT_RECIPE.md`.

---

## Table of contents

1. [Naming: one topology, several vocabularies](#naming-one-topology-several-vocabularies)
2. [The dual-path architecture](#the-dual-path-architecture)
3. [Build matrix](#build-matrix)
4. [Build steps per backend](#build-steps-per-backend)
5. [The shared embed server (port 8082)](#sovereign-http-fallback-port-8082)
6. [m3-memory integration](#m3-memory-integration)
7. [Environment variables](#environment-variables)
8. [Troubleshooting](#troubleshooting)
9. [Cross-references](#cross-references)

---

## Naming: one topology, several vocabularies

There are **two** ways to run the embedder, but the docs (and the code, and the
benchmark reports) accumulated several names for each. They are synonyms — this
table is the canonical mapping, and it exists because nothing previously stated
it, so "Tier 2" and "the shared server" read as different subsystems.

| Canonical term | Also written as | What it is |
|---|---|---|
| **Shared embed server** *(the default)* | Tier 2, tier-2, sovereign HTTP server, `cpu-http-fallback`, "the embed server", port 8082 | One `m3-embed-server` process on `127.0.0.1:8082`. **Every** m3 process sends embed requests to it, so one model sits in host RAM (and one GPU context, on a GPU wheel) for the whole machine. |
| **In-process embedder** *(opt-in)* | Tier 1, tier-1, inproc, `EmbeddedEmbedder`, `*-inprocess` backend labels | llama.cpp linked into the calling process via pyo3. No IPC, but **not shareable** — each process that uses it loads its own copy of the model. |

Two clarifications the old naming actively obscured:

- **"Fallback" is a misnomer for the shared server.** The label
  `cpu-http-fallback` and the section title "CPU HTTP fallback" date from a
  design where in-process came first. It is now the **default path**, and
  in-process is the opt-in. The backend label is kept for compatibility with
  existing metrics; read it as "shared server", not "degraded mode".
- **"Tier" numbers do not imply preference or quality.** Tier 1 is not "better"
  than Tier 2 — it trades shareability for the loss of one loopback round trip
  (~33 ms vs ~28 ms P50 for a single small embed on the reference host). Both
  produce the same vectors from the same GGUF.

Where a page below says "tier-1" or "fallback", map it through this table.

## The dual-path architecture

> ⚠ **This diagram used to be inverted**, labelling in-process as `PRIMARY` and
> the shared server as a `FALLBACK`. That is backwards: `bin/memory/embed.py`
> routes to the **shared server by default** and treats in-process as opt-in.
> The safe-by-default gate was added deliberately, to stop a misresolved config
> root from spinning up a per-process GPU context.

```
   +------------------------------------------------+
   |  m3-memory (Python)  bin/memory_core.py        |
   |     _embed() / _embed_many()                   |
   +------------------------------------------------+
              |
              | 1. DEFAULT: shared server over loopback HTTP
              |    (one model in RAM for every m3 process)
              v
   +------------------------------------------------+
   |  m3-embed-server (127.0.0.1:8082)              |
   |  from the m3-core-rs wheel; always-on          |
   |  scheduled task / service                      |
   +------------------------------------------------+
              |
              | 2. OPT-IN INSTEAD: in-process via pyo3, when
              |    embed.inproc = true or .embed_config.json
              |    permits it (see the gate below)
              v
   +------------------------------------------------+
   |  m3_core_rs.EmbeddedEmbedder                   |
   |  (llama.cpp linked in-process; zero IPC)       |
   |  backend label: cuda-inprocess /               |
   |  vulkan-inprocess / metal-inprocess /          |
   |  cpu-inprocess                                 |
   +------------------------------------------------+
              |
              | 3. EXTERNAL PROVIDER: M3_EMBED_URL /
              |    M3_EMBED_FALLBACK_URL, when set
              v
   +------------------------------------------------+
   |  Any OpenAI-compatible /v1/embeddings endpoint  |
   |  (Ollama, LM Studio, vLLM, another host's m3)  |
   +------------------------------------------------+
```

Decision points (implemented in `bin/memory_core.py` around line 2018):

- **In-process** requires BOTH a usable GGUF (`M3_EMBED_GGUF`, or an
  autodetected bge-m3) with an `embedded[-cuda|-vulkan|-metal]` wheel, **AND**
  explicit permission: either `embed.inproc = true` (`m3 config set embed.inproc true`), or an `.embed_config.json`
  at the config root that does not set `disable_inproc_embedder`. A GGUF path
  alone is **not** sufficient — with no config file present and
  `embed.inproc` off, m3 logs a warning and routes to the shared server.
  That gate is deliberate (`bin/memory/embed.py:322-345`): a missing config
  means "defer to shared", never "load my own embedder", because the old
  behaviour let a misresolved config root spin up an unbounded per-process CUDA
  context. The dimension is validated against `EMBED_DIM` at construction time;
  a mismatch demotes the backend to HTTP before any real call runs.
- **CPU HTTP fallback** kicks in when the in-process embedder cannot be
  constructed (GGUF missing, CUDA OOM during init, wheel built without an
  `embedded` feature) or raises mid-call. The fallback target is
  `M3_EMBED_FALLBACK_URL` (default `http://127.0.0.1:8082`), POSTed at the
  singular `/embedding` path.
- **Primary HTTP** (`M3_EMBED_URL`) is the original LM-Studio-class path; it
  serves when both the in-process AND the fallback fail.

Runtime observability is exposed via `get_embed_backend_stats()` and
`reset_embed_backend_stats()` in `bin/memory_core.py`. Each served call
increments a thread-safe counter keyed by backend label; bulk calls
attribute one bump per text along the served path.

---

## Build matrix

| feature             | backend  | toolchain                                                                | tested              |
|---------------------|----------|--------------------------------------------------------------------------|---------------------|
| (default)           | HTTP     | none — pure HTTP client                                                  | Win / Linux         |
| `embedded`          | CPU      | C++ compiler (run `install_oxidation_buildtools.ps1` on Windows)         | Win / Linux         |
| `embedded-cuda`     | NVIDIA   | CUDA Toolkit 12.0+ + nvcc + MSVC (CUDA 13.2 confirmed on RTX 5080)       | Win (RTX 5080)      |
| `embedded-vulkan`   | cross-GPU| Vulkan SDK 1.3+ (sets `VULKAN_SDK` env var)                              | wired, untested     |
| `embedded-metal`    | Apple    | macOS + Xcode CLT (compile-gated to `target_os = "macos"`)               | wired, untested     |

GPU backends are **mutually exclusive** — pick exactly one per build. The
`embedded-cuda`/`-vulkan`/`-metal` features each additively enable
`embedded`, so `--features embedded-cuda` is sufficient on its own.
Conflicting GPU features fail at compile time via `compile_error!` in
`crates/m3-embed-llamacpp/src/lib.rs`.

`embedded-metal` is hard-gated on `target_os = "macos"`. Building it on
Windows/Linux produces a compile error rather than a silent CPU artifact.

The active backend at runtime is reported by
`m3_core_rs.embed_backend_label()` — one of `"cpu"`, `"cuda"`, `"vulkan"`,
`"metal"`, or `"none"` if the wheel was built without `embedded`.

---

## Build steps per backend

All commands assume PowerShell on Windows. The `m3-core-rs` repo lives at
`C:\Users\<USER>\m3-core-rs`; the Python bindings crate is `m3-core-py`.

### CPU build (universal)

1. Run the bootstrap script in an **elevated** PowerShell (installs cmake,
   MSVC Build Tools + VCTools workload, and LLVM/libclang — sets
   `LIBCLANG_PATH` machine-wide). CUDA is intentionally NOT installed by
   this script.

   ```powershell
   C:\Users\<USER>\m3-memory\install_oxidation_buildtools.ps1
   ```

2. Open a fresh PowerShell so the updated PATH and `LIBCLANG_PATH` are
   visible, then build and install the wheel:

   ```powershell
   cd C:\Users\<USER>\m3-core-rs\crates\m3-core-py
   python -m maturin build --release --features embedded
   python -m pip install --force-reinstall --no-deps `
       C:\Users\<USER>\m3-core-rs\target\wheels\m3_core_rs-0.0.0-cp314-cp314-win_amd64.whl
   ```

3. Verify:

   ```powershell
   python -c "import m3_core_rs; print(m3_core_rs.embed_backend_label())"
   # -> cpu
   ```

### CUDA build (Nvidia / Blackwell+)

Prerequisites:

- All CPU build prereqs above.
- CUDA Toolkit 12.0+ on PATH (13.2 confirmed working on RTX 5080 /
  Blackwell, sm_120).
- `nvcc --version` succeeds in a fresh PowerShell.
- `$env:CUDA_PATH` points at the toolkit root.

```powershell
# Refresh shell env to pick up the CUDA install:
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path","User")
$env:CUDA_PATH = [Environment]::GetEnvironmentVariable("CUDA_PATH","Machine")

# Populate MSVC env (skip if you are already in a Developer PowerShell):
$vsPath = & "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe" `
    -latest -products * -property installationPath
cmd /c "`"$vsPath\VC\Auxiliary\Build\vcvarsall.bat`" x64 && exit"

cd C:\Users\<USER>\m3-core-rs\crates\m3-core-py
python -m maturin build --release --features embedded-cuda
# 5-20 min compile (llama.cpp + ggml-cuda from source)
python -m pip install --force-reinstall --no-deps `
    C:\Users\<USER>\m3-core-rs\target\wheels\m3_core_rs-0.0.0-cp314-cp314-win_amd64.whl

# Verify:
python -c "import m3_core_rs; print(m3_core_rs.embed_backend_label())"
# -> cuda
```

#### CUDA wheel portability

The CUDA wheel produced by `maturin build --release --features embedded-cuda`
is **not** self-contained. The compiled `m3_core_rs*.pyd` dynamically links
against:

- `cublas64_13.dll`
- `cublasLt64_13.dll`
- `cudart64_13.dll`

These DLLs are loaded at import time from `$env:CUDA_PATH\bin\x64`. The
Python shim in `python/m3_core_rs/__init__.py` auto-registers that path via
`os.add_dll_directory()` before the extension is imported, so as long as the
target machine has a matching CUDA Toolkit installed and `CUDA_PATH` is set
(or the toolkit lives at the standard
`C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v*` layout), the wheel
imports cleanly — no extra operator setup.

What this means in practice:

- **The CUDA wheel is NOT redistributable.** Copying it to a machine without
  the CUDA Toolkit installed will fail at import with
  `ImportError: DLL load failed while importing m3_core_rs: The specified
  module could not be found.`
- The CUDA Toolkit version on the target machine should match the major
  version the wheel was built against (CUDA 13.x for this repo's wheels —
  `cublas64_13.dll` is the giveaway). A CUDA 12 host will not satisfy a
  CUDA 13 wheel.
- The **CPU wheel** (`--features embedded` with no `-cuda`) IS self-contained
  and portable — no external runtime DLLs needed.
- For a truly redistributable CUDA wheel, the CUDA runtime DLLs would need
  to be bundled into the wheel. On Linux this is `auditwheel repair`; on
  Windows the equivalent is [`delvewheel`](https://github.com/adang1345/delvewheel),
  which we have NOT exercised in this repo. Filed as future work — the
  current operator story assumes CUDA Toolkit is installed locally.

**Blackwell auto-fix (wave 9.1)**: the `m3-embed-llamacpp` init path detects
CUDA compute capability via `nvidia-smi` and sets
`GGML_CUDA_DISABLE_GRAPHS=1` automatically when sm_120+ is detected, before
`LlamaBackend::init()` runs. Operators on Blackwell do not need to set this
manually. Explicit user-set values win — if you export the variable
yourself (to any value), the auto-fix does not overwrite it.

### Vulkan build (cross-GPU, Linux / Windows)

Wired and compiles, but not exercised in this repo.

1. Install the LunarG Vulkan SDK from <https://www.lunarg.com/vulkan-sdk/> (or
   on Debian/Ubuntu: `apt install vulkan-sdk libvulkan-dev`).
2. Confirm `$env:VULKAN_SDK` (Windows) or `$VULKAN_SDK` (Linux) is set after
   install — restart the shell so the installer-set var is visible. Without
   it, `llama-cpp-sys-2`'s `build.rs` panics with
   `"Please install Vulkan SDK and ensure that VULKAN_SDK env variable is
   set: NotPresent"`.
3. Build the wheel:

   ```powershell
   cd C:\Users\<USER>\m3-core-rs\crates\m3-core-py
   python -m maturin build --release --features embedded-vulkan
   ```

   `embed_backend_label()` should return `vulkan`.

### Metal build (macOS only)

1. Install Xcode Command Line Tools: `xcode-select --install`.
2. Install `cmake` via Homebrew: `brew install cmake`.
3. Build the wheel:

   ```bash
   cd ~/m3-core-rs/crates/m3-core-py
   # Build for specific Python versions (e.g., 3.12)
   python -m maturin build --release --features embedded-metal --interpreter python3.12
   ```

   `embed_backend_label()` should return `metal`.

> **Pre-compiled macOS Wheels**: If you are using the `skynetcmd/m3-core-rs` repository, check the GitHub Actions "Build macOS Wheels" workflow for pre-compiled artifacts for Python 3.12, 3.13, and 3.14.

| Variant | Build Feature | Target Python |
| :--- | :--- | :--- |
| **Metal GPU** | `embedded-metal` | 3.12, 3.13, 3.14 |
| **CPU Sovereign** | `embedded` | 3.12, 3.13, 3.14 |

---
## Sovereign HTTP fallback (port 8082)

`m3-embed-server` is an HTTP embedder that wraps the same
`m3-embed-llamacpp` in-process backend behind an OpenAI-compatible HTTP API.
It is the deterministic fallback for the in-process path.

### Build

```bash
cd ~/m3-core-rs

# For CPU (Universal)
cargo build -p m3-embed-server --release --features embedded

# For Metal GPU (macOS Apple Silicon)
cargo build -p m3-embed-server --release --features embedded-metal
```

# Binary at: C:\Users\<USER>\m3-core-rs\target\release\m3-embed-server.exe
```

### Settings: m3_config.toml (and the secrets file)

The server reads m3's own two files and nothing else: `m3_config.toml` and the
secrets file it names (`secrets.file`, default `<config root>/m3_secrets.toml`).
Its keys (all optional except `embed.gguf`; full list with defaults in
[CONFIG_REFERENCE.md](CONFIG_REFERENCE.md)):

```toml
[embed]
gguf = "C:/Users/<USER>/.m3/models/bge-m3-Q4_K_M.gguf"
ctx  = 8192

[embed.server]
host = "127.0.0.1"
port = 8082
# streams, seq_max, n_batch, n_ubatch, coalesce_ms, max_batch_tokens,
# remote_listen, tls_cert, tls_key (set by `m3 embedder secure`)

[crypto]
fips_mode = false
```

Set them with `m3 config set <key> <value>` (for example
`m3 config set embed.server.port 8093`), then restart the server.
**Precedence**: `--set` (foreground and `secure --check` only) >
`m3_config.toml` > the default; the former `M3_*` environment names are not read
(the server logs one line at startup naming any still set). Without
`--config` the server finds the file the way m3 does
(`M3_CONFIG_ROOT` > `~/.m3/config`); an invalid file
exits 78.

Cores before 3.10.16 kept a separate `config.toml`
(`%PROGRAMDATA%\m3-embed-server\config.toml`, `~/.config/m3-embed-server/`,
`~/Library/Application Support/m3-embed-server/`). The server no longer reads
it; `m3 config import-env` (run by `m3 upgrade`) and `m3 embedder install`
move its values into m3's files and remove it. A value `m3_config.toml`
already holds differently is reported, not overwritten.

### Install as a Windows Service (elevated PowerShell)

```powershell
m3 embedder install
```

`m3 embedder install` records what the service needs in `m3_config.toml`
(the model it found, and any server setting this shell has only from an
environment variable), then runs `m3-embed-server --config <m3_config.toml>
install`, which registers the service with that `--config` on its command line:
it runs as LocalSystem and could not find your config otherwise. The server
itself refuses to install while this shell sets a value the file does not hold,
and names the `m3 config set` command.

Verify the service is up:

```powershell
curl http://127.0.0.1:8082/health    # -> "OK"
curl http://127.0.0.1:8082/metrics   # -> JSON dispatcher stats
Get-Service m3-embed-server          # status: Running
m3 embedder status
```

`status` works without admin; `start`/`stop`/`install`/`uninstall` need
elevation. `uninstall` leaves `m3_config.toml` in place (it is m3's).

### Foreground / dev mode

```powershell
m3-embed-server --config "$env:USERPROFILE\.m3\config\m3_config.toml"
# a one-off override:  m3-embed-server "--set={embed.server.port=8093}"
# Ctrl-C to stop. Logs go to stderr.
```

### Recovery actions

Configured automatically by `install`: the installer shells out to
`sc.exe failure` to set restart-on-failure (3 restarts, 5 s delay, 60 s
reset window). If the `sc.exe` call fails (typically because `install` was
run unelevated and only succeeded partially), the installer prints a WARN
with the exact one-liner to run by hand:

```powershell
sc.exe failure m3-embed-server reset= 60 actions= restart/5000/restart/5000/restart/5000
```

Equivalent GUI path: `services.msc` -> m3 Embed Server -> Recovery tab.

### Logs

Service-mode logs land in `%PROGRAMDATA%\m3-embed-server\service.log.YYYY-MM-DD`,
rotated daily (UTC) via `tracing-appender`. The active day's file is the
newest one in the directory. Foreground / dev mode keeps the original
`env_logger` stderr behaviour — only the Windows Service path uses the
rolling-file appender.

Old rolled files are pruned on service startup: anything older than 14 days
is deleted. No external rotator needed.

Tail the most recent day's log:

```powershell
Get-ChildItem $env:PROGRAMDATA\m3-embed-server\service.log.* |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1 |
    Get-Content -Wait
```

### Non-Windows

Foreground mode (CPU build) runs on Linux unchanged. There is no systemd
unit generator in this crate yet — wire it up manually if you need a
sovereign install on Linux.

---

## Shared GPU embedder (one CUDA context for all m3 processes)

**The problem.** The in-process Rust embedder (`m3_core_rs.EmbeddedEmbedder`,
tier-1) runs the GGUF model *inside* the calling process. CUDA contexts do not
cross process boundaries, so **every** m3 process that embeds in-process opens
its **own** CUDA context — each a multi-GB host-RAM reservation. On a box where
the MCP memory server *and* the cognitive loop both embed, you pay for the model
twice (~4 GB + up to ~12 GB observed). "In-process, shared between processes" is
a contradiction — the only way to load the GPU model **once** and serve many
processes is one owner process + thin clients over IPC.

**The fix.** `bin/embed_server_inproc.py` loads the embedder ONCE and serves it
over localhost HTTP; the other processes disable their own tier-1 and defer to
it. **One CUDA context total (~9-10 GB reclaimed) — the win is host RAM, not
latency.** Measured (RTX 5080): a single small embed is ~33 ms P50 via the server
vs ~28 ms in-process — the localhost round-trip adds a few ms (~10-15% on a
single small request). That fixed per-call cost amortises across a batch (one
round-trip for N vectors), so bulk paths (the cognitive loop, file ingestion)
see negligible overhead. You are trading a few ms on interactive single embeds
for ~9-10 GB of RAM back.

### Enable it

```bash
# 1. Route all m3 processes to the shared server (writes .embed_config.json):
m3 embedder shared                 # or: --port 8091 for a non-default port

# 2. Make the shared server run (it is the SOLE GPU embedder):
#    - it is installed as the AgentOS_EmbedServer scheduled task by
#      `python bin/install_schedules.py --repair` (Windows; elevated shell), OR
#    - start it directly:
python bin/embed_server_inproc.py --port 8082

# 3. Restart the MCP server + cognitive loop so they re-read the config.
```

`m3 embedder shared` writes `<config_root>/.embed_config.json`:

```json
{ "disable_inproc_embedder": true, "fallback_url": "http://127.0.0.1:8082" }
```

This is a **config file, not an env var** — because a headless daemon (the
scheduled-task loop, the MCP server) does not inherit your shell environment
(DESIGN_PHILOSOPHIES §3). `bin/memory/embed.py` reads it at import: precedence is
m3_config.toml > config file > default.

### Revert

⚠ **Removing the config does NOT switch processes to in-process embedding.** With
no `.embed_config.json` present, `bin/memory/embed.py` falls back to the
`embed.inproc` setting, which defaults to off — so every process routes to the shared
server instead. Running `m3 embedder unshared` and then stopping the shared
server leaves you with **no embedder at all**: writes still succeed, but
embedding is deferred and new rows are FTS-only until an embedder returns.
(Verified 2026-09-10; the CLI's own success message claimed the opposite and has
been corrected.)

To actually run per-process in-process embedders:

```bash
m3 embedder unshared               # removes .embed_config.json
m3 config set embed.inproc true    # every m3 process reads it from m3_config.toml
# Restart the m3 processes (your agents' MCP server, the background service) so
# they pick it up; keep the shared server running until they have.
m3 embedder status                 # confirm which backend each process reports
```

To simply go back to the default (shared) topology, run `m3 embedder shared` —
that writes the config and prints the three steps to bring the server up.

### Serving other machines (TLS and a bearer token)

The embed server receives raw memory text, and the vectors it returns can be
partly inverted back to it. So its normal listener (`embed.server.host`) must be
a loopback address and is plaintext; a non-loopback host is refused (exit 78),
by the Rust `m3-embed-server` and by every Python embed server alike.

To let other machines use one embedder, the Rust `m3-embed-server` adds a
**second listener** that always requires TLS 1.3 and a bearer token. The loopback
listener, and everything on this machine that uses it, is unchanged.

```bash
# On the server. Put it on a private network first; Tailscale is recommended,
# and gives a publicly trusted certificate for the machine's tailnet name:
tailscale cert myhost.example-tailnet.ts.net      # writes the .crt and .key here
m3 embedder secure --listen 100.64.0.7 \
    --cert myhost.example-tailnet.ts.net.crt --key myhost.example-tailnet.ts.net.key
m3 embedder stop && m3 embedder start             # prints the token to give clients

# On each client:
m3 secret set M3_EMBED_SERVER_TOKEN               # paste the token
m3 config set embed.fallback_url https://myhost.example-tailnet.ts.net:8082
# Only for a certificate from a private CA:
m3 config set embed.fallback_ca /path/to/ca.pem
```

- `--listen` takes an IP or `IP:port` (the port defaults to the loopback one).
- The server checks the settings first (`m3-embed-server secure --check`, which
  writes nothing); m3 then records `embed.server.remote_listen`, `tls_cert` and
  `tls_key` in `m3_config.toml`, and the token's SHA-256 as the secret
  `M3_EMBED_SERVER_TOKEN_SHA256` in the secrets file. No elevation is needed.
- The server keeps only the token's SHA-256; `/health` needs no token.
- Clients send the token only over `https://` (or to loopback), never in
  plaintext to another machine.
- Tailscale decides who can reach the port; TLS and the token still apply inside
  it. WireGuard's algorithms are not FIPS-approved, so Tailscale alone is never
  the FIPS-ready layer: m3 is FIPS 140-3 ready on this path when the server is
  the `fips` build of m3-embed-server (AWS-LC's FIPS module), and
  `crypto.fips_strict` refuses the listener from any other build. For a
  validated deployment end to end, see [FIPS_MODULE_BOUNDARY.md](FIPS_MODULE_BOUNDARY.md).
- `m3 embedder secure --off` unsets those keys and the digest (restart to
  apply); `--new-token` rotates the token.

## Draining an embed backlog

Rows that failed to embed (or predate an embedder) stay FTS-searchable but are
invisible to semantic/vector search. To drain them:

```bash
m3 embedder backfill                       # every store: main AND chatlog
m3 embedder backfill --limit 500           # cap the run
m3 embedder backfill --db /path/to/one.db  # target one store deliberately
```

**Sweeps every store by default, and that default matters.** On a split topology
(the default) the chatlog is a separate database file. A `--db`-only sweep leaves
it permanently unembedded while the cognitive loop reports no pending work,
because the main DB is clean — observed 2026-07-25 with 6,653 unembedded chat
turns on a host whose loop was running normally.

Oversized rows are split and mean-pooled rather than dropped. `--no-subdivide-oversize`
restores the old drop behaviour and is almost never what you want: it leaves
those rows permanently without a vector.

If the sweep aborts, read the message carefully — it distinguishes what was
**observed** from what is merely **possible**, and content failures (a row the
embedder refused) no longer count toward the abort threshold at all:

```
ABORT: 5 consecutive INFRASTRUCTURE failures (content failures are excluded).
observed: 5 failed batch(es), 0 of them content-shaped; 0 row(s) embedded.
possible: embed server unreachable, overloaded, or a network path issue.
inspect: M3_EMBED_FALLBACK_URL. try: m3 embedder status
```

---

### When to use it

- **Use shared** when multiple m3 processes embed on the same GPU and host RAM is
  tight — the common desktop/homelab case (one GPU, MCP server + loop + maybe a
  local chat model competing for VRAM).
- **Keep per-process (unshared)** when only one process embeds, or when you want
  zero inter-process dependency (each process is self-contained but heavier).

The shared server binds `127.0.0.1` only (it is not a LAN service), serializes
GPU calls with a semaphore, caps batch size (413 on overflow, never silent
truncation), and exits non-zero if the embedder can't load — see the module
docstring in `bin/embed_server_inproc.py`.

---

## m3-memory integration

`bin/memory_core.py`'s `_embed()` (single) and `_embed_many()` (batch)
implement the dual-path chain:

1. **In-process via `m3_core_rs.EmbeddedEmbedder`.** Requires a GGUF AND the
   wheel built with `embedded[-cuda|...]` AND explicit permission
   (`embed.inproc = true` (`m3 config set embed.inproc true`), or an `.embed_config.json` that does not disable it) —
   a GGUF path alone routes to the shared server instead.
   Backend labels recorded: `cuda-inprocess` / `vulkan-inprocess` /
   `metal-inprocess` / `cpu-inprocess`.

2. **CPU HTTP fallback.** Triggered when the in-process embedder fails to
   construct OR raises during a call. POSTs to `{M3_EMBED_FALLBACK_URL}/embedding`
   (default `http://127.0.0.1:8082/embedding`, singular path). Vectors are
   tagged with `M3_EMBED_GGUF_MODEL_TAG`, sharing the cache namespace with
   in-process vectors. Backend label: `cpu-http-fallback`.

3. **Legacy primary HTTP via `M3_EMBED_URL`.** The original LM-Studio /
   llama-server route. Backend label: `http-primary`.

Vectors produced by paths (1) and (2) share a cache namespace
(`M3_EMBED_GGUF_MODEL_TAG`, default `bge-m3-GGUF-Q4_K_M.gguf`) that is
distinct from LM Studio's `text-embedding-bge-m3` namespace. This is a
deliberate cache-segregation choice so the in-process path can be enabled
without invalidating prior HTTP-tagged rows.

### Observability

```python
from memory_core import get_embed_backend_stats, reset_embed_backend_stats

reset_embed_backend_stats()
# ... do work that produces embeddings ...
print(get_embed_backend_stats())
# e.g. {'cuda-inprocess': 1234, 'cpu-http-fallback': 7}
```

Use this to diagnose distribution between paths:

- Lots of `cpu-http-fallback` where you expected `cuda-inprocess` -> the
  wheel may be the CPU variant, or the GGUF path is wrong, or the in-process
  embedder is raising mid-call (look in stderr).
- Lots of `http-primary` -> both in-process AND fallback failed; check
  service status and `m3_core_rs.embed_backend_label()`.
- All `http-primary` from the start -> in-process was never enabled (needs
  `embed.inproc = true` (`m3 config set embed.inproc true`) or a permitting `.embed_config.json`, plus a GGUF) and
  the fallback server is unreachable.

Both helpers are thread-safe. `_embed` bumps by 1; `_embed_many` attributes
one bump per text along the path that served it.

---

## Environment variables

The canonical list lives in [`docs/CONFIG_REFERENCE.md`](CONFIG_REFERENCE.md)
(the `[embed]` tables); [`docs/ENVIRONMENT_VARIABLES.md`](ENVIRONMENT_VARIABLES.md)
maps each name below to its key. Relevant rows for embed deployment,
summarized — see the reference for full semantics and defaults:

| Variable                    | Role                                                               |
|-----------------------------|--------------------------------------------------------------------|
| `M3_EMBED_GGUF`             | Path to bge-m3 GGUF for the in-process path (which also needs `embed.inproc = true` (`m3 config set embed.inproc true`) or `.embed_config.json`). |
| `M3_EMBED_GGUF_MODEL_TAG`   | `embed_model` cache-namespace tag for in-process + fallback rows.  |
| `M3_EMBED_URL`              | Legacy primary HTTP endpoint (LM Studio, llama-server).            |
| `M3_EMBED_FALLBACK_URL`     | CPU HTTP fallback endpoint (default `http://127.0.0.1:8082`).      |
| `M3_EMBED_STREAMS`          | Legacy name of `embed.streams`: in-process context count. The server's is `embed.server.streams`. |
| `M3_EMBED_CTX`              | Per-context `n_ctx`; KV cache scales with this.                    |
| `M3_EMBED_SEQ_MAX`          | Legacy name of `embed.seq_max` (server: `embed.server.seq_max`).   |
| `M3_EMBED_N_BATCH`          | Legacy name of `embed.n_batch` (server: `embed.server.n_batch`).   |
| `M3_EMBED_N_UBATCH`         | Legacy name of `embed.n_ubatch` (server: `embed.server.n_ubatch`). |
| `M3_EMBED_COALESCE_MS`      | Legacy name of `embed.coalesce_ms` (server: `embed.server.coalesce_ms`). |
| `M3_EMBED_MAX_BATCH_TOKENS` | Legacy name of `embed.max_batch_tokens` (server: `embed.server.max_batch_tokens`). |
| `M3_EMBED_SERVER_PORT`      | Port `m3-embed-server` binds (default 8082).                       |
| `M3_EMBED_SERVER_HOST`      | Bind host (default `127.0.0.1`).                                   |
| `GGML_CUDA_DISABLE_GRAPHS`  | Blackwell auto-set by wave 9.1; user value wins if explicit.       |

---

## Troubleshooting

### Blackwell embed slow (~2 s per call)

The wave 9.1 auto-fix detects sm_120+ via `nvidia-smi` and sets
`GGML_CUDA_DISABLE_GRAPHS=1` before `LlamaBackend::init()`. If detection
fails silently (no `nvidia-smi`, unexpected output format), force it:

```powershell
$env:GGML_CUDA_DISABLE_GRAPHS = "1"
```

Confirm the auto-fix fired by checking stderr at startup for a log line of
the form `detected CUDA compute cap X.Y - setting GGML_CUDA_DISABLE_GRAPHS=1`.

### CUDA OOM during bulk ingest

Reduce `embed.streams` (in-process, default 8) or `embed.server.streams`
(the server: 2 on a GPU build, 1 on CPU): `m3 config set embed.streams 4`. Each context costs roughly `n_ctx * 96 KB` of KV
cache on the GPU for bge-m3 fp16. At `M3_EMBED_CTX=8192` x 8 streams that is
~6 GB on a 16 GB RTX 5080; drop to 4 streams for headroom, or shrink `ctx`.

### "decode: cannot decode batches" or "embeddings required but some input tokens were not marked"

Pre-wave-8c bug. Cause: `LlamaBatch::add_sequence(..., false)` plus
`ctx.decode()` for a CLS-pooled non-causal model (bge-m3 is BERT-style).
Two-line fix in `crates/m3-embed-llamacpp/src/lib.rs` (around lines 756 +
765): pass `true` to `add_sequence` and call `ctx.encode()` instead of
`ctx.decode()`. See `C:\Users\<USER>\m3-core-rs\reports\hang_diagnosis.md`
and `fix_verification.md` for the full diagnosis. If you are hitting this,
update to a post-wave-8c `m3-core-rs` build and rebuild the wheel.

### Port 8082 already in use

`m3-embed-server` bind fails with "Only one usage of each socket address".
Find the offender:

```powershell
Get-NetTCPConnection -LocalPort 8082 | Select-Object OwningProcess
Get-Process -Id <pid>
```

Either kill the offender, or move the server: `m3 config set
embed.server.port <port>` and restart the service. Then
point m3 at the new port with `m3 embedder shared --port <port>` (writes
`.embed_config.json`) and update `M3_EMBED_FALLBACK_URL` wherever it is set in
the MCP server registration's `env` — background services don't read your
shell env.

### Fallback chain not behaving as expected

Run a short Python session and inspect the counters:

```python
from memory_core import get_embed_backend_stats, reset_embed_backend_stats
reset_embed_backend_stats()
# ... a few writes / searches ...
get_embed_backend_stats()
```

Cross-check:

- `embed.gguf` names a file that exists (`m3 config get embed.gguf --origin`)?
- `python -c "import m3_core_rs; print(m3_core_rs.embed_backend_label())"`
  returns the backend you expect?
- `curl http://127.0.0.1:8082/health` returns `OK`?
- `Get-Service m3-embed-server` shows `Running`?

### Wheel labeled "cuda" but actually serving CPU

`m3_core_rs.embed_backend_label()` returns `cuda` if and only if the wheel
was built with `--features embedded-cuda`. If it returns `cpu`, the wheel
is the CPU variant — rebuild with `--features embedded-cuda` and reinstall
with `pip install --force-reinstall --no-deps <wheel>`.

### `VULKAN_SDK` not present at build time

`llama-cpp-sys-2`'s `build.rs` panics with
`"Please install Vulkan SDK and ensure that VULKAN_SDK env variable is set:
NotPresent"`. Install the LunarG SDK, restart the shell so the installer's
env-var changes take effect, and re-run the build. Verify with
`$env:VULKAN_SDK` (Windows) or `echo $VULKAN_SDK` (Linux).

### `embedded-metal` on Linux/Windows

Compile error by design (wave 9.6). The feature is gated to
`target_os = "macos"`; building it elsewhere is an explicit error rather
than a silent CPU artifact. Use `embedded` (CPU) or `embedded-vulkan` /
`embedded-cuda` on non-Apple hosts.

### Bench results

For end-to-end numbers (latency, throughput, GPU utilisation), see
`C:\Users\<USER>\m3-core-rs\reports\bench_optimized.md` (post-wave-9
combined report). Pre-oxidation baselines live in
`reports/bench_baseline.md`. Wave 9.7 will land the CUDA-build numbers in
that combined report — do not infer numbers from this document.

---

## Cross-references

- Input-side recipe (what text gets embedded): `C:\Users\<USER>\m3-memory\docs\EMBED_INPUT_RECIPE.md`
- m3-embed-llamacpp crate docs: `C:\Users\<USER>\m3-core-rs\crates\m3-embed-llamacpp\README.md`
- m3-embed-server crate docs: `C:\Users\<USER>\m3-core-rs\crates\m3-embed-server\README.md`
- CPU build-tools bootstrap: `C:\Users\<USER>\m3-memory\install_oxidation_buildtools.ps1`
- Settings canonical list: `docs/CONFIG_REFERENCE.md` (legacy env names: `docs/ENVIRONMENT_VARIABLES.md`)
- Bench numbers: `C:\Users\<USER>\m3-core-rs\reports\bench_baseline.md` (wave 0),
  `C:\Users\<USER>\m3-core-rs\reports\bench_optimized.md` (post-wave-9)
- Hang / fix history: `C:\Users\<USER>\m3-core-rs\reports\hang_diagnosis.md`,
  `C:\Users\<USER>\m3-core-rs\reports\fix_verification.md`
