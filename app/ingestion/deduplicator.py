from sqlalchemy.orm import Session
from sqlalchemy import select, func
from app.models import Posting


def is_exact_duplicate(db: Session, source: str, source_id: str) -> bool:
    """Check for exact duplicate by (source, source_id) — primary dedup."""
    result = db.execute(
        select(Posting.id).where(
            Posting.source == source,
            Posting.source_id == source_id
        ).limit(1)
    ).first()
    return result is not None


def find_fuzzy_duplicate(db: Session, company_id: int, title: str, location: str) -> int | None:
    """
    Cross-source fuzzy dedup: same company + title + location.
    Returns the existing posting id if found, else None.
    """
    if not company_id or not title:
        return None
    result = db.execute(
        select(Posting.id).where(
            Posting.company_id == company_id,
            func.lower(Posting.title) == title.lower(),
            func.lower(func.coalesce(Posting.location, "")) == (location or "").lower(),
        ).limit(1)
    ).first()
    return result[0] if result else None
