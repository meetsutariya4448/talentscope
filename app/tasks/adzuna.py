import hashlib
import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from app.tasks.celery_app import app as celery_app
from app.database import SessionLocal
from app.models import Company
from app.ingestion.normalizer import normalize_adzuna
from app.ingestion.provider_payloads import extract_job_list
from app.ingestion.ingest import ingest_posting
from app.ingestion.skills import ensure_skills
from app.config import settings
import logging

logger = logging.getLogger(__name__)

ADZUNA_BASE = (
    "https://api.adzuna.com/v1/api/jobs/us/search/{page}"
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
    # Adzuna occasionally omits the display name or returns padding-only
    # text. Normalize it before both identity comparisons and persistence so
    # those records cannot create a blank company/slug or duplicate a company
    # whose only difference is provider whitespace.
    name = name.strip() or "Unknown"
    base_slug = name.lower().replace(" ", "-")[:255]
    company_id = _insert_company(db, name, base_slug)
    if company_id is not None:
        return company_id

    existing_id, existing_name = db.execute(
        select(Company.id, Company.name).where(Company.slug == base_slug)
    ).one()
    if existing_name == name:
        return existing_id

    # Names such as "A B" and "A-B" share the readable base slug. Preserve
    # that existing company and give the distinct name a deterministic suffix
    # instead of silently attaching its postings to the wrong company.
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    suffix = f"-{digest}"
    collision_slug = f"{base_slug[:255 - len(suffix)]}{suffix}"
    company_id = _insert_company(db, name, collision_slug)
    if company_id is not None:
        return company_id

    return db.execute(
        select(Company.id).where(
            Company.slug == collision_slug,
            Company.name == name,
        )
    ).scalar_one()


def _insert_company(db: Session, name: str, slug: str) -> int | None:
    statement = (
        pg_insert(Company)
        .values(name=name, slug=slug)
        .on_conflict_do_nothing(index_elements=[Company.slug])
        .returning(Company.id)
    )
    return db.execute(statement).scalar_one_or_none()


def _fetch_results(query: str, page: int) -> list[dict]:
    """Fetch one page without leaking query-string credentials on failure."""
    try:
        with httpx.Client(timeout=30) as client:
            response = client.get(
                ADZUNA_BASE.format(page=page),
                params={
                    "app_id": settings.adzuna_app_id,
                    "app_key": settings.adzuna_app_key,
                    "results_per_page": 50,
                    "what": query,
                    "content-type": "application/json",
                },
            )
            response.raise_for_status()
            return extract_job_list(response.json(), key="results")
    except httpx.HTTPError as error:
        detail = (
            f"HTTP {error.response.status_code}"
            if isinstance(error, httpx.HTTPStatusError)
            else type(error).__name__
        )
        logger.warning(
            "Adzuna fetch failed for query %r page %s (%s)", query, page, detail
        )
        # HTTPX exception text includes the full URL, including app_key.
        # Celery records the raised exception after retries are exhausted, so
        # replace it with a useful but credential-free failure.
        raise RuntimeError(f"Adzuna request failed ({detail})") from None


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

    results = _fetch_results(query, page)

    db: Session = SessionLocal()
    inserted_ids: list[int] = []
    changed_ids: list[int] = []
    try:
        skill_map = ensure_skills(db)
        for job in results:
            data = normalize_adzuna(job)
            company_name = data.pop("company_name", "") or "Unknown"
            data["company_id"] = _get_or_create_company(db, company_name)

            # Adzuna is a search index, not a company's own board: no company_token,
            # so left_truncated is always true and it never drives disappeared_at
            # (see app.ingestion.panel.AUTHORITATIVE_SOURCES).
            result = ingest_posting(db, data, skill_map, company_token=None)
            if result.is_new:
                inserted_ids.append(result.posting_id)
            elif result.content_changed:
                changed_ids.append(result.posting_id)
        db.commit()
        logger.info(
            f"Adzuna '{query}' p{page}: {len(inserted_ids)} new, "
            f"{len(changed_ids)} updated (of {len(results)} fetched)"
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    from app.tasks.embedding import embed_posting
    for pid in inserted_ids + changed_ids:
        embed_posting.delay(pid)

    return {
        "query": query, "page": page, "fetched": len(results),
        "inserted": len(inserted_ids), "updated": len(changed_ids),
    }
