from datetime import date, datetime, timedelta, timezone

from app.ingestion.hashing import hash_text, normalize_description
from app.ingestion.ingest import ingest_posting
from app.models import (
    Company,
    CollectionRun,
    CollectionRunCompany,
    MonitoredCompany,
    Posting,
    PostingDescriptionVersion,
    PostingSnapshot,
)
from app.tasks.panel import _detect_disappeared


# ---------------------------------------------------------------------------
# Hashing / normalization
# ---------------------------------------------------------------------------

def test_normalize_description_strips_html_and_relative_dates():
    text = "<p>We need a <strong>Python</strong> dev. Posted 3 days ago!</p>"
    normalized = normalize_description(text)
    assert "<" not in normalized
    assert "days ago" not in normalized
    assert "python" in normalized


def test_normalize_description_case_and_whitespace_insensitive():
    a = normalize_description("Python   Engineer\n\nrequired")
    b = normalize_description("python engineer required")
    assert a == b


def test_hash_text_stable_for_equivalent_normalized_text():
    a = hash_text(normalize_description("<p>Python engineer</p>"))
    b = hash_text(normalize_description("python engineer"))
    assert a == b


# ---------------------------------------------------------------------------
# ingest_posting: insert / update / drift / resurrection
# ---------------------------------------------------------------------------

def _company(db, slug: str) -> Company:
    company = Company(name=slug, slug=slug)
    db.add(company)
    db.flush()
    return company


def test_ingest_posting_first_sight_sets_panel_fields(db):
    company = _company(db, "panelco-1")
    data = {
        "company_id": company.id,
        "title": "Backend Engineer",
        "location": "Remote",
        "description": "Python and Postgres required",
        "source": "greenhouse",
        "source_id": "panel-1",
        "currency": "USD",
    }
    result = ingest_posting(db, data, skill_map={}, company_token="panelco-1")
    assert result.is_new is True

    posting = db.get(Posting, result.posting_id)
    assert posting.company_token == "panelco-1"
    assert posting.first_seen_at is not None
    assert posting.first_seen_at == posting.last_seen_at
    assert posting.description_hash is not None
    assert posting.left_truncated is True  # no MonitoredCompany registered → unknown history

    # Not filtered by date: a TIMESTAMPTZ read back through psycopg2 renders
    # in the session's local timezone, so posting.first_seen_at.date() can
    # legitimately differ from the UTC date snapshot_date was stored under.
    assert db.query(PostingSnapshot).filter_by(posting_id=posting.id).count() == 1

    versions = db.query(PostingDescriptionVersion).filter_by(posting_id=posting.id).all()
    assert len(versions) == 1
    assert versions[0].version_seq == 1


def test_ingest_posting_second_sight_updates_not_duplicates(db):
    company = _company(db, "panelco-2")
    data = {
        "company_id": company.id,
        "title": "Frontend Engineer",
        "location": "Remote",
        "description": "React required",
        "source": "greenhouse",
        "source_id": "panel-2",
        "currency": "USD",
    }
    first = ingest_posting(db, data, skill_map={}, company_token="panelco-2")
    second = ingest_posting(db, dict(data), skill_map={}, company_token="panelco-2")

    assert second.is_new is False
    assert second.posting_id == first.posting_id
    assert db.query(Posting).filter_by(source="greenhouse", source_id="panel-2").count() == 1

    # Same day seen twice → still exactly one snapshot row (ON CONFLICT DO NOTHING).
    posting = db.get(Posting, first.posting_id)
    snapshots = db.query(PostingSnapshot).filter_by(posting_id=posting.id).all()
    assert len(snapshots) == 1


def test_ingest_posting_description_change_creates_new_version(db):
    company = _company(db, "panelco-3")
    base = {
        "company_id": company.id,
        "title": "Data Engineer",
        "location": "NYC",
        "source": "greenhouse",
        "source_id": "panel-3",
        "currency": "USD",
    }
    ingest_posting(db, {**base, "description": "SQL required"}, skill_map={}, company_token="panelco-3")
    result = ingest_posting(db, {**base, "description": "SQL and dbt required"}, skill_map={}, company_token="panelco-3")

    versions = (
        db.query(PostingDescriptionVersion)
        .filter_by(posting_id=result.posting_id)
        .order_by(PostingDescriptionVersion.version_seq)
        .all()
    )
    assert len(versions) == 2
    assert versions[1].description_text == "SQL and dbt required"


def test_ingest_posting_reappearance_clears_disappeared_and_counts_episode(db):
    company = _company(db, "panelco-4")
    data = {
        "company_id": company.id,
        "title": "SRE",
        "location": "Remote",
        "description": "Kubernetes required",
        "source": "greenhouse",
        "source_id": "panel-4",
        "currency": "USD",
    }
    first = ingest_posting(db, data, skill_map={}, company_token="panelco-4")
    posting = db.get(Posting, first.posting_id)
    posting.disappeared_at = datetime.now(timezone.utc)
    db.flush()

    ingest_posting(db, dict(data), skill_map={}, company_token="panelco-4")

    db.refresh(posting)
    assert posting.disappeared_at is None
    assert posting.absence_episode_count == 1


