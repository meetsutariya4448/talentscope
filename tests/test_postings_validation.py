from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api import postings


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(postings.router, prefix="/postings")
    return TestClient(app)


def test_search_rejects_unbounded_text_filters_before_querying_database():
    with _client() as client:
        assert client.get("/postings/", params={"q": "q" * 501}).status_code == 422
        assert client.get("/postings/", params={"skill": "s" * 129}).status_code == 422
        assert client.get("/postings/", params={"location": "l" * 256}).status_code == 422


def test_fts_pagination_uses_a_stable_primary_key_tiebreaker():
    count_result = MagicMock()
    count_result.scalar.return_value = 0
    rows_result = MagicMock()
    rows_result.all.return_value = []
    db = MagicMock()
    db.execute.side_effect = [count_result, rows_result]

    postings._fts_results(db, "", "", "", page=2, page_size=20)

    statement = db.execute.call_args_list[1].args[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "postings.posted_at DESC NULLS LAST, postings.id DESC" in compiled
