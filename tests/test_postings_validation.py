from fastapi import FastAPI
from fastapi.testclient import TestClient

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
