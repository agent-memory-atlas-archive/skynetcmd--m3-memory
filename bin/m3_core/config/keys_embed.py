"""Registry table [embed].

Every default and env name is taken from today's readers (surveyed in
bin/memory/config.py, embed.py, tokens.py, chunking.py,
embed_server_inproc.py, embed_server_gpu.py, m3_memory/embedder_admin.py).
Where a reader parsed a value differently from the canonical parser, that
reading is ``legacy_parse``: the canonical one wins and the difference is
logged once (§2, intended-change list).
"""

from __future__ import annotations

from .registry import Key, in_range, parse_bool, parse_float, parse_int, parse_list, parse_path, parse_str

_NONNEG = in_range(lo=0)


def _int(
    name: str,
    env: tuple[str, ...],
    default: int,
    doc: str,
    *,
    lo: int = 1,
    hi: int | None = None,
    reloadable: bool = False,
) -> Key[int]:
    return Key[int](
        name=name,
        parse=parse_int,
        default=default,
        validate=in_range(lo=lo, hi=hi),
        env_names=env,
        reloadable=reloadable,
        doc=doc,
    )


def _int_derived(name: str, env: tuple[str, ...], doc: str, *, lo: int = 1, hi: int = 2**32 - 1) -> Key[int]:
    """An integer whose default the Rust core derives (from ctx, streams or the
    build), so m3 has no single number to state: unset = the core decides."""
    return Key[int](
        name=name,
        parse=parse_int,
        default=None,
        validate=in_range(lo=lo, hi=hi),
        env_names=env,
        reloadable=False,
        doc=doc,
    )


def _float(name: str, env: tuple[str, ...], default: float, doc: str, *, reloadable: bool = False) -> Key[float]:
    return Key[float](
        name=name, parse=parse_float, default=default, validate=_NONNEG, env_names=env, reloadable=reloadable, doc=doc
    )


def _str(name: str, env: tuple[str, ...], default: str | None, doc: str) -> Key[str]:
    return Key[str](name=name, parse=parse_str, default=default, env_names=env, reloadable=False, doc=doc)


