#!/usr/bin/env python3
"""
Probe vector-mode latency outliers.

Replays the exact benchmark parameters (12 queries × 50 repeats, 2 warm-up
discarded) and records every individual timing with its query index and
position within the repeat sequence.  Reports all samples >50 ms and
checks for clustering by query or run position.
"""
import sys
import time
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

REPEATS = 50
WARMUP  = 2
OUTLIER_THRESHOLD_MS = 50.0


def main() -> None:
    from app.database import SessionLocal
    from app.search.hybrid import vector_search, TOP_K
    from app.search.encoder import get_model

    # Warm up encoder (mirrors benchmark warm-up path)
    print("Loading encoder…", end=" ", flush=True)
    get_model().encode(QUERIES[:2], normalize_embeddings=True)
    print("ready.\n")

    db = SessionLocal()

    # records: (query_idx, query_str, repeat_idx_in_valid_sequence, latency_ms)
    all_samples: list[tuple[int, str, int, float]] = []

    try:
        for qi, q in enumerate(QUERIES):
            valid_idx = 0
            for rep in range(WARMUP + REPEATS):
                t0 = time.perf_counter()
                vector_search(db, q, limit=TOP_K)
                ms = (time.perf_counter() - t0) * 1_000
                if rep >= WARMUP:
                    all_samples.append((qi, q, valid_idx, ms))
                    valid_idx += 1
    finally:
        db.close()

    total = len(all_samples)
    outliers = [(qi, q, pos, ms) for qi, q, pos, ms in all_samples if ms > OUTLIER_THRESHOLD_MS]

    print(f"Total samples : {total}  ({len(QUERIES)} queries × {REPEATS} repeats)")
    print(f"Outliers >50ms: {len(outliers)}  ({100*len(outliers)/total:.1f}%)\n")

    if not outliers:
        print("No outliers found.")
        return

    print(f"{'Query':45s}  {'qi':>3}  {'pos':>4}  {'ms':>7}")
    print("-" * 70)
    for qi, q, pos, ms in sorted(outliers, key=lambda x: -x[3]):
        print(f"{q[:45]:45s}  {qi:>3}  {pos:>4}  {ms:>7.1f}")

    # --- Query clustering ---
    from collections import Counter
    by_query: Counter = Counter(qi for qi, _, _, _ in outliers)
    print("\nOutliers by query index:")
    for qi, count in sorted(by_query.items()):
        print(f"  q{qi:02d} ({QUERIES[qi][:35]:35s})  {count:3d} outlier(s)")

    # --- Position distribution ---
    positions = [pos for _, _, pos, _ in outliers]
    early  = sum(1 for p in positions if p < 10)
    middle = sum(1 for p in positions if 10 <= p < 40)
    late   = sum(1 for p in positions if p >= 40)
    print(f"\nOutliers by position in repeat sequence (0-indexed):")
    print(f"  early  (pos  0– 9): {early}")
    print(f"  middle (pos 10–39): {middle}")
    print(f"  late   (pos 40–49): {late}")
    print(f"\n  All positions: {sorted(positions)}")


if __name__ == "__main__":
    main()
