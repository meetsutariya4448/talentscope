from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.ingestion.hashing import hash_text, normalize_description
from app.models import (
    CollectionRun,
    CollectionRunCompany,
    MonitoredCompany,
    Posting,
    PostingDescriptionVersion,
    PostingSnapshot,
)

# Boards a company controls directly. A posting missing from one of these means
# the req closed. Adzuna is a search index — a posting missing from its results
# means "not returned by this query today" (ranking, quota, feed lag), never
# "req closed" — so it must never drive disappeared_at.
AUTHORITATIVE_SOURCES = {"greenhouse", "lever", "ashby"}

# A company just added to monitoring may already have postings open that we
# have no history for. Give one cadence's worth of slack (fetches run every
# 4h) before treating a first-seen posting as having a known age.
LEFT_TRUNCATION_WINDOW = timedelta(hours=8)


def get_monitored_company(db: Session, source: str, company_token: str | None) -> MonitoredCompany | None:
    if not company_token:
        return None
    return (
        db.query(MonitoredCompany)
        .filter_by(source=source, company_token=company_token)
        .first()
    )


def compute_left_truncated(monitored: MonitoredCompany | None, now: datetime) -> bool:
    if monitored is None or monitored.monitoring_started_at is None:
        return True
    return (now - monitored.monitoring_started_at) < LEFT_TRUNCATION_WINDOW


def _upsert_snapshot(db: Session, posting_id: int, now: datetime, description_hash: str | None) -> None:
    stmt = (
        pg_insert(PostingSnapshot)
        .values(
            posting_id=posting_id,
            snapshot_date=now.date(),
            description_hash=description_hash,
            captured_at=now,
        )
        .on_conflict_do_nothing(index_elements=["posting_id", "snapshot_date"])
    )
    db.execute(stmt)


def apply_panel_fields_on_insert(
    db: Session, posting: Posting, raw_description: str, monitored: MonitoredCompany | None
) -> None:
    """Called once, right after a brand-new posting is flushed and has an id."""
    now = datetime.now(timezone.utc)
    normalized = normalize_description(raw_description)

    posting.description_hash = hash_text(normalized)
    posting.raw_hash = hash_text(raw_description)
    posting.first_seen_at = now
    posting.last_seen_at = now
    posting.left_truncated = compute_left_truncated(monitored, now)

    db.add(
        PostingDescriptionVersion(
            posting_id=posting.id,
            version_seq=1,
            description_text=raw_description or "",
            description_hash=posting.description_hash,
            first_seen_snapshot_date=now.date(),
        )
    )
    _upsert_snapshot(db, posting.id, now, posting.description_hash)


def apply_panel_fields_on_update(db: Session, posting: Posting, raw_description: str) -> None:
    """Called every time an already-known posting (same source+source_id) is
    seen again. Handles resurrection (clearing disappeared_at) and requirement
    drift (a new posting_description_versions row) in the same pass."""
    now = datetime.now(timezone.utc)
    normalized = normalize_description(raw_description)
    new_hash = hash_text(normalized)

    if posting.disappeared_at is not None:
        posting.disappeared_at = None
        posting.absence_episode_count = (posting.absence_episode_count or 0) + 1

    if new_hash != posting.description_hash:
        last_version = (
            db.query(PostingDescriptionVersion)
            .filter_by(posting_id=posting.id)
            .order_by(PostingDescriptionVersion.version_seq.desc())
            .first()
        )
        next_seq = (last_version.version_seq + 1) if last_version else 1
        db.add(
            PostingDescriptionVersion(
                posting_id=posting.id,
                version_seq=next_seq,
                description_text=raw_description or "",
                description_hash=new_hash,
                first_seen_snapshot_date=now.date(),
            )
        )
        posting.description_hash = new_hash

    posting.raw_hash = hash_text(raw_description)
    posting.last_seen_at = now
    _upsert_snapshot(db, posting.id, now, posting.description_hash)


def get_or_create_collection_run(db: Session, source: str, collection_date) -> CollectionRun:
    run = (
        db.query(CollectionRun)
        .filter_by(source=source, collection_date=collection_date)
        .first()
    )
    if run is None:
        run = CollectionRun(
            source=source,
            collection_date=collection_date,
            run_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.flush()
    return run


def record_company_check(
    source: str,
    company_token: str,
    *,
    status: str,
    http_status: int | None,
    postings_seen: int,
    error_detail: str | None,
) -> None:
    """Self-contained: opens and commits its own session so a company's health
    record lands even if the caller's fetch/ingest transaction later fails or
    is retried by Celery. Idempotent per (run, source, token) — a retry just
    overwrites with the latest outcome."""
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        run = get_or_create_collection_run(db, source, now.date())
        stmt = pg_insert(CollectionRunCompany).values(
            run_id=run.id,
            source=source,
            company_token=company_token,
            status=status,
            http_status=http_status,
            postings_seen=postings_seen,
            error_detail=error_detail,
            checked_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["run_id", "source", "company_token"],
            set_={
                "status": stmt.excluded.status,
                "http_status": stmt.excluded.http_status,
                "postings_seen": stmt.excluded.postings_seen,
                "error_detail": stmt.excluded.error_detail,
                "checked_at": stmt.excluded.checked_at,
            },
        )
        db.execute(stmt)
        db.commit()
    finally:
        db.close()
