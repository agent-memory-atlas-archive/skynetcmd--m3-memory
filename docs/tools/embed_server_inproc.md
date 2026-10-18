---
tool: bin/embed_server_inproc.py
sha1: 085907f8de46
mtime_utc: 2026-10-18T01:45:14.624782+00:00
generated_utc: 2026-10-18T01:45:45.800019+00:00
private: false
---

# bin/embed_server_inproc.py

## Purpose

Shared in-process GPU embedder server — one CUDA context, many thin clients.

WHY: `m3_core_rs.EmbeddedEmbedder` runs the GGUF embedder in-process. Every
process that embeds in-process opens its OWN CUDA context (multi-GB host
reservation) — CUDA contexts do not cross process boundaries, so the MCP memory
server and the cognitive loop each loaded a full copy (~4 GB + ~12 GB ≈ 18 GB on
the reference host) for one embedder's worth of work. This server loads the embedder ONCE and
serves it over localhost HTTP; clients disable their own tier-1 (M3_EMBED_GGUF
unset + M3_EMBED_GGUF_AUTODETECT=0) and defer to it via M3_EMBED_FALLBACK_URL.

The win is HOST RAM, not latency: one CUDA context instead of one-per-process.
Measured on the reference host (RTX 5080): a SINGLE small embed is P50 ~33 ms / P95 ~48 ms
via this server vs ~28 ms in-process — the localhost HTTP round-trip adds a few
ms (~10-15% on a single small request), not a rounding error. That per-call cost
AMORTISES across a batch (one round-trip for N vectors), so bulk paths — the
cognitive loop, file ingestion — see negligible overhead. Trade a few ms on
interactive single embeds for ~9-10 GB of host RAM back.

CONTRACT (matches the client tier-2 fallback in bin/memory/embed.py):
    POST /embedding        {"input": [texts...]}  -> {"data": [{"embedding": [...]}, ...]}  (input order)
    POST /v1/embeddings    {"model": ..., "input": str|list} -> OpenAI shape  (compat / tier-3)
    GET  /health           -> {"status", "model", "dim"}
Binary fast-path (high-volume ingestion): send `Accept: application/octet-stream`
to /embedding -> body = little-endian f32, row-major [n, dim], preceded by an
8-byte header (uint32 n, uint32 dim). numpy on both ends; no JSON float bloat.

HARDENING (§6): binds 127.0.0.1 only by default (the embedder is not a LAN
service); a semaphore serialises GPU calls; request size is capped. FAIL-LOUD
(§3): if the embedder can't load, exit non-zero with a clear message rather than
serve garbage. GPU-matrix (§1): the wheel picks CUDA/Metal/Vulkan/CPU; this
server is backend-agnostic — it just loads whatever EmbeddedEmbedder resolves.

Usage:
    python bin/embed_server_inproc.py                 # 127.0.0.1:8082 (the tier-2 default)
    python bin/embed_server_inproc.py --port 8082 --host 127.0.0.1
Model: M3_EMBED_GGUF env, else auto-detected (discover_bge_m3_gguf).

---

## Entry points

- `def main()` (line 581)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--port` |  | `_m3cfg.setting('embed.server.port')` |  | int |  |
| `--host` | Bind host: a loopback address (default 127.0.0.1). This server is plaintext, so any other address is refused; other machines use m3-embed-server's TLS listener. | `_m3cfg.setting('embed.server.host')` |  | str |  |
| `--log-file` |  | None |  | str |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `_task_runtime (_rotate_if_oversized)`
- `m3_sdk (acquire_or_exit)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `fastapi (FastAPI, Request, Response)`
- `fastapi.responses (JSONResponse)`
- `m3_core.config (settings)`
- `m3_core.paths (add_config_arg)`
- `m3_core.paths (apply_early_argv)`
- `m3_core.tier2_client (EXIT_CONFIG_REFUSED, is_loopback_host, plaintext_bind_refusal)`
- `memory (config)`
- `memory.embed (_EMBED_GGUF_MODEL_TAG, discover_bge_m3_gguf, new_embedded_embedder)`
- `numpy`
- `pydantic (BaseModel, Field)`
- `uvicorn`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
