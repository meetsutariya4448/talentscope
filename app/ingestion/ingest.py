from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingestion.deduplicator import find_fuzzy_duplicate
from app.ingestion.panel import apply_panel_fields_on_insert, apply_panel_fields_on_update, get_monitored_company
from app.ingestion.skills import extract_skills
from app.models import Posting, PostingSkill


@dataclass
class IngestResult:
    posting_id: int | None
    is_new: bool
    skipped: bool  # True only for a cross-source fuzzy duplicate — nothing written
    content_changed: bool = False  # existing posting whose title/description drifted — needs re-embedding


def ingest_posting(
    db: Session,
    data: dict,
    skill_map: dict[str, int],
    *,
    company_token: str | None = None,
) -> IngestResult:
    """
    Single insert-or-update path shared by every source (Greenhouse, Lever,
    Ashby, Adzuna). Replaces the old dedup-then-skip behavior: a posting seen
    again now gets its panel fields (last_seen_at, description_hash, snapshot
    row) updated in place rather than being silently ignored, which is what
    turns a one-shot search index into a daily panel.
    """
    source = data["source"]
    source_id = data["source_id"]

    existing = db.execute(
        select(Posting).where(Posting.source == source, Posting.source_id == source_id)
    ).scalar_one_or_none()

    if existing is not None:
        if company_token and existing.company_token != company_token:
            # Backfills postings ingested before company_token existed (or
            # under a stale value) — without this, a pre-migration posting's
            # company_token stays NULL forever and can never be matched
            # against collection_run_companies, so it can never be gated for
            # disappearance no matter how many times it's re-fetched.
            existing.company_token = company_token
        # Refresh mutable content fields on every re-sight. Previously only
        # description_hash/PostingDescriptionVersion tracked content drift —
        # the live row (what FTS, embeddings, and every API response read)
        # kept whatever was there at first-seen forever, so an edited title
        # or description on a later fetch never actually reached search or
        # the dashboard. This is what "incremental indexing" requires: the
        # index (search_vector, embedding) tracks live content, which in
        # turn requires the live row itself to be current.
        content_changed = (
            existing.title != data["title"] or existing.description != data.get("description")
        )
        existing.title = data["title"]
        existing.location = data.get("location")
        existing.description = data.get("description")
        existing.salary_min = data.get("salary_min")
        existing.salary_max = data.get("salary_max")
        existing.currency = data.get("currency", existing.currency)
        existing.url = data.get("url")
        existing.posted_at = data.get("posted_at") or existing.posted_at
        apply_panel_fields_on_update(db, existing, data.get("description") or "")
        return IngestResult(existing.id, is_new=False, skipped=False, content_changed=content_changed)

    if find_fuzzy_duplicate(db, data.get("company_id"), data["title"], data.get("location", "")):
        return IngestResult(None, is_new=False, skipped=True)

    posting = Posting(
        company_id=data["company_id"],
        title=data["title"],
        location=data.get("location"),
        description=data.get("description"),
        salary_min=data.get("salary_min"),
        salary_max=data.get("salary_max"),
        currency=data.get("currency", "USD"),
        source=source,
        source_id=source_id,
        url=data.get("url"),
        posted_at=data.get("posted_at"),
        company_token=company_token,
    )
    db.add(posting)
    db.flush()

    monitored = get_monitored_company(db, source, company_token)
    apply_panel_fields_on_insert(db, posting, data.get("description") or "", monitored)

    desc = data.get("title", "") + " " + (data.get("description") or "")
    for skill_name in extract_skills(desc):
        if skill_name in skill_map:
            db.add(PostingSkill(posting_id=posting.id, skill_id=skill_map[skill_name]))

    # search_vector is a Postgres GENERATED column (see migration 0005) —
    # Postgres recomputes it on every insert/update automatically, including
    # on the update path above, so it never needs writing here.
    return IngestResult(posting.id, is_new=True, skipped=False)
