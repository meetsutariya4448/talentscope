"""
Seeds a realistic-scale corpus for load testing — ~1,500 postings,
matching this app's actual production scale (README's own numbers), not
the artificially inflated 20k-row corpus scripts/db_engineering_report.py
uses to stress-test individual indexes in isolation. Load testing is
measuring end-to-end request handling capacity, which is a different
question from index behavior at scale, so it should run against a
realistic corpus size, not the largest one available.

Run inside the api container:
    docker compose exec -T api python k6/seed_for_load_test.py
"""
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.database import SessionLocal

N_POSTINGS = 1500
DIM = 384

ROLES = [
    "Backend Engineer", "Frontend Engineer", "Data Engineer", "Machine Learning Engineer",
    "DevOps Engineer", "Site Reliability Engineer", "Platform Engineer", "Data Scientist",
    "Full Stack Developer", "Cloud Engineer", "Security Engineer", "Mobile Engineer",
]
KEYWORDS = [
    "python", "kubernetes", "postgresql", "react", "golang", "aws", "terraform",
    "kafka", "spark", "docker", "typescript", "distributed systems", "microservices",
    "redis", "grpc", "graphql", "machine learning", "airflow", "snowflake",
]
LOCATIONS = ["Remote", "San Francisco, CA", "New York, NY", "Austin, TX", "Seattle, WA", "Boston, MA"]


def main():
    db = SessionLocal()
    try:
        existing = db.execute(text("SELECT COUNT(*) FROM postings WHERE source = 'loadtest'")).scalar()
        if existing and existing >= N_POSTINGS:
            print(f"Already seeded ({existing} loadtest postings) — skipping.")
            return

        db.execute(text(
            "INSERT INTO companies (name, slug) VALUES ('Load Test Co', 'load-test-co') "
            "ON CONFLICT (slug) DO NOTHING"
        ))
        company_id = db.execute(text("SELECT id FROM companies WHERE slug = 'load-test-co'")).scalar_one()

        rng = random.Random(7)
        now = datetime.utcnow()
        skill_ids = {
            row[0]: row[1]
            for row in db.execute(text("SELECT name, id FROM skills")).fetchall()
        }

        batch = []
        for i in range(N_POSTINGS):
            role = rng.choice(ROLES)
            kws = rng.sample(KEYWORDS, k=4)
            title = f"{role} - {kws[0].title()}"
            description = (
                f"We are looking for a {role} with experience in "
                f"{', '.join(kws)}. Join our team and work on distributed systems at scale."
            )
            location = rng.choice(LOCATIONS)
            posted_at = now - timedelta(days=rng.uniform(0, 90))
            vec = [rng.gauss(0, 1) for _ in range(DIM)]
            norm = sum(v * v for v in vec) ** 0.5
            vec = [v / norm for v in vec]
            vec_str = "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
            batch.append({
                "company_id": company_id, "title": title, "description": description,
                "location": location, "source": "loadtest", "source_id": f"loadtest-{i}",
                "currency": "USD", "embedding": vec_str, "posted_at": posted_at,
            })
            if len(batch) >= 500:
                _flush(db, batch)
                batch = []
        if batch:
            _flush(db, batch)
        db.commit()

        # Attach 2-4 skills per posting so the skill filter path (used by
        # some k6 scenarios) has real data to filter against too.
        posting_ids = db.execute(
            text("SELECT id FROM postings WHERE source = 'loadtest'")
        ).scalars().all()
        skill_id_list = list(skill_ids.values())
        for pid in posting_ids:
            for sid in rng.sample(skill_id_list, k=min(3, len(skill_id_list))):
                db.execute(
                    text(
                        "INSERT INTO posting_skills (posting_id, skill_id) VALUES (:p, :s) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"p": pid, "s": sid},
                )
        db.commit()
        db.execute(text("ANALYZE postings"))
        db.commit()
        print(f"Seeded {len(posting_ids)} postings with skills.")
    finally:
        db.close()


def _flush(db, batch):
    db.execute(
        text("""
            INSERT INTO postings
                (company_id, title, description, location, source, source_id, currency, embedding, posted_at)
            VALUES
                (:company_id, :title, :description, :location, :source, :source_id, :currency,
                 CAST(:embedding AS vector), :posted_at)
            ON CONFLICT (source, source_id) DO NOTHING
        """),
        batch,
    )


if __name__ == "__main__":
    main()
