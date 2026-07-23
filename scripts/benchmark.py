#!/usr/bin/env python3
"""
Search latency benchmark for TalentScope.

Measures wall-clock p50/p95/p99 latency for FTS, vector, and hybrid search
against the live database (no HTTP overhead).  Results are saved to
evals/benchmark.json for traceability.

Usage:
    cd /path/to/talentscope
    python scripts/benchmark.py [--repeats N]

Requirements:
    - DATABASE_URL set in .env or environment
    - At least 100 embedded postings in the DB
"""
import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

QUERIES = [
    "software engineer python backend",
    "machine learning pytorch deep learning",
    "frontend engineer react typescript",
    "backend Go microservices distributed",
    "data engineer spark kafka pipeline",
    "devops kubernetes terraform cloud",
    "mobile iOS swift developer",
    "security engineer penetration testing",
    "full stack node javascript",
    "data scientist SQL pandas",
    "platform engineer reliability SRE",
    "Android Kotlin mobile engineer",
]

DEFAULT_REPEATS = 50
WARMUP          = 2     # discarded per-query warm-up runs (model load, plan cache)


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def percentile(data: list[float], p: int) -> float:
    """p-th percentile of sorted data (1 ≤ p ≤ 100), linear interpolation."""
    if len(data) < 2:
        return data[0] if data else 0.0
    s = sorted(data)
    idx = (p / 100) * (len(s) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def summarize(times: list[float]) -> dict:
    return {
        "p50_ms":  round(percentile(times, 50), 1),
        "p95_ms":  round(percentile(times, 95), 1),
        "p99_ms":  round(percentile(times, 99), 1),
        "min_ms":  round(min(times), 1),
        "max_ms":  round(max(times), 1),
        "mean_ms": round(statistics.mean(times), 1),
        "stdev_ms": round(statistics.stdev(times), 1) if len(times) > 1 else 0.0,
        "n":       len(times),
    }


# ---------------------------------------------------------------------------
# Measurement loop
# ---------------------------------------------------------------------------

def measure_fn(fn, *args, repeats: int, warmup: int = WARMUP) -> list[float]:
    """Run fn(*args) warmup+repeats times; return only the post-warmup timings."""
    times = []
    for i in range(warmup + repeats):
        t0 = time.perf_counter()
        fn(*args)
        elapsed_ms = (time.perf_counter() - t0) * 1_000
        if i >= warmup:
            times.append(elapsed_ms)
    return times


def run_hybrid(db, q: str) -> list[int]:
    from app.search.hybrid import fts_search, vector_search, reciprocal_rank_fusion, TOP_K
    fts_ids = fts_search(db, q, limit=TOP_K)
    vec_ids = vector_search(db, q, limit=TOP_K)
    return reciprocal_rank_fusion([fts_ids, vec_ids])


# ---------------------------------------------------------------------------
# Embedding timing
# ---------------------------------------------------------------------------

def run_embedding_timing() -> dict:
    """
    Two measurements for the encoder:
      - single_query_ms : encode 1 sentence (what vector_search does per request)
      - batch_sps       : throughput encoding 48 sentences as one batch
    Both run after the model is loaded (warm).
    """
    from app.search.encoder import get_model

    model = get_model()

    # Warm up
    model.encode(QUERIES[:2], normalize_embeddings=True)

    # Single-query latency (mirrors production vector_search call)
    single_times = []
    for _ in range(50):
        t0 = time.perf_counter()
        model.encode(QUERIES[0], normalize_embeddings=True)
        single_times.append((time.perf_counter() - t0) * 1_000)

    # Batch throughput (mirrors embedding backfill task)
    batch = QUERIES * 4   # 48 sentences
    t0 = time.perf_counter()
    model.encode(batch, normalize_embeddings=True)
    batch_elapsed = time.perf_counter() - t0

    return {
        "single_query": {
            "p50_ms":  round(percentile(single_times, 50), 2),
            "p95_ms":  round(percentile(single_times, 95), 2),
            "mean_ms": round(statistics.mean(single_times), 2),
        },
        "batch": {
            "n_sentences":       len(batch),
            "elapsed_s":         round(batch_elapsed, 3),
            "sentences_per_sec": round(len(batch) / batch_elapsed, 1),
            "ms_per_sentence":   round(batch_elapsed / len(batch) * 1_000, 2),
        },
    }


# ---------------------------------------------------------------------------
# Environment fingerprint
# ---------------------------------------------------------------------------

def collect_env() -> dict:
    cpu = "unknown"
    try:
        cpu = subprocess.check_output(
            ["sysctl", "-n", "machdep.cpu.brand_string"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        cpu = platform.processor() or platform.machine()

    mem_gb = 0
    try:
        mem_bytes = int(
            subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], stderr=subprocess.DEVNULL
            ).decode().strip()
        )
        mem_gb = round(mem_bytes / (1024 ** 3))
    except Exception:
        pass

    logical_cores = 0
    try:
        logical_cores = int(
            subprocess.check_output(
                ["sysctl", "-n", "hw.logicalcpu"], stderr=subprocess.DEVNULL
            ).decode().strip()
        )
    except Exception:
        pass

    return {
        "cpu":           cpu,
        "logical_cores": logical_cores,
        "ram_gb":        mem_gb,
        "os":            f"{platform.system()} {platform.release()}",
        "python":        platform.python_version(),
        "note":          "local dev machine — not a production environment",
    }


# ---------------------------------------------------------------------------
# Corpus stats
# ---------------------------------------------------------------------------

def corpus_size() -> dict:
    from app.database import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    try:
        total    = db.execute(text("SELECT COUNT(*) FROM postings")).scalar()
        embedded = db.execute(text("SELECT COUNT(*) FROM postings WHERE embedding IS NOT NULL")).scalar()
        return {"total_postings": total, "embedded_postings": embedded}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS,
                        help=f"Valid samples per query per mode (default {DEFAULT_REPEATS})")
    args = parser.parse_args()
    repeats = args.repeats
    n_total = len(QUERIES) * repeats

    print("TalentScope — search latency benchmark")
    print(f"  queries : {len(QUERIES)}")
    print(f"  repeats : {repeats}  (+ {WARMUP} warm-up per query, discarded)")
    print(f"  samples : {n_total} per mode")
    print()

    from app.database import SessionLocal
    from app.search.hybrid import fts_search, vector_search, TOP_K

    # Model warm-up + embedding timing (must happen before search loops so
    # the encoder singleton is loaded and cached for all subsequent vector calls)
    print("Loading encoder + measuring embedding latency…", end=" ", flush=True)
    embed = run_embedding_timing()
    print(
        f"single-query p50={embed['single_query']['p50_ms']} ms  "
        f"| batch {embed['batch']['sentences_per_sec']} sent/s\n"
    )

    db = SessionLocal()
    mode_results: dict[str, dict] = {}

    try:
        for label, fn in [
            ("fts",    lambda q: fts_search(db, q, limit=TOP_K)),
            ("vector", lambda q: vector_search(db, q, limit=TOP_K)),
            ("hybrid", lambda q: run_hybrid(db, q)),
        ]:
            all_times: list[float] = []
            for q in QUERIES:
                all_times.extend(measure_fn(fn, q, repeats=repeats))

            stats = summarize(all_times)
            mode_results[label] = stats
            print(
                f"  {label:8s}  "
                f"p50={stats['p50_ms']:6.1f} ms  "
                f"p95={stats['p95_ms']:6.1f} ms  "
                f"p99={stats['p99_ms']:6.1f} ms  "
                f"σ={stats['stdev_ms']:5.1f} ms  "
                f"(n={stats['n']})"
            )
    finally:
        db.close()

    env = collect_env()
    print(f"\nEnvironment: {env['cpu']}  |  {env['ram_gb']} GB RAM  |  {env['os']}")

    out = {
        "run_at":    datetime.now(timezone.utc).isoformat(),
        "corpus":    corpus_size(),
        "config":    {"queries": len(QUERIES), "repeats": repeats, "warmup": WARMUP},
        "env":       env,
        "embedding": embed,
        "search":    mode_results,
    }

    dest = ROOT / "evals" / "benchmark.json"
    dest.write_text(json.dumps(out, indent=2))
    print(f"Saved → {dest}")


if __name__ == "__main__":
    main()
