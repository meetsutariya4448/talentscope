import httpx
from sqlalchemy.orm import Session
from app.tasks.celery_app import app as celery_app
from app.database import SessionLocal
from app.models import Company, Posting, Skill, PostingSkill
from app.ingestion.normalizer import normalize_adzuna
from app.ingestion.deduplicator import is_exact_duplicate
from app.ingestion.skills import SKILLS, extract_skills
from app.config import settings
from sqlalchemy import text
import logging

logger = logging.getLogger(__name__)

ADZUNA_BASE = (
    "https://api.adzuna.com/v1/api/jobs/us/search/{page}"
    "?app_id={app_id}&app_key={app_key}&results_per_page=50"
    "&what={query}&content-type=application/json"
)

ADZUNA_QUERIES = [
    "software engineer",
    "backend engineer",
    "frontend engineer",
    "data engineer",
    "machine learning engineer",
    "devops engineer",
    "full stack developer",
    "python developer",
    "data scientist",
    "cloud engineer",
]


def _get_or_create_company(db: Session, name: str) -> int:
    slug = name.lower().strip().replace(" ", "-")[:255]
    company = db.query(Company).filter_by(slug=slug).first()
    if not company:
        from app.models import Company as CompanyModel
        company = CompanyModel(name=name, slug=slug)
        db.add(company)
        db.flush()
    return company.id


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
    name="app.tasks.adzuna.fetch_adzuna",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    max_retries=3,
)
def fetch_adzuna(self, query: str, page: int = 1):
    if not settings.adzuna_app_id or not settings.adzuna_app_key:
        logger.warning("Adzuna credentials not configured, skipping")
        return {"skipped": True}

    url = ADZUNA_BASE.format(
        page=page,
        app_id=settings.adzuna_app_id,
        app_key=settings.adzuna_app_key,
        query=query.replace(" ", "%20"),
    )
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(url)
            resp.raise_for_status()
            results = resp.json().get("results", [])
    except httpx.HTTPError as e:
        logger.warning(f"Adzuna fetch failed for query '{query}' page {page}: {e}")
        raise

    db: Session = SessionLocal()
    inserted_ids: list[int] = []
    try:
        skill_map = _ensure_skills(db)
        for job in results:
            data = normalize_adzuna(job)
            if is_exact_duplicate(db, data["source"], data["source_id"]):
                continue

            company_name = data.pop("company_name", "") or "Unknown"
            data["company_id"] = _get_or_create_company(db, company_name)

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
        logger.info(f"Adzuna '{query}' p{page}: {len(inserted_ids)}/{len(results)} new postings")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    from app.tasks.embedding import embed_posting
    for pid in inserted_ids:
        embed_posting.delay(pid)

    return {"query": query, "page": page, "fetched": len(results), "inserted": len(inserted_ids)}
