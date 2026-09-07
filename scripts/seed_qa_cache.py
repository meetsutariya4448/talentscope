#!/usr/bin/env python3
"""Pre-compute answers for the demo's fixed question list into the Redis cache.

Makes the demo's headline questions instant and free: they are served from the
same `rag:v1:` cache app/search/rag.py already reads, so a demo Q&A costs no
provider call and cannot be affected by the daily budget running out or the
provider being unavailable.

Written with no TTL (the normal path writes a 1-hour expiry) because a demo
that quietly stops answering an hour in is exactly what "predictable demo"
rules out.

    docker compose exec -T api python scripts/seed_qa_cache.py          # dry-run
    docker compose exec -T api python scripts/seed_qa_cache.py --write  # spends budget

Requires GROQ_API_KEY. Each question is one billed call, once — that is the
whole point: pay a fixed, known number of calls up front instead of an
unbounded number during the demo.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings
from app.database import SessionLocal
from app.search.rag import CACHE_PREFIX, _cache_key, answer_question
from app.tasks.redis_utils import get_redis

# The questions the demo actually asks. Keep this list short and stable —
# every entry costs one provider call per reseed.
DEMO_QUESTIONS = [
    "Which companies are hiring backend engineers?",
    "What skills show up most often in data engineering roles?",
    "Which roles mention Kubernetes?",
    "What is the salary range for senior engineers?",
    "Which companies hire for remote positions?",
    "What machine learning roles are open?",
    "Which locations have the most openings?",
    "What does a platform engineer role involve here?",
]

DEFAULT_MODE = "hybrid"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true",
                        help="Actually call the provider and write the cache. "
                             "Without this, prints what would be done.")
    parser.add_argument("--mode", default=DEFAULT_MODE,
                        choices=["fts", "vector", "hybrid"])
    parser.add_argument("--overwrite", action="store_true",
                        help="Recompute entries that are already cached.")
    args = parser.parse_args()

    rc = get_redis()
    if rc is None:
        raise SystemExit("Redis unavailable — nothing to seed into.")

    if not settings.groq_api_key:
        raise SystemExit("GROQ_API_KEY is not set; cannot precompute answers.")

    existing = 0
    to_write = []
    for question in DEMO_QUESTIONS:
        key = _cache_key(question, args.mode)
        if rc.get(key) and not args.overwrite:
            existing += 1
            continue
        to_write.append((question, key))

    print(f"{len(DEMO_QUESTIONS)} demo questions | already cached: {existing} | "
          f"would call provider: {len(to_write)}")
    if not args.write:
        for q, _ in to_write:
            print(f"  would compute: {q}")
        print("\n(dry run — pass --write to spend that many provider calls)")
        return

    db = SessionLocal()
    written = 0
    try:
        for question, key in to_write:
            # llm_gate is deliberately omitted: seeding is an operator action
            # with a known, bounded call count, not public traffic, so it does
            # not draw down the public daily budget.
            result = answer_question(
                question=question,
                db=db,
                mode=args.mode,
                redis_client=None,   # bypass the read path; we write below
                groq_api_key=settings.groq_api_key,
            )
            if result.get("answer") is None:
                print(f"  FAILED: {question} -> {result.get('error') or result.get('degraded')}")
                continue
            payload = {
                "answer": result["answer"],
                "sources": result["sources"],
                "cited_ids": result.get("cited_ids", []),
                "cached": False,
                "model": result.get("model"),
                "latency_ms": result.get("latency_ms", 0),
            }
            # No expiry, unlike the request path's CACHE_TTL.
            rc.set(key, json.dumps(payload))
            written += 1
            print(f"  cached: {question}")
    finally:
        db.close()

    print(f"\nwrote {written} entries under {CACHE_PREFIX}* (no TTL)")


if __name__ == "__main__":
    main()
