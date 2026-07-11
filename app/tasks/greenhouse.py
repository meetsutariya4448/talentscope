import httpx
from sqlalchemy.orm import Session
from app.tasks.celery_app import app as celery_app
from app.database import SessionLocal
from app.models import Company, Posting, Skill, PostingSkill
from app.ingestion.normalizer import normalize_greenhouse
from app.ingestion.deduplicator import is_exact_duplicate, find_fuzzy_duplicate
from app.ingestion.skills import SKILLS, extract_skills
import logging

logger = logging.getLogger(__name__)

GREENHOUSE_BASE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def _ensure_skills(db: Session) -> dict[str, int]:
    """Ensure all known skills exist in DB and return name->id map."""
    skill_map = {}
    for skill_name, category in SKILLS:
        skill = db.query(Skill).filter_by(name=skill_name).first()
        if not skill:
            skill = Skill(name=skill_name, category=category)
            db.add(skill)
            db.flush()
        skill_map[skill_name] = skill.id
    return skill_map


def _upsert_posting(db: Session, data: dict, skill_map: dict[str, int]) -> bool:
    """Insert posting if not duplicate. Returns True if inserted."""
    if is_exact_duplicate(db, data["source"], data["source_id"]):
        return False

    fuzzy_id = find_fuzzy_duplicate(db, data.get("company_id"), data["title"], data.get("location", ""))
    if fuzzy_id:
        return False

    posting = Posting(
        company_id=data["company_id"],
        title=data["title"],
        location=data.get("location"),
        description=data.get("description"),
        salary_min=data.get("salary_min"),
        salary_max=data.get("salary_max"),
        currency=data.get("currency", "USD"),
        source=data["source"],
        source_id=data["source_id"],
        url=data.get("url"),
        posted_at=data.get("posted_at"),
    )
    db.add(posting)
    db.flush()

    # Extract and link skills
    desc = (data.get("title", "") + " " + (data.get("description") or ""))
    found_skills = extract_skills(desc)
    for skill_name in found_skills:
        if skill_name in skill_map:
            db.add(PostingSkill(posting_id=posting.id, skill_id=skill_map[skill_name]))

    # Update search_vector via raw SQL
    from sqlalchemy import text
    db.execute(
        text(
            "UPDATE postings SET search_vector = to_tsvector('english', "
            "coalesce(title,'') || ' ' || coalesce(description,'') || ' ' || coalesce(location,'')) "
            "WHERE id = :id"
        ),
        {"id": posting.id},
    )
    return True


@celery_app.task(
    name="app.tasks.greenhouse.fetch_greenhouse",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    max_retries=3,
)
def fetch_greenhouse(self, board_token: str, company_id: int):
    """Fetch all jobs from a Greenhouse board and upsert into DB."""
    url = GREENHOUSE_BASE.format(token=board_token)
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(url)
            resp.raise_for_status()
            jobs = resp.json().get("jobs", [])
    except httpx.HTTPError as e:
        logger.warning(f"Greenhouse fetch failed for {board_token}: {e}")
        raise

    db: Session = SessionLocal()
    inserted = 0
    try:
        skill_map = _ensure_skills(db)
        for job in jobs:
            data = normalize_greenhouse(job, company_id)
            if _upsert_posting(db, data, skill_map):
                inserted += 1
        db.commit()
        logger.info(f"Greenhouse {board_token}: {inserted}/{len(jobs)} new postings")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    return {"board_token": board_token, "fetched": len(jobs), "inserted": inserted}
