#!/usr/bin/env python3
"""Generate evals/fixtures/demo_postings.json — the fixed demo corpus.

Run once (or when the demo corpus should deliberately change) and commit the
result. The demo itself reads the committed JSON via scripts/seed_demo.py; it
does not re-generate, because a corpus regenerated per environment is not a
predictable demo.

Deterministic: a fixed seed, and sorted iteration everywhere a set or dict
could otherwise leak hash ordering into the output.
"""
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "evals" / "fixtures" / "demo_postings.json"

SEED = 20260906
N_POSTINGS = 240

COMPANIES = [
    "Northwind Systems", "Lumen Analytics", "Harbor Logistics", "Cobalt Health",
    "Meridian Robotics", "Alder Financial", "Solstice Media", "Vantage Retail",
    "Ironwood Security", "Blue Delta Energy",
]

ROLES = [
    ("Backend Engineer", ["python", "postgresql", "docker", "microservices"]),
    ("Frontend Engineer", ["react", "typescript", "graphql"]),
    ("Data Engineer", ["spark", "kafka", "airflow", "snowflake"]),
    ("Machine Learning Engineer", ["python", "machine learning", "kubernetes"]),
    ("DevOps Engineer", ["kubernetes", "terraform", "aws", "docker"]),
    ("Site Reliability Engineer", ["kubernetes", "aws", "distributed systems"]),
    ("Platform Engineer", ["golang", "kubernetes", "grpc"]),
    ("Data Scientist", ["python", "machine learning", "postgresql"]),
    ("Security Engineer", ["aws", "kubernetes", "distributed systems"]),
    ("Mobile Engineer", ["typescript", "react"]),
]

LOCATIONS = [
    "Remote", "San Francisco, CA", "New York, NY", "Austin, TX",
    "Seattle, WA", "Boston, MA", "Chicago, IL", "Denver, CO",
]

LEVELS = ["Junior", "", "Senior", "Staff", "Principal"]

DESCRIPTION = (
    "{company} is hiring a {level_title} to work on {focus}. "
    "You will build and operate {focus_detail} using {skills}. "
    "We care about clear writing, measurable reliability, and shipping "
    "changes that are easy to reverse. Compensation is {salary_text}. "
    "Location: {location}."
)

FOCUS = [
    ("large-scale data pipelines", "batch and streaming ingestion"),
    ("the core search platform", "ranking and retrieval services"),
    ("customer-facing web products", "the front-end application stack"),
    ("internal developer tooling", "CI/CD and deployment automation"),
    ("real-time analytics", "aggregation and query services"),
]


def main() -> None:
    rng = random.Random(SEED)
    base_date = datetime(2026, 9, 1, tzinfo=timezone.utc)
    postings = []

    for i in range(N_POSTINGS):
        company = COMPANIES[i % len(COMPANIES)]
        role, skills = ROLES[rng.randrange(len(ROLES))]
        level = LEVELS[rng.randrange(len(LEVELS))]
        location = LOCATIONS[rng.randrange(len(LOCATIONS))]
        focus, focus_detail = FOCUS[rng.randrange(len(FOCUS))]

        title = f"{level} {role}".strip()
        salary_min = rng.randrange(110, 190) * 1000
        salary_max = salary_min + rng.randrange(20, 70) * 1000
        posted_at = base_date - timedelta(days=rng.randrange(0, 90))

        postings.append({
            "company_name": company,
            "title": title,
            "location": location,
            "description": DESCRIPTION.format(
                company=company,
                level_title=title,
                focus=focus,
                focus_detail=focus_detail,
                skills=", ".join(skills),
                salary_text=f"${salary_min:,}-${salary_max:,}",
                location=location,
            ),
            "salary_min": float(salary_min),
            "salary_max": float(salary_max),
            "currency": "USD",
            "source": "demo",
            "source_id": f"demo-{i:04d}",
            "url": f"https://example.invalid/jobs/demo-{i:04d}",
            "posted_at": posted_at.isoformat(),
        })

    DEST.parent.mkdir(parents=True, exist_ok=True)
    DEST.write_text(json.dumps(
        {
            "generated_by": "scripts/build_demo_fixture.py",
            "seed": SEED,
            "count": len(postings),
            "postings": postings,
        },
        indent=2,
        sort_keys=True,
    ) + "\n")
    print(f"wrote {len(postings)} postings → {DEST}")


if __name__ == "__main__":
    main()
