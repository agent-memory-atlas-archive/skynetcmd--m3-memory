#!/usr/bin/env python3
"""bench_concurrent_writes.py — many agents writing to one SQLite store at once,
optionally while an ingestion pipeline writes to the same store.

Usage:
    python bin/bench_concurrent_writes.py                      # 1,4,8,16 writers, no ingestion
    python bin/bench_concurrent_writes.py --ingest import      # bulk import running alongside
    python bin/bench_concurrent_writes.py --ingest maintenance # decay/purge passes alongside
    python bin/bench_concurrent_writes.py --writers 32 --per-writer 100 --ingest both --json

Each writer is a separate OS process, as real agents are, writing through m3's own
write path (memory_write, not raw SQL), embedding off so the figures are the
database write and its locking. The ingestion process runs m3's own long write
transactions (memory_import of a batch, memory_maintenance) in a loop on the same
store for the whole run. Everything happens under a throwaway config root whose
engine, logs and memory roots are temp directories: the developer's store is never
touched.

Reported per run: per-write latency p50/p95/max, writes failed, and the longest
ingestion transaction (the bound on how long any writer can be made to wait).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable

HERE = os.path.dirname(os.path.abspath(__file__))


def _write_config(root: str) -> dict[str, str]:
    """A config root whose every root is under ``root``; returns the child env."""
    cfg = os.path.join(root, "config")
    os.makedirs(cfg, exist_ok=True)
    paths = {k: os.path.join(root, k).replace("\\", "/") for k in ("engine", "logs", "memory")}
    for p in paths.values():
        os.makedirs(p, exist_ok=True)
    with open(os.path.join(cfg, "m3_config.toml"), "w", encoding="utf-8", newline="\n") as f:
        f.write(
            "[paths]\n"
            f'engine_root = "{paths["engine"]}"\n'
            f'logs_root = "{paths["logs"]}"\n'
            '[db]\nbackend = "sqlite"\n'
            # The measurement is the database lock, not crypto: a shell that still
            # exports M3_FIPS_MODE would otherwise demand wolfSSL under the temp root.
            "[crypto]\nfips_mode = false\nfips_strict = false\n"
        )
    # No inherited M3_* name reaches a child: a leftover export (an engine root, a
    # DSN, FIPS mode) must not steer the run back to the developer's store. The
    # memory root (where the audit trail lives) has no config-file key, so it is
    # pinned to the temp tree with its legacy name.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("M3_")}
    env["M3_CONFIG_ROOT"] = cfg
    env["M3_MEMORY_ROOT"] = paths["memory"]
    return env


def _run(coro_or_value):
    return asyncio.run(coro_or_value) if asyncio.iscoroutine(coro_or_value) else coro_or_value


def _role_writer(n: int, start_at: float, tag: str) -> None:
    sys.path.insert(0, HERE)
    import memory_core as mc

    _run(mc.memory_write_impl(type="note", content="warm-up", title="warm", embed=False))
    while time.time() < start_at:
        time.sleep(0.001)
    lat, failed, errors = [], 0, []
    for i in range(n):
        t = time.perf_counter()
        try:
            r = _run(
                mc.memory_write_impl(type="note", content=f"{tag} write {i} {uuid.uuid4()}", title=tag, embed=False)
            )
            if not str(r).startswith("Created"):
                failed += 1
                errors.append(str(r)[:200])
        except Exception as e:  # noqa: BLE001 — a failed write is the measurement
            failed += 1
            errors.append(f"{type(e).__name__}: {e}"[:200])
        lat.append((time.perf_counter() - t) * 1000)
    print(json.dumps({"lat_ms": lat, "failed": failed, "errors": errors[:3]}))


def _role_ingest(kind: str, stop_file: str, batch: int) -> None:
    sys.path.insert(0, HERE)
    import memory_core as mc
    import memory_maintenance as mm

    txn_ms, rounds, errors = [], 0, []
    _run(mc.memory_write_impl(type="note", content="ingest warm-up", title="warm", embed=False))
    print("READY", flush=True)
    while not os.path.exists(stop_file):
        steps: list[tuple[str, Callable[[], object]]] = []
        if kind in ("import", "both"):
            items = [
                {"id": str(uuid.uuid4()), "type": "note", "title": "ingested", "content": f"ingested row {k} " * 20}
                for k in range(batch)
            ]
            payload = json.dumps({"items": items})

            def _import(payload: str = payload) -> object:
                return mm.memory_import_impl(payload)

            steps.append(("memory_import", _import))
        if kind in ("maintenance", "both"):
            steps.append(
                (
                    "memory_maintenance",
                    lambda: mm.memory_maintenance_impl(decay=True, purge_expired=True, prune_orphan_embeddings=True),
                )
            )
        for name, step in steps:
            t = time.perf_counter()
            try:
                _run(step())
            except Exception as e:  # noqa: BLE001 — a failed ingestion step is the measurement
                import traceback

                frames = [f for f in traceback.extract_tb(e.__traceback__) if "bin" in f.filename]
                where = f"{os.path.basename(frames[-1].filename)}:{frames[-1].lineno}" if frames else "?"
                errors.append(
                    f"{name} after {(time.perf_counter() - t) * 1000:.0f} ms at {where}: {type(e).__name__}: {e}"[:300]
                )
            txn_ms.append((time.perf_counter() - t) * 1000)
        rounds += 1
    print(
        json.dumps({"txn_ms": txn_ms, "rounds": rounds, "errors": errors[:3], "error_count": len(errors)}), flush=True
    )


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(q * (len(xs) - 1)))] if xs else 0.0


def _one_run(writers: int, per_writer: int, ingest: str, batch: int) -> dict:
    root = tempfile.mkdtemp(prefix="m3-walbench-")
    env = _write_config(root)
    stop_file = os.path.join(root, "stop")
    me = os.path.abspath(__file__)
    ing = None
    try:
        if ingest != "none":
            ing = subprocess.Popen(
                [
                    sys.executable,
                    me,
                    "--role",
                    "ingest",
                    "--ingest",
                    ingest,
                    "--stop-file",
                    stop_file,
                    "--batch",
                    str(batch),
                ],
                env=env,
                stdout=subprocess.PIPE,
                text=True,
            )
            assert ing.stdout is not None
            seen = []
            for line in ing.stdout:  # m3 may log to stdout before the marker
                if line.strip() == "READY":
                    break
                seen.append(line.rstrip())
            else:
                raise RuntimeError(f"ingestion process exited ({ing.wait()}) before READY; it printed: {seen[-5:]}")
        start_at = time.time() + 3.0  # every writer imports m3 first, then starts together
        procs = [
            subprocess.Popen(
                [
                    sys.executable,
                    me,
                    "--role",
                    "writer",
                    "--per-writer",
                    str(per_writer),
                    "--start-at",
                    str(start_at),
                    "--tag",
                    f"w{i}",
                ],
                env=env,
                stdout=subprocess.PIPE,
                text=True,
            )
            for i in range(writers)
        ]
        results = []
        for p in procs:
            out, _ = p.communicate(timeout=600)
            results.append(json.loads(out.strip().splitlines()[-1]))
        ing_result = {}
        if ing is not None:
            open(stop_file, "w").close()
            out, _ = ing.communicate(timeout=600)
            ing_result = json.loads(out.strip().splitlines()[-1])
        lat = [x for r in results for x in r["lat_ms"]]
        return {
            "writers": writers,
            "writes": len(lat),
            "failed": sum(r["failed"] for r in results),
            "errors": [e for r in results for e in r["errors"]][:3],
            "p50_ms": round(statistics.median(lat), 1),
            "p95_ms": round(_pct(lat, 0.95), 1),
            "max_ms": round(max(lat), 1),
            "ingest": ingest,
            "ingest_txns": len(ing_result.get("txn_ms", [])),
            "ingest_longest_ms": round(max(ing_result.get("txn_ms", [0.0])), 1),
            "ingest_errors": ing_result.get("errors", []),
        }
    finally:
        if ing is not None and ing.poll() is None:
            ing.kill()
        shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--writers", default="1,4,8,16", help="comma-separated writer counts")
    ap.add_argument("--per-writer", type=int, default=40, help="writes per writer")
    ap.add_argument("--ingest", choices=("none", "import", "maintenance", "both"), default="none")
    ap.add_argument("--batch", type=int, default=500, help="rows per memory_import transaction")
    ap.add_argument("--json", action="store_true", help="print results as JSON")
    ap.add_argument("--role", choices=("writer", "ingest"), help=argparse.SUPPRESS)
    ap.add_argument("--start-at", type=float, help=argparse.SUPPRESS)
    ap.add_argument("--tag", default="w", help=argparse.SUPPRESS)
    ap.add_argument("--stop-file", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.role == "writer":
        _role_writer(a.per_writer, a.start_at, a.tag)
        return 0
    if a.role == "ingest":
        _role_ingest(a.ingest, a.stop_file, a.batch)
        return 0
    rows = [_one_run(int(n), a.per_writer, a.ingest, a.batch) for n in a.writers.split(",")]
    if a.json:
        print(json.dumps(rows, indent=2))
    else:
        print(f"ingestion alongside: {a.ingest}")
        print("| Writers | per-write p50 | p95 | slowest | failed | longest ingestion txn |")
        print("|---|---|---|---|---|---|")
        for r in rows:
            longest = f"{r['ingest_longest_ms']} ms ({r['ingest_txns']} txns)" if r["ingest"] != "none" else "—"
            print(
                f"| {r['writers']} | {r['p50_ms']} ms | {r['p95_ms']} ms | {r['max_ms']} ms | "
                f"{r['failed']} of {r['writes']} | {longest} |"
            )
        for r in rows:
            if r["errors"] or r["ingest_errors"]:
                print(f"errors at {r['writers']} writers: {r['errors'] + r['ingest_errors']}")
    return 1 if any(r["failed"] or r["ingest_errors"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
