from app.models import Company, Posting, Skill, PostingSkill
from datetime import datetime

from app.api.postings import _row_to_dict
from decimal import Decimal
from unittest.mock import MagicMock


def test_posting_response_preserves_zero_salary_bounds():
    posting = Posting(
        title="Volunteer Developer",
        source="greenhouse",
        source_id="zero-salary-test",
        currency="USD",
        salary_min=0,
        salary_max=0,
    )

    result = _row_to_dict((posting, "Community Org"))

    assert result["salary_min"] == 0.0
    assert result["salary_max"] == 0.0


def test_salary_trends_preserves_zero_averages():
    from app.api.analytics import salary_trends

    db = MagicMock()
    db.execute.return_value.all.return_value = [
        (datetime(2026, 1, 1), Decimal("0"), Decimal("0"), 1),
    ]

    result = salary_trends(role="", location="", db=db)

    assert result["trends"][0]["avg_salary_min"] == 0.0
    assert result["trends"][0]["avg_salary_max"] == 0.0


def test_cluster_summary_preserves_zero_silhouette():
    from app.api.analytics import get_clusters

    run_at = datetime(2026, 1, 1)
    latest_result = MagicMock()
    latest_result.scalar.return_value = run_at
    cluster = MagicMock(
        cluster_id=0,
        label="Uncategorized",
        size=4,
        top_skills="[]",
        silhouette=Decimal("0"),
    )
    clusters_result = MagicMock()
    clusters_result.scalars.return_value.all.return_value = [cluster]
    db = MagicMock()
    db.execute.side_effect = [latest_result, clusters_result]

    result = get_clusters(db=db)

    assert result["silhouette"] == 0.0


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_postings_empty(client):
    resp = client.get("/postings/")
    assert resp.status_code == 200
    data = resp.json()
    assert "results" in data
    assert "total" in data


def test_postings_search(client, db):
    company = Company(name="SearchCo", slug="searchco-api")
    db.add(company)
    db.flush()

    posting = Posting(
        company_id=company.id,
        title="Python Developer",
        location="Remote",
        source="greenhouse",
        source_id="api-test-001",
        currency="USD",
        posted_at=datetime.utcnow(),
    )
    db.add(posting)
    db.commit()

    resp = client.get("/postings/?location=Remote")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1


def test_skill_demand_endpoint(client):
    resp = client.get("/analytics/skill-demand?window=30d")
    assert resp.status_code == 200
    data = resp.json()
    assert "skills" in data
    assert "window" in data


def test_skill_demand_rejects_unknown_window(client):
    resp = client.get("/analytics/skill-demand?window=fortnight")

    assert resp.status_code == 422


def test_salary_trends_endpoint(client):
    resp = client.get("/analytics/salary-trends")
    assert resp.status_code == 200
    data = resp.json()
    assert "trends" in data


def test_top_companies_endpoint(client):
    resp = client.get("/analytics/top-companies")
    assert resp.status_code == 200
    data = resp.json()
    assert "companies" in data


def test_posting_stats(client):
    resp = client.get("/postings/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_postings" in data
