from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import select, func
from app.database import get_db
from app.models import Posting, Skill, PostingSkill
from datetime import datetime, timedelta
from typing import Optional

router = APIRouter()


@router.get("/skill-demand")
def skill_demand(
    window: str = Query(default="30d", description="Time window: 7d, 30d, 90d, all"),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    """Return top skills by posting count within the given time window."""
    cutoff = _parse_window(window)

    q = (
        select(Skill.name, Skill.category, func.count(PostingSkill.posting_id).label("count"))
        .join(PostingSkill, Skill.id == PostingSkill.skill_id)
        .join(Posting, PostingSkill.posting_id == Posting.id)
    )
    if cutoff:
        q = q.where(Posting.created_at >= cutoff)

    q = q.group_by(Skill.name, Skill.category).order_by(func.count(PostingSkill.posting_id).desc()).limit(limit)

    results = db.execute(q).all()
    return {
        "window": window,
        "skills": [{"name": r[0], "category": r[1], "count": r[2]} for r in results],
    }


@router.get("/salary-trends")
def salary_trends(
    role: str = Query(default="", description="Role keyword filter"),
    location: str = Query(default="", description="Location filter"),
    db: Session = Depends(get_db),
):
    """Return average salary by month for postings that have salary data."""
    q = (
        select(
            func.date_trunc("month", Posting.posted_at).label("month"),
            func.avg(Posting.salary_min).label("avg_salary_min"),
            func.avg(Posting.salary_max).label("avg_salary_max"),
            func.count(Posting.id).label("count"),
        )
        .where(Posting.salary_min.isnot(None))
        .where(Posting.posted_at.isnot(None))
    )

    if role:
        q = q.where(Posting.title.ilike(f"%{role}%"))
    if location:
        q = q.where(Posting.location.ilike(f"%{location}%"))

    q = q.group_by(func.date_trunc("month", Posting.posted_at)).order_by(
        func.date_trunc("month", Posting.posted_at)
    )

    results = db.execute(q).all()
    return {
        "role": role,
        "location": location,
        "trends": [
            {
                "month": r[0].isoformat() if r[0] else None,
                "avg_salary_min": round(float(r[1]), 2) if r[1] else None,
                "avg_salary_max": round(float(r[2]), 2) if r[2] else None,
                "count": r[3],
            }
            for r in results
        ],
    }


@router.get("/top-companies")
def top_companies(
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    from app.models import Company
    q = (
        select(Company.name, func.count(Posting.id).label("count"))
        .join(Posting, Posting.company_id == Company.id)
        .group_by(Company.name)
        .order_by(func.count(Posting.id).desc())
        .limit(limit)
    )
    results = db.execute(q).all()
    return {"companies": [{"name": r[0], "count": r[1]} for r in results]}


def _parse_window(window: str) -> Optional[datetime]:
    if window == "all":
        return None
    mapping = {"7d": 7, "30d": 30, "90d": 90, "180d": 180, "365d": 365}
    days = mapping.get(window, 30)
    return datetime.utcnow() - timedelta(days=days)
