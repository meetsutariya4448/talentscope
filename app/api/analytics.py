from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session
from sqlalchemy import select, func
from app.database import get_db
from app.models import Posting, Skill, PostingSkill, SkillCluster
from datetime import datetime, timedelta
from typing import Literal, Optional
import json

router = APIRouter()


@router.get("/skill-demand")
def skill_demand(
    window: Literal["7d", "30d", "90d", "180d", "365d", "all"] = Query(
        default="30d",
        description="Time window: 7d, 30d, 90d, 180d, 365d, all",
    ),
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
                "avg_salary_min": round(float(r[1]), 2) if r[1] is not None else None,
                "avg_salary_max": round(float(r[2]), 2) if r[2] is not None else None,
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


@router.get("/clusters")
def get_clusters(db: Session = Depends(get_db)):
    """Return the most recent clustering run: per-cluster label, size, and top skills."""
    latest_run_at = db.execute(select(func.max(SkillCluster.run_at))).scalar()

    if not latest_run_at:
        return {"clusters": [], "run_at": None, "k": 0, "silhouette": None,
                "message": "No clustering run yet — POST /analytics/clusters/run to trigger one"}

    clusters = db.execute(
        select(SkillCluster)
        .where(SkillCluster.run_at == latest_run_at)
        .order_by(SkillCluster.size.desc())
    ).scalars().all()

    sil = (
        float(clusters[0].silhouette)
        if clusters and clusters[0].silhouette is not None
        else None
    )

    return {
        "run_at":     latest_run_at.isoformat(),
        "k":          len(clusters),
        "silhouette": round(sil, 4) if sil is not None else None,
        "clusters": [
            {
                "cluster_id": c.cluster_id,
                "label":      c.label,
                "size":       c.size,
                "top_skills": json.loads(c.top_skills or "[]"),
            }
            for c in clusters
        ],
    }


@router.post("/clusters/run", status_code=202)
def trigger_clustering(
    response: Response,
    k: Optional[int] = Query(default=None, ge=2, le=30, description="Fix k; omit to auto-select via silhouette"),
    sync: bool = Query(
        default=False,
        description="Run inline and return the summary instead of dispatching. "
                    "Local/CI seeding only — this blocks a request thread for the "
                    "whole fit.",
    ),
    db: Session = Depends(get_db),
):
    """
    Dispatch KMeans clustering to the worker and return 202 with a task id.

    This used to run inline. That made a public, unauthenticated endpoint
    capable of occupying a request thread for the length of a full-corpus
    silhouette grid search over k=5..15 with n_init=10 — the same work the
    Celery task guards with time_limit=1200 because it can exceed ten minutes.
    The HTTP route had no timeout at all, so a handful of concurrent calls
    could saturate the API's threadpool at no cost to the caller.

    `sync=true` keeps the old inline behaviour for CI seeding and local demo
    setup, where blocking is the point and there may be no worker running.
    """
    if sync:
        from app.ml.clustering import run_clustering
        result = run_clustering(db, k=k)
        if "error" in result:
            raise HTTPException(status_code=422, detail=result["error"])
        response.status_code = 200
        return result

    from app.tasks.clustering import run_clustering_task
    task = run_clustering_task.delay(k)
    return {
        "status": "accepted",
        "task_id": task.id,
        "k": k,
        "detail": "Clustering dispatched to the maintenance queue. "
                  "Poll GET /analytics/clusters for results.",
    }


def _parse_window(
    window: Literal["7d", "30d", "90d", "180d", "365d", "all"],
) -> Optional[datetime]:
    if window == "all":
        return None
    mapping = {"7d": 7, "30d": 30, "90d": 90, "180d": 180, "365d": 365}
    days = mapping[window]
    return datetime.utcnow() - timedelta(days=days)
