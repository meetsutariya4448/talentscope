"""
Proves at-least-once delivery (task_acks_late + task_reject_on_worker_lost,
see app/tasks/celery_app.py) doesn't corrupt data: re-running the same
ingest/embed work twice — simulating Celery redelivering a task whose ack
was lost, or an overlapping beat firing — must never create duplicate rows
and must leave the DB in the same state a single successful run would.
"""
from unittest.mock import MagicMock, patch

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.ingestion.ingest import ingest_posting
from app.models import Company, FailedTask, Posting, PostingDescriptionVersion, PostingSkill, Skill


def _company(db, slug: str) -> Company:
    company = Company(name=slug, slug=slug)
    db.add(company)
    db.flush()
    return company


# ---------------------------------------------------------------------------
# ingest_posting: duplicate delivery of an unchanged posting
# ---------------------------------------------------------------------------

def test_duplicate_delivery_does_not_create_duplicate_posting(db):
    company = _company(db, "idempo-1")
    data = {
        "company_id": company.id,
        "title": "Backend Engineer",
        "location": "Remote",
        "description": "Python and Postgres required",
        "source": "greenhouse",
        "source_id": "dup-1",
        "currency": "USD",
    }

    result1 = ingest_posting(db, data, skill_map={}, company_token="idempo-1")
    result2 = ingest_posting(db, dict(data), skill_map={}, company_token="idempo-1")

    assert result1.is_new is True
    assert result2.is_new is False
    assert result1.posting_id == result2.posting_id
    assert result2.content_changed is False

    postings = db.query(Posting).filter_by(source="greenhouse", source_id="dup-1").all()
    assert len(postings) == 1

    # Unchanged description → no spurious new version.
    versions = db.query(PostingDescriptionVersion).filter_by(posting_id=result1.posting_id).all()
    assert len(versions) == 1


def test_duplicate_delivery_does_not_duplicate_skill_links(db):
    company = _company(db, "idempo-2")
    skill = Skill(name="Python", category="language")
    db.add(skill)
    db.flush()
    skill_map = {"Python": skill.id}

    data = {
        "company_id": company.id,
        "title": "Python Engineer",
        "location": "Remote",
        "description": "Python required",
        "source": "greenhouse",
        "source_id": "dup-2",
        "currency": "USD",
    }

    result = ingest_posting(db, data, skill_map, company_token="idempo-2")
    ingest_posting(db, dict(data), skill_map, company_token="idempo-2")

    links = db.query(PostingSkill).filter_by(posting_id=result.posting_id).all()
    assert len(links) == 1


# ---------------------------------------------------------------------------
# Content drift: fixes the root-cause bug where postings.description/title
# were never refreshed on re-fetch, so search_vector (and embeddings) stayed
# stale forever even after being made a GENERATED column.
# ---------------------------------------------------------------------------

def test_content_change_is_detected_and_flagged_for_reembedding(db):
    company = _company(db, "idempo-3")
    data = {
        "company_id": company.id,
        "title": "Data Engineer",
        "location": "Remote",
        "description": "Original description",
        "source": "greenhouse",
        "source_id": "dup-3",
        "currency": "USD",
    }
    first = ingest_posting(db, data, {}, company_token="idempo-3")
    assert first.content_changed is False  # new posting, not an "update"

    unchanged = ingest_posting(db, dict(data), {}, company_token="idempo-3")
    assert unchanged.content_changed is False

    updated = dict(data)
    updated["description"] = "Completely different description"
    changed = ingest_posting(db, updated, {}, company_token="idempo-3")
    assert changed.content_changed is True

    posting = db.get(Posting, first.posting_id)
    assert posting.description == "Completely different description"


