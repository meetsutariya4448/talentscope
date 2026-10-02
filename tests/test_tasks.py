from datetime import timezone

import pytest
import httpx
from unittest.mock import patch, MagicMock
from app.ingestion.skills import extract_skills
from app.ingestion.normalizer import (
    _strip_html,
    normalize_adzuna,
    normalize_ashby,
    normalize_greenhouse,
    normalize_lever,
)


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


def test_skill_catalog_initialization_is_conflict_safe():
    from app.ingestion.skills import ensure_skills
    from sqlalchemy.dialects import postgresql

    insert_result = MagicMock()
    select_result = MagicMock()
    select_result.all.return_value = [("Python", 1), ("Go", 2)]
    db = MagicMock()
    db.execute.side_effect = [insert_result, select_result]

    assert ensure_skills(db) == {"Python": 1, "Go": 2}
    statement = db.execute.call_args_list[0].args[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (name) DO NOTHING" in compiled


def test_strip_html():
    html = "<p>Hello <strong>world</strong></p>"
    assert _strip_html(html) == "Hello world"


def test_strip_html_decodes_entities_and_normalizes_nonbreaking_spaces():
    html = "<p>R&amp;D&nbsp;&lt;Platform&gt; &#39;team&#39;</p>"

    assert _strip_html(html) == "R&D <Platform> 'team'"


def test_strip_html_discards_script_and_style_contents():
    html = (
        "<style>.job { display: none; }</style>"
        "<p>Platform engineer</p>"
        "<script>trackApplicant('secret')</script>"
    )

    assert _strip_html(html) == "Platform engineer"


@pytest.mark.parametrize("value", [None, 123, {"html": "<p>unexpected</p>"}])
def test_strip_html_tolerates_non_string_provider_values(value):
    assert _strip_html(value) == ""


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
    assert result["posted_at"].tzinfo is timezone.utc


def test_normalizers_fall_back_from_blank_or_nul_only_preferred_fields():
    lever = normalize_lever(
        {
            "id": "lever-fallback",
            "descriptionPlain": " \x00 ",
            "description": "Valid fallback description",
        },
        company_id=2,
    )
    ashby = normalize_ashby(
        {
            "id": "ashby-fallback",
            "jobUrl": "\x00 ",
            "applyUrl": "https://example.test/apply",
        },
        company_id=3,
    )

    assert lever["description"] == "Valid fallback description"
    assert ashby["url"] == "https://example.test/apply"


def test_normalize_lever_converts_numeric_source_id_to_string():
    result = normalize_lever({"id": 123, "text": "Engineer"}, company_id=2)

    assert result["source_id"] == "123"


def test_normalizers_return_utc_aware_provider_timestamps():
    greenhouse = normalize_greenhouse(
        {"id": "gh-time", "updated_at": "2024-01-15T10:00:00"}, company_id=1
    )
    ashby = normalize_ashby(
        {"id": "ashby-time", "publishedAt": "2024-01-15T10:00:00-05:00"},
        company_id=2,
    )

    assert greenhouse["posted_at"].tzinfo is timezone.utc
    assert greenhouse["posted_at"].hour == 10
    assert ashby["posted_at"].tzinfo is timezone.utc
    assert ashby["posted_at"].hour == 15


def test_normalizers_ignore_non_timestamp_provider_values():
    lever = normalize_lever({"id": "lever-time", "createdAt": True}, company_id=1)
    adzuna = normalize_adzuna({"id": "adzuna-time", "created": {"date": "bad"}})

    assert lever["posted_at"] is None
    assert adzuna["posted_at"] is None


@pytest.mark.parametrize("created_at", [-1, float("nan"), float("inf")])
def test_lever_normalizer_rejects_invalid_epoch_timestamps(created_at):
    result = normalize_lever(
        {"id": "lever-invalid-time", "createdAt": created_at}, company_id=1
    )

    assert result["posted_at"] is None


@pytest.mark.parametrize(
    ("normalizer", "job", "args"),
    [
        (normalize_greenhouse, {"title": "Engineer"}, (1,)),
        (normalize_lever, {"id": "  ", "text": "Engineer"}, (1,)),
        (normalize_ashby, {"id": None, "title": "Engineer"}, (1,)),
        (normalize_adzuna, {"id": False, "title": "Engineer"}, ()),
    ],
)
def test_normalizers_reject_missing_provider_ids(normalizer, job, args):
    with pytest.raises(ValueError, match="valid id"):
        normalizer(job, *args)


@pytest.mark.parametrize("source_id", [1.25, float("nan"), ["job-1"], {"id": "job-1"}])
def test_normalizers_reject_compound_or_nonintegral_provider_ids(source_id):
    with pytest.raises(ValueError, match="valid id"):
        normalize_adzuna({"id": source_id, "title": "Engineer"})


def test_normalizers_reject_provider_ids_that_exceed_storage_limit():
    with pytest.raises(ValueError, match="512-character storage limit"):
        normalize_adzuna({"id": "x" * 513, "title": "Engineer"})


@pytest.mark.parametrize(
    "source_id",
    [" job-1", "job-1 ", "job\x001", "job\n1", "job\u202e1"],
)
def test_normalizers_reject_ambiguous_identity_fields(source_id):
    with pytest.raises(ValueError, match="whitespace|control|format"):
        normalize_adzuna({"id": source_id, "title": "Engineer"})


def test_normalizers_remove_nul_from_provider_text_fields():
    result = normalize_adzuna({
        "id": "safe-id",
        "title": "Platform\x00 Engineer",
        "description": "Build\x00 systems",
        "company": {"display_name": "Acme\x00 Corp"},
    })

    assert result["title"] == "Platform Engineer"
    assert result["description"] == "Build systems"
    assert result["company_name"] == "Acme Corp"


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


@pytest.mark.parametrize("salary", [-1, "-0.01"])
def test_normalize_adzuna_rejects_negative_salary_bounds(salary):
    result = normalize_adzuna({
        "id": "adzuna-negative-salary",
        "title": "Engineer",
        "salary_min": salary,
        "salary_max": salary,
    })

    assert result["salary_min"] is None
    assert result["salary_max"] is None


@pytest.mark.parametrize("salary", [10_000_000_000, "10000000000.00", 1e100])
def test_normalize_adzuna_rejects_salary_bounds_that_overflow_storage(salary):
    result = normalize_adzuna({
        "id": "adzuna-oversized-salary",
        "title": "Engineer",
        "salary_min": salary,
        "salary_max": salary,
    })

    assert result["salary_min"] is None
    assert result["salary_max"] is None


def test_normalize_adzuna_discards_inverted_salary_range():
    result = normalize_adzuna({
        "id": "adzuna-inverted-salary",
        "title": "Engineer",
        "salary_min": 150000,
        "salary_max": 100000,
    })

    assert result["salary_min"] is None
    assert result["salary_max"] is None


def test_normalizers_tolerate_malformed_nested_provider_metadata():
    greenhouse = normalize_greenhouse(
        {"id": "gh-1", "location": "Remote"}, company_id=1
    )
    lever = normalize_lever({"id": "lever-1", "categories": ["Remote"]}, company_id=2)
    adzuna = normalize_adzuna(
        {"id": "adzuna-1", "location": "Remote", "company": ["Acme"]}
    )

    assert greenhouse["location"] == ""
    assert lever["location"] == ""
    assert adzuna["location"] == ""
    assert adzuna["company_name"] == ""


def test_normalizers_tolerate_malformed_top_level_text_fields():
    greenhouse = normalize_greenhouse(
        {"id": "gh-text", "title": ["Engineer"], "absolute_url": {"url": "bad"}},
        company_id=1,
    )
    lever = normalize_lever(
        {
            "id": "lever-text",
            "text": {"value": "Engineer"},
            "descriptionPlain": ["bad"],
            "description": "Valid fallback",
            "hostedUrl": 123,
        },
        company_id=2,
    )
    ashby = normalize_ashby(
        {
            "id": "ashby-text",
            "title": 123,
            "location": ["Remote"],
            "jobUrl": {"url": "bad"},
            "applyUrl": "https://example.test/apply",
        },
        company_id=3,
    )
    adzuna = normalize_adzuna(
        {"id": "adzuna-text", "title": ["Engineer"], "description": {}, "redirect_url": 1}
    )

    assert (greenhouse["title"], greenhouse["url"]) == ("", "")
    assert (lever["title"], lever["description"], lever["url"]) == (
        "", "Valid fallback", "",
    )
    assert (ashby["title"], ashby["location"], ashby["url"]) == (
        "", "", "https://example.test/apply",
    )
    assert (adzuna["title"], adzuna["description"], adzuna["url"]) == ("", "", "")


def test_normalizers_bound_text_to_database_column_limits():
    result = normalize_greenhouse(
        {
            "id": "gh-long-fields",
            "title": "T" * 513,
            "location": {"name": "L" * 256},
        },
        company_id=1,
    )

    assert result["title"] == "T" * 512
    assert result["location"] == "L" * 255

    adzuna = normalize_adzuna({
        "id": "adzuna-long-company",
        "company": {"display_name": "C" * 256},
    })
    assert adzuna["company_name"] == "C" * 255


def test_adzuna_http_failure_does_not_expose_credentials(monkeypatch, caplog):
    from app.config import settings
    from app.tasks.adzuna import _fetch_results

    secret = "test-app-key-must-not-appear"
    monkeypatch.setattr(settings, "adzuna_app_id", "test-app-id")
    monkeypatch.setattr(settings, "adzuna_app_key", secret)
    request = httpx.Request(
        "GET", f"https://example.test/jobs?app_id=test-app-id&app_key={secret}"
    )
    response = httpx.Response(401, request=request)
    client = MagicMock()
    client.__enter__.return_value.get.return_value = response

    with patch("app.tasks.adzuna.httpx.Client", return_value=client):
        with pytest.raises(RuntimeError, match=r"Adzuna request failed \(HTTP 401\)") as error:
            _fetch_results("python & data", 1)

    assert secret not in str(error.value)
    assert secret not in caplog.text


def test_adzuna_company_creation_uses_conflict_safe_insert():
    from app.tasks.adzuna import _get_or_create_company
    from sqlalchemy.dialects import postgresql

    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = 17

    assert _get_or_create_company(db, "Acme Corporation") == 17
    statement = db.execute.call_args.args[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (slug) DO NOTHING" in compiled


def test_adzuna_company_creation_reads_winner_after_conflict():
    from app.tasks.adzuna import _get_or_create_company

    insert_result = MagicMock()
    insert_result.scalar_one_or_none.return_value = None
    select_result = MagicMock()
    select_result.one.return_value = (23, "Acme Corporation")
    db = MagicMock()
    db.execute.side_effect = [insert_result, select_result]

    assert _get_or_create_company(db, "Acme Corporation") == 23
    assert db.execute.call_count == 2


def test_adzuna_company_creation_disambiguates_colliding_slugs():
    from app.tasks.adzuna import _get_or_create_company

    base_insert = MagicMock()
    base_insert.scalar_one_or_none.return_value = None
    base_company = MagicMock()
    base_company.one.return_value = (23, "A-B")
    collision_insert = MagicMock()
    collision_insert.scalar_one_or_none.return_value = 29
    db = MagicMock()
    db.execute.side_effect = [base_insert, base_company, collision_insert]

    assert _get_or_create_company(db, "A B") == 29
    collision_statement = db.execute.call_args_list[2].args[0]
    assert collision_statement.compile().params["slug"].startswith("a-b-")
    assert len(collision_statement.compile().params["slug"]) <= 255


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
