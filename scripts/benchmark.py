#!/usr/bin/env python3
"""
Search latency benchmark for TalentScope.

Measures wall-clock p50/p95/p99 latency for FTS, vector, and hybrid search
against the live database (no HTTP overhead).  Results are saved to
evals/benchmark.json for traceability.

Usage:
    cd /path/to/talentscope
    python scripts/benchmark.py

Requirements:
    - DATABASE_URL set in .env or environment
    - At least 100 embedded postings in the DB
"""
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make the project root importable when run from any directory
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

REPEATS = 5      # per query per mode
WARMUP  = 2      # discarded warm-up runs (model load, plan cache)


def percentile(data: list[float], p: int) -> float:
    """p-th percentile of data (1 ≤ p ≤ 100)."""
    if len(data) < 2:
        return data[0] if data else 0.0
    sorted_d = sorted(data)
    idx = (p / 100) * (len(sorted_d) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_d) - 1)
    return sorted_d[lo] + (sorted_d[hi] - sorted_d[lo]) * (idx - lo)


def measure_fn(fn, *args, repeats: int = REPEATS, warmup: int = WARMUP) -> list[float]:
    """Run fn(*args) warmup+repeats times; discard first `warmup` timings."""
    times = []
    for i in range(warmup + repeats):
        t0 = time.perf_counter()
        fn(*args)
        elapsed_ms = (time.perf_counter() - t0) * 1_000
        if i >= warmup:
            times.append(elapsed_ms)
    return times


def summarize(times: list[float]) -> dict:
    return {
        "p50_ms":  round(percentile(times, 50), 1),
        "p95_ms":  round(percentile(times, 95), 1),
        "p99_ms":  round(percentile(times, 99), 1),
        "min_ms":  round(min(times), 1),
        "max_ms":  round(max(times), 1),
        "mean_ms": round(statistics.mean(times), 1),
        "n":       len(times),
    }


def run_hybrid(db, q: str) -> list[int]:
    from app.search.hybrid import fts_search, vector_search, reciprocal_rank_fusion, TOP_K
    fts_ids = fts_search(db, q, limit=TOP_K)
    vec_ids = vector_search(db, q, limit=TOP_K)
    return reciprocal_rank_fusion([fts_ids, vec_ids])


def run_embedding_throughput() -> dict:
    """Measure encoding speed: sentences/sec for all-MiniLM-L6-v2."""
    from app.search.encoder import get_model

    model = get_model()
    batch = QUERIES * 4          # 48 sentences
    # warm up
    model.encode(batch[:4], normalize_embeddings=True)

    t0 = time.perf_counter()
    model.encode(batch, normalize_embeddings=True)
    elapsed = time.perf_counter() - t0

    sps = round(len(batch) / elapsed, 1)
    ms_per = round(elapsed / len(batch) * 1_000, 2)
    return {"sentences": len(batch), "elapsed_s": round(elapsed, 3),
            "sentences_per_sec": sps, "ms_per_sentence": ms_per}


def main() -> None:
    from app.database import SessionLocal
    from app.search.hybrid import fts_search, vector_search, TOP_K

    print("TalentScope — search latency benchmark")
    print(f"  queries : {len(QUERIES)}")
    print(f"  repeats : {REPEATS}  (+ {WARMUP} warm-up, discarded)")
    print()

    # --- Embedding throughput (model warm-up happens here) ---
    print("Warming up encoder model…", end=" ", flush=True)
    embed_stats = run_embedding_throughput()
    print(f"{embed_stats['sentences_per_sec']} sentences/sec  "
          f"({embed_stats['ms_per_sentence']} ms each)\n")

    db = SessionLocal()

    try:
        mode_results: dict[str, dict] = {}

        for label, fn in [
            ("fts",    lambda q: fts_search(db, q, limit=TOP_K)),
            ("vector", lambda q: vector_search(db, q, limit=TOP_K)),
            ("hybrid", lambda q: run_hybrid(db, q)),
        ]:
            all_times: list[float] = []
            for q in QUERIES:
                all_times.extend(measure_fn(fn, q))

            stats = summarize(all_times)
            mode_results[label] = stats
            print(
                f"  {label:8s}  "
                f"p50={stats['p50_ms']:6.1f}ms  "
                f"p95={stats['p95_ms']:6.1f}ms  "
                f"p99={stats['p99_ms']:6.1f}ms  "
                f"(n={stats['n']})"
            )

    finally:
        db.close()

    out = {
        "run_at":    datetime.now(timezone.utc).isoformat(),
        "corpus":    _corpus_size(),
        "queries":   len(QUERIES),
        "repeats":   REPEATS,
        "warmup":    WARMUP,
        "embedding": embed_stats,
        "search":    mode_results,
    }

    dest = ROOT / "evals" / "benchmark.json"
    dest.write_text(json.dumps(out, indent=2))
    print(f"\nSaved → {dest}")


def _corpus_size() -> dict:
    from app.database import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    try:
        total    = db.execute(text("SELECT COUNT(*) FROM postings")).scalar()
        embedded = db.execute(text("SELECT COUNT(*) FROM postings WHERE embedding IS NOT NULL")).scalar()
        return {"total_postings": total, "embedded_postings": embedded}
    finally:
        db.close()


if __name__ == "__main__":
    main()