def test_ingest_posting_backfills_company_token_on_update(db):
    company = _company(db, "panelco-5")
    data = {
        "company_id": company.id,
        "title": "Support Engineer",
        "location": "Remote",
        "description": "Zendesk required",
        "source": "greenhouse",
        "source_id": "panel-5",
        "currency": "USD",
    }
    # First ingested without a token (simulates a pre-panel legacy row).
    result = ingest_posting(db, data, skill_map={}, company_token=None)
    posting = db.get(Posting, result.posting_id)
    assert posting.company_token is None

    ingest_posting(db, dict(data), skill_map={}, company_token="panelco-5")
    db.refresh(posting)
    assert posting.company_token == "panelco-5"


def test_left_truncated_false_once_monitoring_window_has_passed(db):
    company = _company(db, "panelco-6")
    db.add(
        MonitoredCompany(
            source="greenhouse",
            company_token="panelco-6",
            display_name="PanelCo 6",
            monitoring_started_at=datetime.now(timezone.utc) - timedelta(days=30),
            is_active=True,
        )
    )
    db.flush()

    data = {
        "company_id": company.id,
        "title": "Platform Engineer",
        "location": "Remote",
        "description": "Go required",
        "source": "greenhouse",
        "source_id": "panel-6",
        "currency": "USD",
    }
    result = ingest_posting(db, data, skill_map={}, company_token="panelco-6")
    posting = db.get(Posting, result.posting_id)
    assert posting.left_truncated is False


# ---------------------------------------------------------------------------
# Disappearance gating: only a successfully-checked company can mark absence
# ---------------------------------------------------------------------------

def _seed_run_company(db, *, today: date, source: str, token: str, status: str) -> None:
    run = db.query(CollectionRun).filter_by(source=source, collection_date=today).first()
    if run is None:
        run = CollectionRun(source=source, collection_date=today, run_at=datetime.now(timezone.utc))
        db.add(run)
        db.flush()
    db.add(
        CollectionRunCompany(
            run_id=run.id, source=source, company_token=token, status=status,
            http_status=200 if status == "ok" else 503, postings_seen=0,
            checked_at=datetime.now(timezone.utc),
        )
    )
    db.flush()


def test_detect_disappeared_marks_posting_when_company_check_ok(db):
    company = _company(db, "panelco-7")
    data = {
        "company_id": company.id,
        "title": "QA Engineer",
        "location": "Remote",
        "description": "Selenium required",
        "source": "greenhouse",
        "source_id": "panel-7",
        "currency": "USD",
    }
    result = ingest_posting(db, data, skill_map={}, company_token="panelco-7")
    posting = db.get(Posting, result.posting_id)
    today = datetime.now(timezone.utc).date()

    # Simulate the posting being absent from today's fetch results.
    db.query(PostingSnapshot).filter_by(posting_id=posting.id, snapshot_date=today).delete()
    _seed_run_company(db, today=today, source="greenhouse", token="panelco-7", status="ok")

    count = _detect_disappeared(db, today)
    # _detect_disappeared only sets the attribute in-memory (the caller,
    # run_daily_rollup, commits) — flush before refresh() or the pending
    # change is discarded and the read-back looks like nothing happened.
    db.flush()
    db.refresh(posting)
    assert count == 1
    assert posting.disappeared_at is not None


def test_detect_disappeared_does_not_mark_posting_when_company_check_failed(db):
    company = _company(db, "panelco-8")
    data = {
        "company_id": company.id,
        "title": "Recruiter",
        "location": "Remote",
        "description": "ATS experience required",
        "source": "greenhouse",
        "source_id": "panel-8",
        "currency": "USD",
    }
    result = ingest_posting(db, data, skill_map={}, company_token="panelco-8")
    posting = db.get(Posting, result.posting_id)
    today = datetime.now(timezone.utc).date()

    db.query(PostingSnapshot).filter_by(posting_id=posting.id, snapshot_date=today).delete()
    _seed_run_company(db, today=today, source="greenhouse", token="panelco-8", status="http_error")

    count = _detect_disappeared(db, today)
    db.flush()
    db.refresh(posting)
    assert count == 0
    assert posting.disappeared_at is None


def test_detect_disappeared_ignores_adzuna(db):
    company = _company(db, "panelco-9")
    data = {
        "company_id": company.id,
        "title": "Growth Marketer",
        "location": "Remote",
        "description": "SEO required",
        "source": "adzuna",
        "source_id": "panel-9",
        "currency": "USD",
    }
    result = ingest_posting(db, data, skill_map={}, company_token=None)
    posting = db.get(Posting, result.posting_id)
    today = datetime.now(timezone.utc).date()

    db.query(PostingSnapshot).filter_by(posting_id=posting.id, snapshot_date=today).delete()

    count = _detect_disappeared(db, today)
    db.refresh(posting)
    assert count == 0
    assert posting.disappeared_at is None
