#!/usr/bin/env python3
"""Generate sustained ingestion-side *inference* load, for CPU-budget testing.

Clears the embedding on N postings and dispatches one embed_posting task per
posting, so the Celery worker genuinely runs the sentence-transformers model.
That is the contention worth measuring: queueing tasks that only touch Postgres
would exercise the database, not the CPU budget shared between request-path
inference and ingestion-path inference.

Run from inside the api container (the repo is bind-mounted at /app):

    docker compose exec -T api python scripts/ingest_load.py 1400

Destructive by design — it nulls embeddings that the worker then recomputes.
Point it at a disposable corpus (k6/seed_for_load_test.py), never at data you
care about.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.database import SessionLocal
from app.tasks.embedding import embed_posting


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 800

    db = SessionLocal()
    try:
        ids = [
            r[0]
            for r in db.execute(
                text("SELECT id FROM postings ORDER BY id LIMIT :n"), {"n": n}
            ).all()
        ]
        if not ids:
            print("no postings found — seed a corpus first "
                  "(python k6/seed_for_load_test.py)")
            return
        db.execute(
            text("UPDATE postings SET embedding = NULL WHERE id = ANY(:ids)"),
            {"ids": ids},
        )
        db.commit()
    finally:
        db.close()

    for pid in ids:
        embed_posting.delay(pid)

    print(f"dispatched {len(ids)} embed_posting tasks over {len(ids)} postings")


if __name__ == "__main__":
    main()
