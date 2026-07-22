from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import select, func
from app.database import get_db
from app.models import Posting, Company, Skill, PostingSkill
from app.search.hybrid import (
    fts_search, vector_search, reciprocal_rank_fusion, fetch_postings_by_ids, TOP_K,
)
from pydantic import BaseModel
from datetime import datetime
from typing import Optional, Literal
from sqlalchemy import String
import math

router = APIRouter()


class PostingOut(BaseModel):
    id: int
    title: str
    location: Optional[str]
    salary_min: Optional[float]
    salary_max: Optional[float]
    currency: str
    source: str
    url: Optional[str]
    posted_at: Optional[datetime]
    company_name: Optional[str]

    model_config = {"from_attributes": True}


@router.get("/", response_model=dict)
def search_postings(
    q: str = Query(default="", description="Full-text search query"),
    skill: str = Query(default="", description="Filter by skill name"),
    location: str = Query(default="", description="Filter by location (partial match)"),
    mode: Literal["fts", "vector", "hybrid"] = Query(
        default="fts",
        description="Search mode: fts (full-text), vector (semantic), hybrid (RRF fusion)",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    # --- FTS mode (original behaviour, unchanged) ---
    if mode == "fts" or (not q and mode in ("vector", "hybrid")):
        return _fts_results(db, q, skill, location, page, page_size)

    # --- Vector or Hybrid mode ---
    fts_ids: list[int] = []
    vec_ids: list[int] = []

    if mode in ("fts", "hybrid"):
        fts_ids = fts_search(db, q, limit=TOP_K, skill=skill, location=location)
    if mode in ("vector", "hybrid"):
        vec_ids = vector_search(db, q, limit=TOP_K, skill=skill, location=location)

    if mode == "vector":
        merged = vec_ids
    else:
        merged = reciprocal_rank_fusion([fts_ids, vec_ids])

    total = len(merged)
    page_ids = merged[(page - 1) * page_size: page * page_size]
    rows = fetch_postings_by_ids(db, page_ids)

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size) if total else 0,
        "mode": mode,
        "results": [_row_to_dict(r) for r in rows],
    }


def _fts_results(
    db: Session,
    q: str,
    skill: str,
    location: str,
    page: int,
    page_size: int,
) -> dict:
    query = select(Posting, Company.name.label("company_name")).join(
        Company, Posting.company_id == Company.id, isouter=True
    )
    if q:
        # OR semantics: plainto_tsquery stems + stop-words, then flip AND → OR
        or_tsquery = func.to_tsquery(
            "english",
            func.replace(func.plainto_tsquery("english", q).cast(String), " & ", " | "),
        )
        query = query.where(Posting.search_vector.op("@@")(or_tsquery))
    if skill:
        skill_subq = (
            select(PostingSkill.posting_id)
            .join(Skill, PostingSkill.skill_id == Skill.id)
            .where(func.lower(Skill.name) == skill.lower())
            .scalar_subquery()
        )
        query = query.where(Posting.id.in_(skill_subq))
    if location:
        query = query.where(Posting.location.ilike(f"%{location}%"))

    total = db.execute(select(func.count()).select_from(query.subquery())).scalar()
    results = db.execute(
        query.order_by(Posting.posted_at.desc().nullslast())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size) if total else 0,
        "mode": "fts",
        "results": [_row_to_dict(r) for r in results],
    }


def _row_to_dict(row) -> dict:
    if isinstance(row[0], Posting):
        # ORM row: (Posting instance, company_name str)
        p, company_name = row[0], row[1]
        return {
            "id": p.id,
            "title": p.title,
            "location": p.location,
            "salary_min": float(p.salary_min) if p.salary_min else None,
            "salary_max": float(p.salary_max) if p.salary_max else None,
            "currency": p.currency or "USD",
            "source": p.source,
            "url": p.url,
            "posted_at": p.posted_at.isoformat() if p.posted_at else None,
            "company_name": company_name,
        }
    # Raw SQL row from fetch_postings_by_ids
    r = row._mapping
    return {
        "id": r["id"],
        "title": r["title"],
        "location": r.get("location"),
        "salary_min": float(r["salary_min"]) if r.get("salary_min") else None,
        "salary_max": float(r["salary_max"]) if r.get("salary_max") else None,
        "currency": r.get("currency") or "USD",
        "source": r["source"],
        "url": r.get("url"),
        "posted_at": r["posted_at"].isoformat() if r.get("posted_at") else None,
        "company_name": r.get("company_name"),
    }


@router.get("/stats")
def posting_stats(db: Session = Depends(get_db)):
    total = db.execute(select(func.count(Posting.id))).scalar()
    by_source = db.execute(
        select(Posting.source, func.count(Posting.id))
        .group_by(Posting.source)
    ).all()
    return {
        "total_postings": total,
        "by_source": {source: count for source, count in by_source},
    }
