import httpx
from sqlalchemy.orm import Session
from app.tasks.celery_app import app as celery_app
from app.database import SessionLocal
from app.models import Posting, Skill, PostingSkill
from app.ingestion.normalizer import normalize_lever
from app.ingestion.deduplicator import is_exact_duplicate, find_fuzzy_duplicate
from app.ingestion.skills import SKILLS, extract_skills
from sqlalchemy import text
import logging

logger = logging.getLogger(__name__)

LEVER_BASE = "https://api.lever.co/v0/postings/{slug}?mode=json&limit=500"


def _ensure_skills(db):
    skill_map = {}
    for skill_name, category in SKILLS:
        skill = db.query(Skill).filter_by(name=skill_name).first()
        if not skill:
            skill = Skill(name=skill_name, category=category)
            db.add(skill)
            db.flush()
        skill_map[skill_name] = skill.id
    return skill_map


@celery_app.task(
    name="app.tasks.lever.fetch_lever",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    max_retries=3,
)
def fetch_lever(self, company_slug: str, company_id: int):
    url = LEVER_BASE.format(slug=company_slug)
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(url)
            resp.raise_for_status()
            jobs = resp.json()
            if not isinstance(jobs, list):
                jobs = []
    except httpx.HTTPError as e:
        logger.warning(f"Lever fetch failed for {company_slug}: {e}")
        raise

    db: Session = SessionLocal()
    inserted_ids: list[int] = []
    try:
        skill_map = _ensure_skills(db)
        for job in jobs:
            data = normalize_lever(job, company_id)
            if is_exact_duplicate(db, data["source"], data["source_id"]):
                continue
            if find_fuzzy_duplicate(db, data.get("company_id"), data["title"], data.get("location", "")):
                continue

            posting = Posting(**{k: v for k, v in data.items() if hasattr(Posting, k)})
            db.add(posting)
            db.flush()

            desc = (data.get("title", "") + " " + (data.get("description") or ""))
            for skill_name in extract_skills(desc):
                if skill_name in skill_map:
                    db.add(PostingSkill(posting_id=posting.id, skill_id=skill_map[skill_name]))

            db.execute(
                text(
                    "UPDATE postings SET search_vector = to_tsvector('english', "
                    "coalesce(title,'') || ' ' || coalesce(description,'') || ' ' || coalesce(location,'')) "
                    "WHERE id = :id"
                ),
                {"id": posting.id},
            )
            inserted_ids.append(posting.id)
        db.commit()
        logger.info(f"Lever {company_slug}: {len(inserted_ids)}/{len(jobs)} new postings")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    from app.tasks.embedding import embed_posting
    for pid in inserted_ids:
        embed_posting.delay(pid)

    return {"company_slug": company_slug, "fetched": len(jobs), "inserted": len(inserted_ids)}
