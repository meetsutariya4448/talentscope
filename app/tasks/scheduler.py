import math
import logging
import redis as redis_lib

from app.tasks.celery_app import app as celery_app
from app.tasks.greenhouse import fetch_greenhouse
from app.tasks.lever import fetch_lever
from app.tasks.adzuna import fetch_adzuna, ADZUNA_QUERIES
from app.database import SessionLocal
from app.models import Company
from app.config import settings

logger = logging.getLogger(__name__)

CURSOR_NS = "talentscope:scheduler:cursor"

# Companies with Greenhouse boards
GREENHOUSE_COMPANIES = [
    ("stripe", "Stripe"),
    ("notion", "Notion"),
    ("figma", "Figma"),
    ("vercel", "Vercel"),
    ("linear", "Linear"),
    ("brex", "Brex"),
    ("rippling", "Rippling"),
    ("gusto", "Gusto"),
    ("chime", "Chime"),
    ("coinbase", "Coinbase"),
    ("plaid", "Plaid"),
    ("airtable", "Airtable"),
    ("asana", "Asana"),
    ("dropbox", "Dropbox"),
    ("hubspot", "HubSpot"),
    ("intercom", "Intercom"),
    ("segment", "Segment"),
    ("cloudflare", "Cloudflare"),
    ("datadog", "Datadog"),
    ("hashicorp", "HashiCorp"),
    ("mongodb", "MongoDB"),
    ("confluent", "Confluent"),
    ("databricks", "Databricks"),
    ("snowflake-computing", "Snowflake"),
    ("okta", "Okta"),
    ("pagerduty", "PagerDuty"),
    ("elastic", "Elastic"),
    ("palantir", "Palantir"),
    ("pinterest", "Pinterest"),
    ("reddit", "Reddit"),
    ("roblox", "Roblox"),
    ("robinhood", "Robinhood"),
    ("lyft", "Lyft"),
    ("doordash", "DoorDash"),
    ("instacart", "Instacart"),
    ("carta", "Carta"),
    ("benchling", "Benchling"),
    ("lattice", "Lattice"),
    ("retool", "Retool"),
    ("canva", "Canva"),
    ("miro", "Miro"),
    ("amplitude", "Amplitude"),
    ("mixpanel", "Mixpanel"),
    ("heap", "Heap"),
    ("posthog", "PostHog"),
    ("sentry", "Sentry"),
]

# Companies with Lever boards
LEVER_COMPANIES = [
    ("netflix", "Netflix"),
    ("airbnb", "Airbnb"),
    ("spotify", "Spotify"),
    ("square", "Square"),
    ("shopify", "Shopify"),
    ("slack", "Slack"),
    ("zoom", "Zoom"),
    ("box", "Box"),
    ("splunk", "Splunk"),
    ("zenefits", "Zenefits"),
    ("calendly", "Calendly"),
    ("brex", "Brex"),
]

BATCH_SIZE = 10


def _get_redis() -> redis_lib.Redis | None:
    try:
        client = redis_lib.Redis.from_url(settings.redis_url, socket_connect_timeout=2)
        client.ping()
        return client
    except Exception:
        return None


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


@celery_app.task(name="app.tasks.scheduler.dispatch_greenhouse_batch")
def dispatch_greenhouse_batch():
    rc = _get_redis()
    if rc is None:
        logger.warning("Redis unavailable — falling back to batch 0 for Greenhouse")
        batch = GREENHOUSE_COMPANIES[:BATCH_SIZE]
    else:
        try:
            batch = _next_batch(rc, f"{CURSOR_NS}:greenhouse", GREENHOUSE_COMPANIES, BATCH_SIZE)
        except Exception as exc:
            logger.warning("Redis error selecting Greenhouse batch, falling back to batch 0: %s", exc)
            batch = GREENHOUSE_COMPANIES[:BATCH_SIZE]

    db = SessionLocal()
    try:
        for token, name in batch:
            company_id = _get_or_create_company(db, name, token)
            fetch_greenhouse.delay(token, company_id)
    finally:
        db.close()

    logger.info("Dispatched Greenhouse batch: %s", [t for t, _ in batch])
    return {"dispatched": [t for t, _ in batch]}


@celery_app.task(name="app.tasks.scheduler.dispatch_lever_batch")
def dispatch_lever_batch():
    rc = _get_redis()
    if rc is None:
        logger.warning("Redis unavailable — falling back to batch 0 for Lever")
        batch = LEVER_COMPANIES[:BATCH_SIZE]
    else:
        try:
            batch = _next_batch(rc, f"{CURSOR_NS}:lever", LEVER_COMPANIES, BATCH_SIZE)
        except Exception as exc:
            logger.warning("Redis error selecting Lever batch, falling back to batch 0: %s", exc)
            batch = LEVER_COMPANIES[:BATCH_SIZE]

    db = SessionLocal()
    try:
        for slug, name in batch:
            company_id = _get_or_create_company(db, name, slug)
            fetch_lever.delay(slug, company_id)
    finally:
        db.close()

    logger.info("Dispatched Lever batch: %s", [s for s, _ in batch])
    return {"dispatched": [s for s, _ in batch]}


@celery_app.task(name="app.tasks.scheduler.dispatch_adzuna_batch")
def dispatch_adzuna_batch():
    for query in ADZUNA_QUERIES:
        for page in range(1, 4):  # 3 pages * 50 results = 150 per query
            fetch_adzuna.delay(query, page)
    logger.info("Dispatched Adzuna batch: %d queries x 3 pages", len(ADZUNA_QUERIES))
    return {"dispatched": len(ADZUNA_QUERIES) * 3}
