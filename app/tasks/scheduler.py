import math
import logging

from app.tasks.celery_app import app as celery_app
from app.tasks.greenhouse import fetch_greenhouse
from app.tasks.lever import fetch_lever
from app.tasks.ashby import fetch_ashby
from app.tasks.adzuna import fetch_adzuna, ADZUNA_QUERIES
from app.tasks.redis_utils import get_redis as _get_redis
from app.database import SessionLocal
from app.models import Company
from app.ingestion.company_registry import load_target_companies, sync_monitored_companies

logger = logging.getLogger(__name__)

CURSOR_NS = "talentscope:scheduler:cursor"

# Target company lists now live in config/target_companies.yml (the editable
# source of truth, synced into `monitored_companies` on worker/beat startup —
# see sync_monitored_companies_from_yaml below), not hardcoded here.
_TARGET_COMPANIES = load_target_companies()
GREENHOUSE_COMPANIES = [(c["token"], c.get("name", c["token"])) for c in _TARGET_COMPANIES.get("greenhouse", [])]
LEVER_COMPANIES = [(c["token"], c.get("name", c["token"])) for c in _TARGET_COMPANIES.get("lever", [])]
ASHBY_COMPANIES = [(c["token"], c.get("name", c["token"])) for c in _TARGET_COMPANIES.get("ashby", [])]

BATCH_SIZE = 10


def sync_monitored_companies_from_yaml() -> None:
    """Called on worker_ready / beat_init (see app.tasks.celery_app) so the
    database — not just the YAML file — has each company's monitoring
    start/stop timestamps."""
    db = SessionLocal()
    try:
        sync_monitored_companies(db, load_target_companies())
    finally:
        db.close()


def _next_batch(redis_client, key: str, items: list, batch_size: int) -> list:
    """
    Atomically advance the batch cursor in Redis and return the next slice.

    Uses INCR (atomic, no read-modify-write) so concurrent or duplicate beat
    firings cannot produce the same batch twice.  Steps a batch counter rather
    than a company index, which means batch boundaries are stable across cycles.
    """
    n_batches = math.ceil(len(items) / batch_size)
    batch_no = (redis_client.incr(key) - 1) % n_batches
    start = batch_no * batch_size
    return items[start:start + batch_size]


def _get_or_create_company(db, name: str, slug: str) -> int:
    company = db.query(Company).filter_by(slug=slug).first()
    if not company:
        company = Company(name=name, slug=slug)
        db.add(company)
        db.commit()
        db.refresh(company)
    return company.id


def _dispatch_batch(source: str, cursor_key: str, companies: list, fetch_task) -> dict:
    rc = _get_redis()
    if rc is None:
        logger.warning("Redis unavailable — falling back to batch 0 for %s", source)
        batch = companies[:BATCH_SIZE]
    else:
        try:
            batch = _next_batch(rc, cursor_key, companies, BATCH_SIZE)
        except Exception as exc:
            logger.warning("Redis error selecting %s batch, falling back to batch 0: %s", source, exc)
            batch = companies[:BATCH_SIZE]

    db = SessionLocal()
    try:
        for token, name in batch:
            company_id = _get_or_create_company(db, name, token)
            fetch_task.delay(token, company_id)
    finally:
        db.close()

    logger.info("Dispatched %s batch: %s", source, [t for t, _ in batch])
    return {"dispatched": [t for t, _ in batch]}


@celery_app.task(name="app.tasks.scheduler.dispatch_greenhouse_batch")
def dispatch_greenhouse_batch():
    return _dispatch_batch("greenhouse", f"{CURSOR_NS}:greenhouse", GREENHOUSE_COMPANIES, fetch_greenhouse)


@celery_app.task(name="app.tasks.scheduler.dispatch_lever_batch")
def dispatch_lever_batch():
    return _dispatch_batch("lever", f"{CURSOR_NS}:lever", LEVER_COMPANIES, fetch_lever)


@celery_app.task(name="app.tasks.scheduler.dispatch_ashby_batch")
def dispatch_ashby_batch():
    return _dispatch_batch("ashby", f"{CURSOR_NS}:ashby", ASHBY_COMPANIES, fetch_ashby)


@celery_app.task(name="app.tasks.scheduler.dispatch_adzuna_batch")
def dispatch_adzuna_batch():
    for query in ADZUNA_QUERIES:
        for page in range(1, 4):  # 3 pages * 50 results = 150 per query
            fetch_adzuna.delay(query, page)
    logger.info("Dispatched Adzuna batch: %d queries x 3 pages", len(ADZUNA_QUERIES))
    return {"dispatched": len(ADZUNA_QUERIES) * 3}
