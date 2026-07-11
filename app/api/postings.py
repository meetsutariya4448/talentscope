from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import select, func
from app.database import get_db
from app.models import Posting, Company, Skill, PostingSkill
from pydantic import BaseModel
from datetime import datetime
from typing import Optional
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
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    query = select(Posting, Company.name.label("company_name")).join(
        Company, Posting.company_id == Company.id, isouter=True
    )

    if q:
        query = query.where(
            Posting.search_vector.op("@@")(func.plainto_tsquery("english", q))
        )

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

    total = db.execute(
        select(func.count()).select_from(query.subquery())
    ).scalar()

    results = db.execute(
        query.order_by(Posting.posted_at.desc().nullslast())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    postings_out = []
    for row in results:
        p = row[0]
        company_name = row[1]
        postings_out.append({
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
        })

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size) if total else 0,
        "results": postings_out,
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
