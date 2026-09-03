import pytest
from unittest.mock import patch, MagicMock
from app.ingestion.skills import extract_skills
from app.ingestion.normalizer import normalize_greenhouse, normalize_lever, normalize_adzuna, _strip_html


def test_extract_skills_python():
    text = "We need someone who knows Python, FastAPI, and PostgreSQL"
    skills = extract_skills(text)
    assert "Python" in skills
    assert "FastAPI" in skills
    assert "PostgreSQL" in skills


def test_extract_skills_case_insensitive():
    text = "Experience with REACT and typescript required"
    skills = extract_skills(text)
    assert "React" in skills
    assert "TypeScript" in skills


def test_extract_skills_empty():
    assert extract_skills("") == []
    assert extract_skills(None) == []


def test_strip_html():
    html = "<p>Hello <strong>world</strong></p>"
    assert _strip_html(html) == "Hello world"


def test_strip_html_decodes_entities_and_normalizes_nonbreaking_spaces():
    html = "<p>R&amp;D&nbsp;&lt;Platform&gt; &#39;team&#39;</p>"

    assert _strip_html(html) == "R&D <Platform> 'team'"


def test_normalize_greenhouse():
    job = {
        "id": 12345,
        "title": "Software Engineer",
        "location": {"name": "San Francisco, CA"},
        "content": "<p>We are looking for a Python developer</p>",
        "absolute_url": "https://boards.greenhouse.io/company/jobs/12345",
        "updated_at": "2024-01-15T10:00:00Z",
    }
    result = normalize_greenhouse(job, company_id=1)
    assert result["title"] == "Software Engineer"
    assert result["source"] == "greenhouse"
    assert result["source_id"] == "12345"
    assert result["company_id"] == 1
    assert "Python developer" in result["description"]


def test_normalize_lever():
    job = {
        "id": "abc-123",
        "text": "Backend Engineer",
        "categories": {"location": "New York"},
        "descriptionPlain": "Looking for Go expertise",
        "hostedUrl": "https://jobs.lever.co/company/abc-123",
        "createdAt": 1705315200000,
    }
    result = normalize_lever(job, company_id=2)
    assert result["title"] == "Backend Engineer"
    assert result["source"] == "lever"
    assert result["source_id"] == "abc-123"


def test_normalize_adzuna():
    job = {
        "id": "adzuna-999",
        "title": "Data Engineer",
        "location": {"display_name": "Austin, TX"},
        "description": "Spark and Kafka experience required",
        "salary_min": 80000,
        "salary_max": 120000,
        "redirect_url": "https://www.adzuna.com/jobs/999",
        "created": "2024-01-10T00:00:00Z",
        "company": {"display_name": "TechCorp"},
    }
    result = normalize_adzuna(job)
    assert result["title"] == "Data Engineer"
    assert result["source"] == "adzuna"
    assert result["salary_min"] == 80000.0
    assert result["salary_max"] == 120000.0


def test_normalize_adzuna_tolerates_invalid_salary_values():
    result = normalize_adzuna({
        "id": "adzuna-invalid-salary",
        "title": "Platform Engineer",
        "salary_min": "not disclosed",
        "salary_max": "NaN",
    })

    assert result["salary_min"] is None
    assert result["salary_max"] is None


def test_normalize_adzuna_preserves_zero_salary_bounds():
    result = normalize_adzuna({
        "id": "adzuna-zero-salary",
        "title": "Volunteer Engineer",
        "salary_min": 0,
        "salary_max": "0",
    })

    assert result["salary_min"] == 0.0
    assert result["salary_max"] == 0.0


def test_normalizers_tolerate_malformed_nested_provider_metadata():
    greenhouse = normalize_greenhouse({"location": "Remote"}, company_id=1)
    lever = normalize_lever({"categories": ["Remote"]}, company_id=2)
    adzuna = normalize_adzuna({"location": "Remote", "company": ["Acme"]})

    assert greenhouse["location"] == ""
    assert lever["location"] == ""
    assert adzuna["location"] == ""
    assert adzuna["company_name"] == ""


def test_fetch_greenhouse_task_eager(db):
    """Test greenhouse task with mocked HTTP call and mocked DB session."""
    from app.models import Company
    company = Company(name="TaskTestCo", slug="tasktestco")
    db.add(company)
    db.commit()
    company_id = company.id

    # record_company_check() opens its own SessionLocal() by design (so a
    # company's health record lands even if the caller's transaction later
    # fails) — point that at the test engine too, via a fresh session per
    # call, rather than letting it fall through to the real dev database.
    from sqlalchemy.orm import sessionmaker
    test_session_factory = sessionmaker(bind=db.get_bind())

    with patch("app.tasks.greenhouse.httpx.Client") as mock_client_cls, \
         patch("app.tasks.greenhouse.SessionLocal", return_value=db), \
         patch("app.ingestion.panel.SessionLocal", test_session_factory), \
         patch("app.tasks.embedding.embed_posting") as mock_embed:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "jobs": [{
                "id": 99001,
                "title": "Test Engineer",
                "location": {"name": "Remote"},
                "content": "<p>Python and Docker</p>",
                "absolute_url": "https://example.com",
                "updated_at": "2024-01-15T10:00:00Z",
            }]
        }
        mock_response.raise_for_status.return_value = None
        mock_response.status_code = 200
        mock_client.get.return_value = mock_response

        from app.tasks.greenhouse import fetch_greenhouse
        result = fetch_greenhouse("test-token", company_id)
        assert result["fetched"] == 1
        assert result["inserted"] >= 0
        # embed_posting.delay must have been called for each inserted posting
        assert mock_embed.delay.called
