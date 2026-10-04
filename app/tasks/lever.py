import httpx
from sqlalchemy.orm import Session
from app.tasks.celery_app import app as celery_app
from app.database import SessionLocal
from app.ingestion.normalizer import normalize_lever
from app.ingestion.provider_payloads import extract_job_list
from app.ingestion.ingest import ingest_posting
from app.ingestion.panel import record_company_check
from app.ingestion.skills import ensure_skills
import logging

logger = logging.getLogger(__name__)

LEVER_BASE = "https://api.lever.co/v0/postings/{slug}?mode=json&limit=500"


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
    http_status = None
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(url)
            http_status = resp.status_code
            resp.raise_for_status()
            jobs = extract_job_list(resp.json())
    except httpx.TimeoutException as e:
        logger.warning(f"Lever fetch timed out for {company_slug}: {e}")
        record_company_check(
            "lever", company_slug, status="timeout",
            http_status=http_status, postings_seen=0, error_detail=str(e),
        )
        raise
    except httpx.HTTPError as e:
        logger.warning(f"Lever fetch failed for {company_slug}: {e}")
        record_company_check(
            "lever", company_slug, status="http_error",
            http_status=http_status, postings_seen=0, error_detail=str(e),
        )
        raise
    except ValueError as e:
        logger.warning("Lever returned an invalid payload for %s: %s", company_slug, e)
        record_company_check(
            "lever", company_slug, status="http_error",
            http_status=http_status, postings_seen=0, error_detail=str(e),
        )
        raise

    db: Session = SessionLocal()
    inserted_ids: list[int] = []
    changed_ids: list[int] = []
    try:
        skill_map = ensure_skills(db)
        for job in jobs:
            data = normalize_lever(job, company_id)
            result = ingest_posting(db, data, skill_map, company_token=company_slug)
            if result.is_new:
                inserted_ids.append(result.posting_id)
            elif result.content_changed:
                changed_ids.append(result.posting_id)
        db.commit()
        logger.info(
            f"Lever {company_slug}: {len(inserted_ids)} new, "
            f"{len(changed_ids)} updated (of {len(jobs)} fetched)"
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    record_company_check(
        "lever", company_slug,
        status="ok" if jobs else "empty",
        http_status=http_status, postings_seen=len(jobs), error_detail=None,
    )

    from app.tasks.embedding import embed_posting
    for pid in inserted_ids + changed_ids:
        embed_posting.delay(pid)

    return {
        "company_slug": company_slug, "fetched": len(jobs),
        "inserted": len(inserted_ids), "updated": len(changed_ids),
    }
