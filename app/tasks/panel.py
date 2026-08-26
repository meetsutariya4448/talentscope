from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.database import SessionLocal
from app.ingestion.panel import AUTHORITATIVE_SOURCES
from app.models import CollectionRun, CollectionRunCompany, Posting, PostingSnapshot
from app.tasks.celery_app import app as celery_app

logger = logging.getLogger(__name__)

HEALTH_LOOKBACK_DAYS = 7
HEALTH_MIN_HISTORY_DAYS = 3
DROP_ALERT_RATIO = 0.5       # postings_seen falling below this fraction of the trailing median triggers an alert
STALE_COMPANY_DAYS = 3       # a company erroring for this many consecutive days triggers an alert
# A company check that succeeded (ok) or genuinely returned zero postings (empty)
# both count as "we looked" — only http_error/timeout leave the day ambiguous.
CHECKED_STATUSES = ("ok", "empty")


def _detect_disappeared(db, today) -> int:
    """
    A posting is marked disappeared only if its company's board was
    successfully checked today and the posting wasn't in the results — never
    for a company whose check failed (ambiguous: closed req vs. our failure)
    and never for a non-authoritative source (Adzuna) where absence just means
    "not returned by this query today."

    Resurrection is handled separately, in app.ingestion.panel.
    apply_panel_fields_on_update, at the moment a posting is actually seen
    again — not here.
    """
    now = datetime.now(timezone.utc)
    seen_today = select(PostingSnapshot.posting_id).where(PostingSnapshot.snapshot_date == today)

    candidates = (
        db.execute(
            select(Posting).where(
                Posting.source.in_(AUTHORITATIVE_SOURCES),
                Posting.disappeared_at.is_(None),
                ~Posting.id.in_(seen_today),
            )
        )
        .scalars()
        .all()
    )

    count = 0
    for posting in candidates:
        checked = db.execute(
            select(CollectionRunCompany.id)
            .join(CollectionRun, CollectionRun.id == CollectionRunCompany.run_id)
            .where(
                CollectionRun.collection_date == today,
                CollectionRunCompany.source == posting.source,
                CollectionRunCompany.company_token == posting.company_token,
                CollectionRunCompany.status.in_(CHECKED_STATUSES),
            )
            .limit(1)
        ).first()
        if checked is not None:
            posting.disappeared_at = now
            count += 1
    return count


def _rollup_collection_run(db, source: str, today) -> None:
    run = db.execute(
        select(CollectionRun).where(CollectionRun.source == source, CollectionRun.collection_date == today)
    ).scalar_one_or_none()
    if run is None:
        return  # nothing was dispatched for this source today

    rows = db.execute(
        select(CollectionRunCompany).where(CollectionRunCompany.run_id == run.id)
    ).scalars().all()

    # Range comparisons on the UTC day boundaries, not func.date(): that
    # truncates a timestamptz using the DB session's timezone (e.g. one
    # configured as America/Phoenix), not UTC, and silently miscounts
    # everything near a day boundary once the two disagree.
    day_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    run.companies_checked = len(rows)
    run.postings_seen = sum(r.postings_seen for r in rows)
    run.errors_count = sum(1 for r in rows if r.status not in CHECKED_STATUSES)
    run.new_postings = db.execute(
        select(func.count()).select_from(Posting).where(
            Posting.source == source,
            Posting.first_seen_at >= day_start,
            Posting.first_seen_at < day_end,
        )
    ).scalar_one()
    run.disappeared_postings = db.execute(
        select(func.count()).select_from(Posting).where(
            Posting.source == source,
            Posting.disappeared_at >= day_start,
            Posting.disappeared_at < day_end,
        )
    ).scalar_one()

    if run.companies_checked > 0 and run.errors_count >= run.companies_checked:
        run.status = "failed"
    elif run.errors_count > 0:
        run.status = "degraded"
    else:
        run.status = "ok"
    run.run_at = datetime.now(timezone.utc)


def _check_health(db, today) -> list[str]:
    alerts: list[str] = []
    lookback_start = today - timedelta(days=HEALTH_LOOKBACK_DAYS)

    for source in AUTHORITATIVE_SOURCES:
        today_run = db.execute(
            select(CollectionRun).where(CollectionRun.source == source, CollectionRun.collection_date == today)
        ).scalar_one_or_none()
        if today_run is None:
            continue

        history = db.execute(
            select(CollectionRun.postings_seen).where(
                CollectionRun.source == source,
                CollectionRun.collection_date >= lookback_start,
                CollectionRun.collection_date < today,
            )
        ).scalars().all()
        if len(history) >= HEALTH_MIN_HISTORY_DAYS:
            median = statistics.median(history)
            if median > 0 and today_run.postings_seen < median * DROP_ALERT_RATIO:
                alerts.append(
                    f"{source}: postings_seen dropped to {today_run.postings_seen} "
                    f"(trailing {HEALTH_LOOKBACK_DAYS}-day median {median:.0f}) — possible silent collection failure"
                )

    stale_cutoff = today - timedelta(days=STALE_COMPANY_DAYS - 1)
    recent_bad = db.execute(
        select(CollectionRunCompany.source, CollectionRunCompany.company_token, func.count())
        .join(CollectionRun, CollectionRun.id == CollectionRunCompany.run_id)
        .where(
            CollectionRun.collection_date >= stale_cutoff,
            CollectionRun.collection_date <= today,
            CollectionRunCompany.status.in_(("http_error", "timeout")),
        )
        .group_by(CollectionRunCompany.source, CollectionRunCompany.company_token)
        .having(func.count() >= STALE_COMPANY_DAYS)
    ).all()
    for source, token, days_bad in recent_bad:
        alerts.append(
            f"{source}/{token}: failing for {days_bad} of the last {STALE_COMPANY_DAYS} days "
            f"— board token may have gone stale"
        )
    return alerts


@celery_app.task(name="app.tasks.panel.run_daily_rollup")
def run_daily_rollup():
    """
    Runs once/day, after all source cadences have had a chance to fire.
    Detects disappearances, rolls per-company health up into collection_runs,
    and raises loud alerts on the two silent-failure patterns that aggregate
    counts alone would miss: an overall volume drop, and one board token
    quietly going stale while the rest keep working.
    """
    db = SessionLocal()
    alerts: list[str] = []
    try:
        today = datetime.now(timezone.utc).date()

        disappeared = _detect_disappeared(db, today)
        db.commit()

        for source in AUTHORITATIVE_SOURCES:
            _rollup_collection_run(db, source, today)
        db.commit()

        alerts = _check_health(db, today)
        for alert in alerts:
            logger.critical("[posting-panel health] %s", alert)
    finally:
        db.close()

    return {"date": today.isoformat(), "disappeared": disappeared, "alerts": alerts}
