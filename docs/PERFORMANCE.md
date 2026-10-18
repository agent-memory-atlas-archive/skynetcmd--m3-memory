# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> Performance

Measured latency for the paths you actually wait on. Every number here was
produced by a harness you can run yourself, and every one is qualified by the
machine it came from.

> **Read the hardware line before the numbers.** These were measured on a fast
> desktop with a GPU-accelerated embedder. They are a *reference point*, not a
> guarantee, and not a floor — a laptop, a small VPS, or a CPU-only install will
> be slower, in some cases by a lot. Where a number is hardware-independent, it
> says so.

## The measurement host

| | |
|---|---|
| CPU | AMD Ryzen 7 9800X3D — 8C/8T, 4.7 GHz |
| GPU | NVIDIA RTX 5080 — the embedder runs the **CUDA** build |
| RAM | 31 GB (8.8 GB free at measurement time) |
| OS / Python | Windows 11 · Python 3.14.6 |
| `m3_core_rs` | 3.7.31 (native wheel present) |
| Machine state | **Stock Windows install**, no tuning — 398 processes, editor, browser and a WSL VM all running |
| m3 state | **nothing hitting m3 concurrently** — no second agent, no loop pass, no sync |

This is consumer hardware — a stock Windows machine with no performance tuning,
no tweaked power plan, running everything a working desktop runs. But it is
*fast* consumer hardware: a current-generation X3D desktop part with a pro-sumer
GPU. No other process was contending for m3's own paths, so nothing here is
distorted by concurrent m3 work.

**The single biggest variable is whether you have a GPU** — and we measured both
sides of that, below.

## Writes: the deferred path is 14.5× faster

| Path | p50 | p95 |
|---|---|---|
| write, embedding inline | 31.40 ms | 33.39 ms |
| write, embedding deferred | **2.16 ms** | 3.66 ms |

The whole gap is the embed call. A write that does not embed — validate,
hash, store — is about **2 ms**. (The write-path contradiction check needs the
vector, so it runs only on inline-embedded writes.)

