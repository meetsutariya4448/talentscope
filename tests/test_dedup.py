from app.ingestion.deduplicator import is_exact_duplicate, find_fuzzy_duplicate
from app.models import Company, Posting
from datetime import datetime


def test_exact_duplicate_not_present(db):
    assert is_exact_duplicate(db, "greenhouse", "nonexistent-999") is False


def test_exact_duplicate_detected(db):
    company = Company(name="TestCo", slug="testco-dedup")
    db.add(company)
    db.flush()

    posting = Posting(
        company_id=company.id,
        title="Engineer",
        source="greenhouse",
        source_id="test-exact-123",
        currency="USD",
    )
    db.add(posting)
    db.flush()

    assert is_exact_duplicate(db, "greenhouse", "test-exact-123") is True


def test_fuzzy_duplicate_detected(db):
    company = Company(name="FuzzyCo", slug="fuzzyco-dedup")
    db.add(company)
    db.flush()

    posting = Posting(
        company_id=company.id,
        title="Senior Software Engineer",
        location="New York, NY",
        source="greenhouse",
        source_id="fuzzy-gh-001",
        currency="USD",
    )
    db.add(posting)
    db.flush()

    result = find_fuzzy_duplicate(db, company.id, "Senior Software Engineer", "New York, NY")
    assert result == posting.id


def test_fuzzy_no_match_different_location(db):
    company = Company(name="FuzzyCo2", slug="fuzzyco2-dedup")
    db.add(company)
    db.flush()

    posting = Posting(
        company_id=company.id,
        title="Backend Engineer",
        location="San Francisco, CA",
        source="lever",
        source_id="fuzzy-lv-002",
        currency="USD",
    )
    db.add(posting)
    db.flush()

    result = find_fuzzy_duplicate(db, company.id, "Backend Engineer", "New York, NY")
    assert result is None
