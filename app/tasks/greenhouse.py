import httpx
from sqlalchemy.orm import Session
from app.tasks.celery_app import app as celery_app
from app.database import SessionLocal
from app.models import Skill
from app.ingestion.normalizer import normalize_greenhouse
from app.ingestion.ingest import ingest_posting
from app.ingestion.panel import record_company_check
from app.ingestion.skills import SKILLS
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
    http_status = None
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(url)
            http_status = resp.status_code
            resp.raise_for_status()
            jobs = resp.json().get("jobs", [])
    except httpx.TimeoutException as e:
        logger.warning(f"Greenhouse fetch timed out for {board_token}: {e}")
        record_company_check(
            "greenhouse", board_token, status="timeout",
            http_status=http_status, postings_seen=0, error_detail=str(e),
        )
        raise
    except httpx.HTTPError as e:
        logger.warning(f"Greenhouse fetch failed for {board_token}: {e}")
        record_company_check(
            "greenhouse", board_token, status="http_error",
            http_status=http_status, postings_seen=0, error_detail=str(e),
        )
        raise

    db: Session = SessionLocal()
    inserted_ids: list[int] = []
    changed_ids: list[int] = []
    try:
        skill_map = _ensure_skills(db)
        for job in jobs:
            data = normalize_greenhouse(job, company_id)
            result = ingest_posting(db, data, skill_map, company_token=board_token)
            if result.is_new:
                inserted_ids.append(result.posting_id)
            elif result.content_changed:
                changed_ids.append(result.posting_id)
        db.commit()
        logger.info(
            f"Greenhouse {board_token}: {len(inserted_ids)} new, "
            f"{len(changed_ids)} updated (of {len(jobs)} fetched)"
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    record_company_check(
        "greenhouse", board_token,
        status="ok" if jobs else "empty",
        http_status=http_status, postings_seen=len(jobs), error_detail=None,
    )

    # Dispatch embedding tasks after commit so postings are guaranteed visible.
    # Re-embed on content change too — an edited title/description with a
    # stale embedding is a search index quietly drifting from reality.
    from app.tasks.embedding import embed_posting
    for pid in inserted_ids + changed_ids:
        embed_posting.delay(pid)

    return {
        "board_token": board_token, "fetched": len(jobs),
        "inserted": len(inserted_ids), "updated": len(changed_ids),
    }