This is what the zero-lag design buys, and it is an architectural result rather
than a hardware one: when no fast embedder is reachable, m3 persists the row
verbatim and defers the vector to the [Cognitive
Loop](ARCHITECTURE.md#-the-cognitive-loop) instead of making the caller wait.
The row is full-text searchable immediately either way, and vector search picks
it up once the `embed` pass has run.

Single-threaded, that is roughly **32 writes/second** with inline embedding and
**460/second** deferred.

## Sync: `bulk_upsert` is 24.6× faster than row-at-a-time

3,000-row upsert to a live PostgreSQL warehouse, median of 10 runs:

| | p50 | p95 |
|---|---|---|
| `bulk_upsert` (`execute_values`) | **25.0 ms** | 28.4 ms |
| `cursor.executemany` | 614.5 ms | 670.8 ms |

**This one is largely hardware-independent** — it is a round-trip count, not a
CPU speed. psycopg2's `executemany` issues the statement once per row, so 3,000
rows is 3,000 round trips; `execute_values` collapses each 1,000-row page into a
single statement. On a WAN link to a remote warehouse the gap *widens*, because
it multiplies by network latency.

It is also why `bulk_upsert` exists as a
[storage-seam primitive](ARCHITECTURE.md#-extension-seams) rather than each
backend improvising: a correct-but-row-at-a-time port would have been 25× slower
and still passed every correctness test.

## Search

| Stage | p50 | p95 |
|---|---|---|
| hybrid k=10 — FTS5+BM25, vector, fuse, MMR, ranking | 45.67 ms | 48.56 ms |
| cross-encoder rerank (opt-in), added to the above | 3.71 ms | 4.61 ms |

Search cost used to grow with store size — 485 ms p50 at 7,500 rows. It no
longer does:

| Rows | before | after |
|---|---|---|
| 2,500 | 104.5 ms | **1.3 ms** |
| 5,000 | 251.0 ms | **1.6 ms** |
| 7,500 | 484.9 ms | **2.0 ms** |

**Near-flat across store size**, measured on a corpus of real prose — a ~0.7 ms
drift across a 3× growth in the store, against 380 ms before.

⚠ Those are *selective* queries against real text. A pathological corpus where
every row shares the same vocabulary is slower and still grows: measured on
synthetic filler, 27.5 ms at 200 rows rising to 71.8 ms at 7,500. That is a
17× improvement on the same corpus (it was 1,212 ms), but it is not flat, and
a store of near-identical documents will behave more like that column. The fix was to
stop ranking `bm25()` inside a join: it must score every FTS match before the
`LIMIT` applies, so the joins were being carried across the whole match set.
Ranking on the FTS table alone in a CTE, then joining only the survivors, cut
the internal FTS index lookups for one search from **1,080,000 to 36**.

Queries that cannot short-circuit on an exact lexical match take the full hybrid
path and cost more — **~95 ms p50** on the same store, of which ~15 ms is the
embed call and the rest is the candidate fetch running twice (hybrid falls back
to a semantic pass when full-text finds nothing). That is the honest worst case
for a natural-language query.

One number worth explaining: the fastest observed query was **0.80 ms**, ~50×
faster than the median. That is the FTS short-circuit — when a query has an
exact lexical match, m3 skips the vector side entirely. It is real, but it
describes a specific case, not the general one.

### Search time by store size

Search is exact on both backends: every stored vector is scored. These are the times
that costs, measured at each size, for the default hybrid search with `k=8`:

| Memories stored | SQLite | PostgreSQL | PostgreSQL with pgvector |
|---|---|---|---|
| 50,000 | 0.45 s | 0.88 s | 0.27 s |
| 100,000 | 0.97 s | 1.70 s | 0.35 s |
| 150,000 | 1.55 s | 2.54 s | 0.46 s |

Vector-only search at the same sizes: SQLite 0.41, 0.85 and 1.32 s; PostgreSQL 0.86,
1.67 and 2.50 s; PostgreSQL with pgvector 0.26, 0.34 and 0.45 s.

Those stores hold one vector per memory. **Real stores** hold about two (a long memory
is embedded in windows), and search time follows the number of vectors:

| Real store (a copy) | Memories | Vectors | Hybrid search |
|---|---|---|---|
| SQLite: the developer's main store | 83,149 | 180,749 | 2.73 s |
| SQLite: the developer's chat log | 90,157 | 176,883 | 2.07 s |
| PostgreSQL: the developer's warehouse | 90,456 | 180,078 | 1.63 s; 0.54 s with pgvector |

Turning the query into a vector adds about 20 ms with the shared embedder.

Measured 2026-10-14 on an Apple M3 Max, PostgreSQL 18.6 with pgvector 0.8 on the same
machine, 1024-dimension vectors, through m3's own write and search code. Each size ran
three rounds of 10 queries: the first round warms the caches and is not counted, and
each figure is the mean of the other two rounds' medians (they agreed within 1%). The
sized stores hold random vectors, since an exact scan costs the same whatever the
values; the real-store queries were real memories' titles, searched with that memory's
own stored vector. The embedding call is excluded throughout. Every search returned
results. On the Windows measurement host, the developer's 160,924-memory chat log
answered real queries with the real embedder in 0.97 s p50 (hybrid), faster than its
vector search (1.83 s) because a query with an exact keyword match skips the vector scan.

### PostgreSQL with pgvector

When the server has the pgvector extension, m3 keeps a `vector` copy of each embedding
(migration `pg_066`) and, once every row has its copy, PostgreSQL scores the query with
`<=>` and returns only the top results. It is still exact: the same rows, in the same
order, with scores equal to the Rust path's within 1e-5 (checked by
`tests/test_vector_search_pgvector_parity_pg_live.py`). The difference is where the
scoring runs, which matters most when the database is across a network: without
pgvector, every stored vector (4 KB each at 1024 dimensions) travels to m3.

| 50,000 memories, vector stage | without pgvector | with pgvector | |
|---|---|---|---|
| m3 on the database machine | 638 ms | 201 ms | 3.2× |
| m3 across a network | 10.3 s | 0.20 s | 51× |

Measured 2026-10-14 on the Apple M3 Max, PostgreSQL 18.6 with pgvector 0.8, 1024-dimension
vectors, median of 5 queries, `k=8` (`k=2,000`, what the hybrid search requests, costs
the same within 10%). The network row ran from the Windows measurement host through an
SSH tunnel on the local network, which slows the bulk transfer; a direct connection is
faster; with pgvector the time does not depend on the link, since only the top
results cross it. These time the vector stage alone,
through the backend's two search paths; full search adds the hydrate and ranking steps
measured above.

Filling the copy for rows written before `pg_066` (in the background right after the
upgrade, or by `m3 doctor --fix` and the cognitive loop) ran at about 2,000 rows/s: 25 s for 50,000, and 93 s for the 180,078 vectors of the
real warehouse copy, in committed batches of 1,000 that each took 0.5 s, so no long
lock is held.

### SQLite: many agents writing at once

SQLite allows one writer at a time; others wait for the lock (30 s busy timeout).
Because a write is short, the wait is too. N separate processes, as real agents are,
wrote 40 memories each to one SQLite store at once:

| Writers | per-write p50 | p95 | slowest | failed |
|---|---|---|---|---|
| 1 | 0.6 ms | 0.7 ms | 32 ms | 0 of 40 |
| 2 | 0.6 ms | 0.7 ms | 62 ms | 0 of 80 |
| 4 | 0.6 ms | 5.1 ms | 118 ms | 0 of 160 |
| 8 | 0.6 ms | 10.5 ms | 259 ms | 0 of 320 |
| 16 | 0.6 ms | 29.0 ms | 491 ms | 0 of 640 |

Every write landed. Measured 2026-10-13 on an Apple M3 Max with the embedder stubbed, so
the figures are the database write and its locking; an inline embedding adds ~30 ms that
runs outside the lock.

**The busy timeout does not cover every wait.** A transaction that starts *deferred*,
reads, and then writes holds a read snapshot; if another process commits first, SQLite
cannot upgrade it to a writer and returns `SQLITE_BUSY` at once — `busy_timeout` does not
apply to that upgrade. With a bulk import or maintenance pass running alongside the
writers, 7 of 40 writes failed this way. Since 2026.10.17.3 every m3 SQLite connection
begins write transactions `IMMEDIATE` (`bin/sqlite_pragmas.py`), taking the write lock at
`BEGIN`, where the busy timeout does apply: 0 of 40 failed on the same run. Measure it with
`python bin/bench_concurrent_writes.py` and its `--ingest` option (an import, a maintenance
pass, or both alongside the writers).

## Embedding, and the shared-vs-in-process question

m3's default is a **shared embed server** on `127.0.0.1:8082` — one model in RAM
for every m3 process. **In-process** embedding (llama.cpp linked into the calling
process, no IPC) is opt-in and loads its own copy per process.

Embedding a 300 KB corpus, 698 paragraphs, one at a time through each topology:

| Paragraph size | n | shared | in-process |
|---|---|---|---|
| small (<200 chars) | 284 | 30.1 ms | **16.0 ms** |
| medium (200–2k) | 395 | 32.2 ms | **19.0 ms** |
| large (>2k) | 19 | **45.3 ms** | 67.2 ms |
| whole corpus (wall clock) | 698 | 22.6 s | **19.5 s** |

**In-process is faster on real text** — about 1.9× on short chunks and 1.75× on
medium ones, which together are 679 of the 698 chunks measured. Only the largest
chunks favour the shared server.

### Without a GPU, expect ~7× slower — and the choice above stops mattering

The same corpus with the GPU taken out of play:

| | shared (GPU) | in-process (GPU) | shared (CPU) | in-process (CPU) |
|---|---|---|---|---|
| small (n=284) | 30.1 ms | **16.0 ms** | 68.1 ms | 57.6 ms |
| medium (n=395) | 32.2 ms | **19.0 ms** | 186.2 ms | 177.5 ms |
| large (n=19) | 45.3 ms | 67.2 ms | 1,464.6 ms | 1,437.6 ms |
| whole corpus | 22.6 s | **19.5 s** | 157.9 s | 152.5 s |

**The GPU is worth about 7× on this corpus**, whichever topology you choose.
That dwarfs everything else on this page, and it is the number to plan around:
if you are sizing a CPU-only box, use the right-hand columns, not the headline
ones.

**On CPU the two topologies converge** — 157.9 s vs 152.5 s, a 3% difference.
With the GPU gone, the model's forward pass dominates so completely that the
IPC hop stops being measurable. So the shared-vs-in-process choice above is a
GPU-only consideration; on CPU, pick shared and save the RAM.

Ranked by what actually moves embed latency here:

1. **GPU vs CPU** — ~7×
2. **Chunk size** — ~25× between short and long paragraphs in one configuration
3. **Shared vs in-process** — ~1.2× on GPU, ~1.03× on CPU

Two honest caveats on the tier table specifically:

- **It rests on one machine**, with a CUDA embedder. This is the most
  hardware-sensitive result on this page.
- **The 19-sample row is thin.** An earlier run of the same comparison, with only
  5 large samples, showed in-process losing 6–8× and reversed the overall
  verdict. Widening the corpus corrected it. Read that row with less confidence
  than the others.

**So why is shared still the default?** Because latency is not the trade — memory
is. In-process loads one model copy *per process*, and a typical setup runs
several (MCP server, cognitive loop, CLI). Shared keeps one model in RAM for all
of them. Choose in-process when you have RAM to spare and a short-text workload;
choose shared — the default — otherwise.

## The Rust core

Separately from the above, m3 ships a Rust compute core (`m3_core_rs`) that
accelerates MMR re-ranking, batch cosine, and FTS compilation. The gain is
per-operation, not one multiplier: on the packed production paths, MMR re-ranking
is ~700–850× and batch cosine ~97–178× faster, while FTS query compilation is
~2.7–3×. It is installed by default, and the pure-Python fallback is results-equivalent —
it changes speed, never answers. See
[Oxidation Benchmarks](OXIDATION_BENCHMARKS.md).

## Reproducing this

The harness is not shipped in the public repo (it captures host-specific
timings, which is not something to publish as a product claim). The method is:

- `time.perf_counter`, untimed warm-up, then N timed iterations
- **p50 and p95, never a mean** — a mean hides the slow tail, which is the part
  that matters on a write path
- a throwaway store, so no real memory is touched
- each measurement reports its own `n`

Three traps worth knowing if you build your own, all of which produced a
confident wrong answer here first:

1. **m3's write and search entry points are coroutines.** Timing the call
   without awaiting it measures coroutine *construction*: 40 "writes" at
   p50 0.00 ms, an excellent number for code that never ran.
2. **`embed.inproc = true` (`m3 config set embed.inproc true`) does not by itself enable in-process embedding.** A
   present `.embed_config.json` takes precedence. A probe that ignores this
   measures the shared server twice and reports the two topologies as identical.
3. **Small buckets lie.** The 5-sample result above was reproducible and wrong.

---

*Retrieval accuracy — the metric that measures the memory layer itself — is in
[Benchmarks](../README.md#-benchmarks).*
