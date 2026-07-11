from app.tasks.celery_app import app as celery_app
from app.tasks.greenhouse import fetch_greenhouse
from app.tasks.lever import fetch_lever
from app.tasks.adzuna import fetch_adzuna, ADZUNA_QUERIES
from app.database import SessionLocal
from app.models import Company
import logging

logger = logging.getLogger(__name__)

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

# Current batch pointers (stored in module state for simplicity;
# in production use Redis or DB to persist across restarts)
_gh_batch_index = 0
_lever_batch_index = 0
BATCH_SIZE = 10


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
    global _gh_batch_index
    batch = GREENHOUSE_COMPANIES[_gh_batch_index: _gh_batch_index + BATCH_SIZE]
    _gh_batch_index = (_gh_batch_index + BATCH_SIZE) % len(GREENHOUSE_COMPANIES)

    db = SessionLocal()
    try:
        for token, name in batch:
            company_id = _get_or_create_company(db, name, token)
            fetch_greenhouse.delay(token, company_id)
    finally:
        db.close()

    logger.info(f"Dispatched Greenhouse batch: {[t for t, _ in batch]}")
    return {"dispatched": [t for t, _ in batch]}


@celery_app.task(name="app.tasks.scheduler.dispatch_lever_batch")
def dispatch_lever_batch():
    global _lever_batch_index
    batch = LEVER_COMPANIES[_lever_batch_index: _lever_batch_index + BATCH_SIZE]
    _lever_batch_index = (_lever_batch_index + BATCH_SIZE) % len(LEVER_COMPANIES)

    db = SessionLocal()
    try:
        for slug, name in batch:
            company_id = _get_or_create_company(db, name, slug)
            fetch_lever.delay(slug, company_id)
    finally:
        db.close()

    logger.info(f"Dispatched Lever batch: {[s for s, _ in batch]}")
    return {"dispatched": [s for s, _ in batch]}


@celery_app.task(name="app.tasks.scheduler.dispatch_adzuna_batch")
def dispatch_adzuna_batch():
    for query in ADZUNA_QUERIES:
        for page in range(1, 4):  # 3 pages * 50 results = 150 per query
            fetch_adzuna.delay(query, page)
    logger.info(f"Dispatched Adzuna batch: {len(ADZUNA_QUERIES)} queries x 3 pages")
    return {"dispatched": len(ADZUNA_QUERIES) * 3}