# Most embed knobs are read at import today (memory/config.py, embed.py), so none
# is reloadable until 2D turns those constants into accessors.
EMBED: list[Key] = [
    # ── identity and location ──
    _str(
        "embed.model",
        ("M3_EMBED_MODEL", "EMBED_MODEL"),
        "text-embedding-bge-m3",
        "The embedding model name requested from the endpoint.",
    ),
    _str(
        "embed.space_tag",
        ("M3_EMBED_SPACE_TAG",),
        None,
        "The vector space rows are tagged with; decides search compatibility. Default: embed.model.",
    ),
    _str(
        "embed.gguf_model_tag",
        ("M3_EMBED_GGUF_MODEL_TAG",),
        None,
        "Tag of vectors from the in-process GGUF embedder. Default: embed.space_tag.",
    ),
    _str(
        "embed.fallback_model_tag",
        ("M3_EMBED_FALLBACK_MODEL_TAG",),
        None,
        "Tag of vectors from the fallback endpoint. Default: embed.model.",
    ),
    Key[list[str]](
        name="embed.compatible_models",
        parse=parse_list,
        default=[],
        env_names=("M3_EMBED_COMPATIBLE_MODELS",),
        reloadable=False,
        doc="Other model tags whose vectors share this space.",
    ),
    _int("embed.dim", ("M3_EMBED_DIM", "EMBED_DIM"), 1024, "Embedding dimension."),
    Key(
        name="embed.gguf",
        parse=parse_path,
        default=None,
        env_names=("M3_EMBED_GGUF",),
        reloadable=False,
        doc="The GGUF weights file. Unset: m3's models root, the bundled asset, then discovery "
        "(embed.autodetect). Not an env override.",
    ),
    Key[bool](
        name="embed.autodetect",
        parse=parse_bool,
        default=True,
        env_names=("M3_EMBED_GGUF_AUTODETECT",),
        reloadable=False,
        legacy_parse=lambda raw: raw != "0",  # today only "0" turned it off
        doc="Search the disk for a bge-m3 GGUF when embed.gguf is unset.",
    ),
    _float(
        "embed.autodetect_budget_s", ("M3_EMBED_GGUF_WALK_BUDGET",), 2.0, "Seconds the GGUF discovery walk may take."
    ),
    Key[bool](
        name="embed.inproc",
        parse=parse_bool,
        default=False,
        env_names=("M3_EMBED_INPROC",),
        reloadable=False,
        legacy_parse=lambda raw: raw == "1",  # today only "1" turned it on
        doc="Allow the in-process tier-1 embedder (opt-in, DESIGN_PHILOSOPHIES §8).",
    ),
    _float(
        "embed.init_timeout_s",
        ("M3_EMBED_INIT_TIMEOUT_S",),
        20.0,
        "Seconds to wait for the in-process embedder to load; 0 disables the limit.",
    ),
    # ── endpoints ──
    _str("embed.url", ("M3_EMBED_URL",), None, "Embedding endpoint override; unset uses the configured endpoints."),
    _str(
        "embed.fallback_url",
        ("M3_EMBED_FALLBACK_URL",),
        "http://127.0.0.1:8082",
        "The fallback embedding endpoint (the shared embed server).",
    ),
    Key(
        name="embed.fallback_ca",
        parse=parse_path,
        default=None,
        env_names=(),
        reloadable=False,
        doc="CA certificate (PEM) that verifies an https embed.fallback_url whose certificate is not "
        "publicly trusted, e.g. a private CA. A `tailscale cert` certificate needs none.",
    ),
    _float(
        "embed.discovery_negative_ttl_s",
        ("M3_EMBED_DISCOVERY_NEG_TTL",),
        60.0,
        "Seconds an endpoint that failed discovery is skipped.",
    ),
    _float(
        "embed.search_deadline_s",
        ("M3_EMBED_SEARCH_DEADLINE_S",),
        8.0,
        "Seconds a search waits for its query embedding; 0 disables the limit.",
    ),
    # ── vectors ──
    Key[bool](
        name="embed.require_unit_norm",
        parse=parse_bool,
        default=True,
        env_names=("M3_EMBED_REQUIRE_UNIT_NORM",),
        reloadable=False,
        legacy_parse=lambda raw: raw.lower() not in ("0", "false", "no"),
        doc="Reject vectors whose norm is not 1 within embed.norm_tolerance.",
    ),
    _float("embed.norm_tolerance", ("M3_EMBED_NORM_TOL",), 0.05, "Allowed deviation from unit norm."),
    # ── sizing ──
    _int(
        "embed.ctx",
        ("M3_EMBED_CTX",),
        8192,
        "Context length in tokens (also read by the Rust core, as a u32).",
        hi=2**32 - 1,
    ),
    # ── the in-process embedder (m3_core_rs.EmbeddedEmbedder parameters) ──
    # Defaults: m3-core-rs crates/m3-core-py/src/config.rs resolve(), which reads
    # m3_dispatcher DEFAULT_STREAMS = 8 and DEFAULT_COALESCE_WINDOW_MS = 3
    # (m3-dispatcher/src/lib.rs:85,87) and m3_embed_llamacpp DEFAULT_SEQ_MAX = 32
    # (m3-embed-llamacpp/src/lib.rs:73). The legacy M3_EMBED_* names alias THESE
    # keys, not [embed.server]: in an m3 process they only ever reached the
    # in-process core (core-py maps them the same way), while m3-embed-server
    # reads them in its own process, below m3_config.toml (embed-server config.rs).
    _int(
        "embed.streams",
        ("M3_EMBED_STREAMS",),
        8,
        "In-process embedder: worker contexts (each holds a ~4 GiB compute graph at ctx 8192).",
        hi=4096,
    ),
    _int(
        "embed.seq_max",
        ("M3_EMBED_SEQ_MAX",),
        32,
        "In-process embedder: sequences packed into one decode call.",
        hi=2**32 - 1,
    ),
    _int_derived(
        "embed.n_batch",
        ("M3_EMBED_N_BATCH",),
        "In-process embedder: llama.cpp batch tokens. Unset: embed.ctx (the only encoder-safe value).",
    ),
    _int_derived(
        "embed.n_ubatch",
        ("M3_EMBED_N_UBATCH",),
        "In-process embedder: llama.cpp micro-batch tokens. Unset: embed.ctx (the only encoder-safe value).",
    ),
    _int(
        "embed.coalesce_ms",
        ("M3_EMBED_COALESCE_MS",),
        3,
        "In-process embedder: milliseconds the dispatcher waits to coalesce requests into one batch.",
        lo=0,
        hi=2**32 - 1,
    ),
    _int_derived(
        "embed.max_batch_tokens",
        ("M3_EMBED_MAX_BATCH_TOKENS",),
        "In-process embedder: the dispatcher's coalescing ceiling in tokens. Unset: embed.streams x embed.ctx.",
    ),
    _int("embed.token_budget", ("M3_EMBED_TOKEN_BUDGET",), 7000, "Tokens per chunk."),
    _int("embed.token_overlap", ("M3_EMBED_TOKEN_OVERLAP",), 2000, "Token overlap between chunks.", lo=0),
    _int("embed.chunk_max_chars", ("M3_EMBED_CHUNK_MAX_CHARS",), 28000, "Characters per chunk."),
    _int(
        "embed.chunk_overlap_chars", ("M3_EMBED_CHUNK_OVERLAP_CHARS",), 8000, "Character overlap between chunks.", lo=0
    ),
    _int("embed.bulk_chunk", ("M3_EMBED_BULK_CHUNK", "EMBED_BULK_CHUNK"), 1024, "Texts per bulk embedding request."),
    _int(
        "embed.bulk_concurrency",
        ("M3_EMBED_BULK_CONCURRENCY", "EMBED_BULK_CONCURRENCY"),
        4,
        "Concurrent bulk embedding requests.",
    ),
    _int(
        "embed.entity_name_cache_max",
        ("M3_ENTITY_NAME_EMBED_CACHE_MAX", "ENTITY_NAME_EMBED_CACHE_MAX"),
        50000,
        "Entity-name embeddings kept in memory.",
    ),
    # ── [embed.http] ──
    _int("embed.http.max_connections", ("M3_EMBED_HTTP_MAX_CONNS",), 32, "HTTP connection pool size."),
    _int("embed.http.max_keepalive", ("M3_EMBED_HTTP_MAX_KEEPALIVE",), 16, "Kept-alive HTTP connections."),
    _float(
        "embed.http.keepalive_expiry_s",
        ("M3_EMBED_HTTP_KEEPALIVE_EXPIRY",),
        5.0,
        "Seconds an idle kept-alive connection lives.",
    ),
    # ── [embed.breaker]: per-tier circuit breakers ──
    *[
        k
        for tier, n, t in (("primary", 3, 60.0), ("embedded", 5, 30.0), ("cpu_fallback", 3, 30.0), ("cloud", 3, 60.0))
        for k in (
            _int(
                f"embed.breaker.{tier}_threshold",
                (f"M3_EMBED_BREAKER_{tier.upper()}_THRESHOLD",),
                n,
                f"Consecutive failures that open the {tier} tier's breaker.",
            ),
            _float(
                f"embed.breaker.{tier}_reset_s",
                (f"M3_EMBED_BREAKER_{tier.upper()}_RESET_SECS",),
                t,
                f"Seconds the {tier} tier's breaker stays open.",
            ),
        )
    ],
    # ── [embed.server]: the shared embed server ──
    _str(
        "embed.server.host",
        ("M3_EMBED_SERVER_HOST", "EMBED_SERVER_HOST"),
        "127.0.0.1",
        "Address the shared embed server binds (also read by the Rust server).",
    ),
    _int(
        "embed.server.port",
        ("M3_EMBED_SERVER_PORT",),
        8082,
        "Port of the shared embed server (also read by the Rust server, as a u16).",
        hi=65535,
    ),
    Key(
        name="embed.server.bin",
        parse=parse_path,
        default=None,
        env_names=("M3_EMBED_SERVER_BIN",),
        reloadable=False,
        doc="The m3-embed-server binary; unset uses the wheel's, then PATH.",
    ),
    # The Rust m3-embed-server reads the keys below from this file (embed-server
    # src/config.rs KEYS; defaults are its DEFAULT_* constants there). No m3 env
    # name: the legacy M3_EMBED_* names belong to the in-process keys above, and
    # the server honours them only in its own environment, below the file.
    _int_derived(
        "embed.server.streams",
        (),
        "m3-embed-server: worker contexts. Unset: 2 on a GPU build (cuda/vulkan/metal), 1 on a CPU-only "
        "build; each holds a ~4 GiB compute graph at ctx 8192.",
        hi=4096,
    ),
    _int("embed.server.seq_max", (), 32, "m3-embed-server: sequences packed into one decode call.", hi=2**32 - 1),
    _int_derived(
        "embed.server.n_batch",
        (),
        "m3-embed-server: llama.cpp batch tokens. Unset: embed.ctx (the only encoder-safe value); "
        "a value below embed.ctx is raised to it.",
    ),
    _int_derived(
        "embed.server.n_ubatch",
        (),
        "m3-embed-server: llama.cpp micro-batch tokens. Unset: embed.ctx (the only encoder-safe value); "
        "a value below embed.ctx is raised to it.",
    ),
    _int(
        "embed.server.coalesce_ms",
        (),
        3,
        "m3-embed-server: milliseconds the dispatcher waits to coalesce requests into one batch.",
        lo=0,
    ),
    _int("embed.server.max_batch_tokens", (), 2048, "m3-embed-server: the dispatcher's coalescing ceiling in tokens."),
    _str(
        "embed.server.remote_listen",
        (),
        None,
        "m3-embed-server: address (ip or ip:port) of the TLS listener for other machines; unset = loopback "
        "only. Set by `m3 embedder secure`; needs tls_cert, tls_key and the M3_EMBED_SERVER_TOKEN_SHA256 secret.",
    ),
    Key(
        name="embed.server.tls_cert",
        parse=parse_path,
        default=None,
        env_names=(),
        reloadable=False,
        doc="m3-embed-server: TLS certificate chain (PEM) for embed.server.remote_listen.",
    ),
    Key(
        name="embed.server.tls_key",
        parse=parse_path,
        default=None,
        env_names=(),
        reloadable=False,
        doc="m3-embed-server: private key (PEM) of embed.server.tls_cert; must be readable only by its owner.",
    ),
    _int("embed.server.max_batch", ("M3_EMBED_SERVER_MAX_BATCH",), 2048, "Texts per server batch."),
    _int(
        "embed.server.concurrency",
        ("M3_EMBED_SERVER_CONCURRENCY",),
        0,
        "Concurrent requests; 0 uses the backend's stream pool.",
        lo=0,
    ),
    _int(
        "embed.server.interactive_reserved",
        ("M3_EMBED_SERVER_INTERACTIVE_RESERVED",),
        1,
        "Slots reserved for interactive (search) requests.",
        lo=0,
    ),
    _int(
        "embed.server.interactive_max_texts",
        ("M3_EMBED_INTERACTIVE_MAX_TEXTS",),
        8,
        "Largest request still treated as interactive.",
    ),
    # ── [embed.gpu]: the llama-server proxy (embed_server_gpu.py) ──
    _str(
        "embed.gpu.host",
        ("M3_EMBED_SERVER_GPU_HOST", "EMBED_SERVER_GPU_HOST"),
        "127.0.0.1",
        "Address the GPU embed proxy binds.",
    ),
    _int("embed.gpu.llama_port", ("M3_LLAMA_PORT", "LLAMA_PORT"), 9904, "llama-server's port."),
    Key(
        name="embed.gpu.llama_server_exe",
        parse=parse_path,
        default=None,
        env_names=("M3_LLAMA_SERVER_EXE", "LLAMA_SERVER_EXE"),
        reloadable=False,
        doc="llama-server executable. Unset: ~/llama.cpp/bin/llama-server.exe.",
    ),
    Key(
        name="embed.gpu.model_path",
        parse=parse_path,
        default=None,
        env_names=("M3_GGUF_MODEL_PATH", "GGUF_MODEL_PATH"),
        reloadable=False,
        doc="GGUF file llama-server loads. Unset: models/qwen3-q8.gguf under the CURRENT "
        "directory (a working-directory-dependent default; 2D fixes it).",
    ),
    # ── [chatlog.embed]: the chatlog embed sweeper ──
    # No range check: 0 OR LESS means unbounded (chatlog_embed_sweeper.py:560).
    Key[float](
        name="chatlog.embed.deadline_s",
        parse=parse_float,
        default=60.0,
        env_names=("M3_CHATLOG_EMBED_DEADLINE_S", "CHATLOG_EMBED_DEADLINE_S"),
        reloadable=False,
        doc="Seconds one sweeper run may take; 0 or less is unbounded.",
    ),
    _int(
        "chatlog.embed.max_per_run",
        ("M3_CHATLOG_EMBED_MAX_PER_RUN", "CHATLOG_EMBED_MAX_PER_RUN"),
        10000,
        "Rows one sweeper run embeds.",
    ),
]

# Every key m3-embed-server reads from m3_config.toml (m3-core-rs
# crates/m3-embed-server/src/config.rs KEYS, less the secrets.file locator). A
# supervised server sees only the file, so `m3 embedder install` records each of
# these that this process has from anywhere else before registering it.
EMBED_SERVER_READS: tuple[str, ...] = (
    "embed.gguf",
    "embed.ctx",
    "embed.server.host",
    "embed.server.port",
    "embed.server.streams",
    "embed.server.seq_max",
    "embed.server.n_batch",
    "embed.server.n_ubatch",
    "embed.server.coalesce_ms",
    "embed.server.max_batch_tokens",
    "embed.server.remote_listen",
    "embed.server.tls_cert",
    "embed.server.tls_key",
    "crypto.fips_mode",
    "crypto.fips_strict",
)
# Of those, the ones every m3 process reads too: m3 never records one on the
# server's behalf (a migration or `install` must not switch m3's crypto mode).
EMBED_SERVER_M3_WIDE: frozenset[str] = frozenset({"crypto.fips_mode", "crypto.fips_strict"})
