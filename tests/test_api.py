from app.models import Company, Posting, Skill, PostingSkill
from datetime import datetime


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