def test_search_vector_recomputes_on_description_change(db):
    """Regression for the staleness bug fixed by migration 0005 (GENERATED
    search_vector) + the content-refresh fix in ingest_posting: a posting's
    FTS text must reflect the current description, not just the one it was
    first inserted with."""
    company = _company(db, "idempo-4")
    data = {
        "company_id": company.id,
        "title": "Platform Engineer",
        "location": "Remote",
        "description": "Original unique keyword zzzqux",
        "source": "greenhouse",
        "source_id": "dup-4",
        "currency": "USD",
    }
    result = ingest_posting(db, data, {}, company_token="idempo-4")

    updated = dict(data)
    updated["description"] = "Completely different unique keyword abcyyy"
    ingest_posting(db, updated, {}, company_token="idempo-4")

    matches_new = db.execute(
        text("SELECT search_vector @@ plainto_tsquery('english', :q) FROM postings WHERE id = :id"),
        {"q": "abcyyy", "id": result.posting_id},
    ).scalar_one()
    matches_old = db.execute(
        text("SELECT search_vector @@ plainto_tsquery('english', :q) FROM postings WHERE id = :id"),
        {"q": "zzzqux", "id": result.posting_id},
    ).scalar_one()
    assert matches_new is True
    assert matches_old is False


# ---------------------------------------------------------------------------
# embed_missing_postings: overlapping backfill firings must not double-dispatch
# ---------------------------------------------------------------------------

def test_embed_missing_postings_skips_ids_already_in_flight(db):
    company = _company(db, "idempo-5")
    posting = Posting(
        company_id=company.id, title="SRE", location="Remote",
        description="Kubernetes", source="greenhouse", source_id="dup-5",
    )
    db.add(posting)
    db.commit()  # separate session below needs this committed to see it

    import app.tasks.embedding as embedding_mod

    # Simulate: first beat firing already claimed this id (in Redis) and its
    # embed_posting task is still running when the second firing happens.
    claimed = {}

    def fake_set(key, value, nx=False, ex=None):
        if nx and key in claimed:
            return None
        claimed[key] = value
        return True

    mock_redis = MagicMock()
    mock_redis.set.side_effect = fake_set

    # embed_missing_postings closes its session when done (correct production
    # behavior) — give it its own session factory bound to the same engine
    # rather than the fixture's `db` directly, so closing it doesn't affect
    # `db`. See test_monitoring.py::_session_factory for the same pattern.
    session_factory = sessionmaker(bind=db.get_bind())

    with patch.object(embedding_mod, "SessionLocal", session_factory), \
         patch.object(embedding_mod, "_get_redis", return_value=mock_redis), \
         patch.object(embedding_mod, "embed_posting") as mock_embed_posting:
        embedding_mod.embed_missing_postings()
        embedding_mod.embed_missing_postings()

    # Assert on *our* posting specifically rather than the raw dispatched
    # count — the underlying query is a global scan over every
    # embedding-IS-NULL posting, so the count is sensitive to whatever else
    # exists in the shared test DB. The property under test is narrower and
    # DB-state-independent: this id was dispatched exactly once across two
    # overlapping firings, not zero and not twice.
    dispatched_ids = [call.args[0] for call in mock_embed_posting.delay.call_args_list]
    assert dispatched_ids.count(posting.id) == 1


def test_embed_posting_clears_pending_marker_on_completion(db):
    import app.tasks.embedding as embedding_mod

    mock_redis = MagicMock()
    with patch.object(embedding_mod, "_get_redis", return_value=mock_redis):
        embedding_mod._clear_pending(123)

    mock_redis.delete.assert_called_once_with(f"{embedding_mod.PENDING_NS}:123")


# ---------------------------------------------------------------------------
# Dead-letter store: a task that exhausts retries lands exactly once, not
# lost, not duplicated.
# ---------------------------------------------------------------------------

def test_task_failure_writes_exactly_one_dead_letter_row(db):
    import app.tasks.monitoring as monitoring_mod

    mock_sender = MagicMock()
    mock_sender.name = "app.tasks.greenhouse.fetch_greenhouse"
    mock_sender.request.retries = 3

    with patch.object(monitoring_mod, "SessionLocal", sessionmaker(bind=db.get_bind())):
        monitoring_mod._on_task_failure(
            sender=mock_sender,
            task_id="task-abc-123",
            exception=RuntimeError("board token 404"),
            traceback="Traceback (most recent call last): ...",
            args=("bad-token", 1),
            kwargs={},
        )

    rows = db.query(FailedTask).filter_by(task_id="task-abc-123").all()
    assert len(rows) == 1
    assert rows[0].task_name == "app.tasks.greenhouse.fetch_greenhouse"
    assert rows[0].retries == 3
    assert "404" in rows[0].exception
