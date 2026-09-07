#!/usr/bin/env python3
"""Seed the fixed demo corpus from evals/fixtures/demo_postings.json.

A predictable demo needs a corpus that is byte-identical everywhere, so this
reads a committed fixture rather than generating rows. It also runs every
posting through the real ingestion path — app/ingestion/ingest.py's
ingest_posting() — instead of inserting rows directly, so the demo data has the
same dedup handling, panel fields (first_seen_at/last_seen_at/description_hash,
snapshot rows) and extracted skills that production data has. Rows written
straight to the table would look right in a search result and be wrong
everywhere the panel or skill joins are involved.

Embeddings are computed locally by the embedding worker; nothing here calls a
paid API.

    docker compose exec -T api python scripts/seed_demo.py [--embed]

--embed dispatches embed_posting for each seeded row (needs a running worker).
Without it the hourly embed_missing_postings backfill will pick them up.
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.database import SessionLocal
from app.ingestion.ingest import ingest_posting
from app.ingestion.skills import SKILLS
from app.models import Company, Skill

FIXTURE = ROOT / "evals" / "fixtures" / "demo_postings.json"


def ensure_skills(db) -> dict[str, int]:
    """Same shape as the ingestion tasks' _ensure_skills."""
    skill_map = {}
    for name, category in SKILLS:
        skill = db.query(Skill).filter_by(name=name).first()
        if not skill:
            skill = Skill(name=name, category=category)
            db.add(skill)
            db.flush()
        skill_map[name] = skill.id
    return skill_map


def _slugify(name: str) -> str:
    """Company.slug is NOT NULL and unique, so it has to be derived here."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def ensure_company(db, name: str, cache: dict[str, int]) -> int:
    if name in cache:
        return cache[name]
    slug = _slugify(name)
    # Look up by slug, not name: slug carries the unique constraint, so a
    # name-only lookup can miss an existing row and then fail on insert.
    company = db.execute(
        select(Company).where(Company.slug == slug)
    ).scalar_one_or_none()
    if company is None:
        company = Company(name=name, slug=slug)
        db.add(company)
        db.flush()
    cache[name] = company.id
    return company.id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--embed", action="store_true",
                        help="Dispatch embed_posting for each seeded posting.")
    args = parser.parse_args()

    if not FIXTURE.exists():
        raise SystemExit(
            f"{FIXTURE} is missing — regenerate it with "
            "python scripts/build_demo_fixture.py"
        )

    fixture = json.loads(FIXTURE.read_text())
    postings = fixture["postings"]
    print(f"Seeding {len(postings)} demo postings (fixture seed={fixture['seed']})")

    db = SessionLocal()
    seeded_ids: list[int] = []
    inserted = updated = skipped = 0
    try:
        skill_map = ensure_skills(db)
        company_cache: dict[str, int] = {}

        for row in postings:
            data = dict(row)
            company_name = data.pop("company_name")
            data["company_id"] = ensure_company(db, company_name, company_cache)
            data["posted_at"] = datetime.fromisoformat(data["posted_at"])

            result = ingest_posting(db, data, skill_map, company_token=None)
            if result.skipped:
                # Cross-source fuzzy duplicate — nothing was written.
                skipped += 1
                continue
            if result.posting_id is not None:
                seeded_ids.append(result.posting_id)
            if result.is_new:
                inserted += 1
            else:
                updated += 1

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print(f"  inserted={inserted} updated={updated} skipped_as_duplicate={skipped}")

    if args.embed:
        from app.tasks.embedding import embed_posting
        for pid in seeded_ids:
            embed_posting.delay(pid)
        print(f"  dispatched {len(seeded_ids)} embed_posting tasks")
    else:
        print("  (run with --embed to embed now, or wait for the hourly backfill)")


if __name__ == "__main__":
    main()
