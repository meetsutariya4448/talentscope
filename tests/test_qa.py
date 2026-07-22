"""
Phase 3 tests: RAG Q&A endpoint.

All Groq and Redis calls are mocked — no network required.
"""
import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Unit tests for app.search.rag
# ---------------------------------------------------------------------------

def test_answer_question_no_groq_key_returns_error(db):
    """Retrieval runs even without a key; answer is None and error key is set."""
    from app.search.rag import answer_question

    result = answer_question(
        "What backend jobs are open?",
        db=db,
        mode="fts",
        redis_client=None,
        groq_api_key="",
    )

    assert result["answer"] is None
    assert "GROQ_API_KEY" in result["error"]
    assert "sources" in result          # retrieval still returns a list (may be empty)
    assert "latency_ms" in result


def test_answer_question_groq_called(db):
    """Groq client is instantiated with the key and its answer is returned."""
    mock_msg = MagicMock()
    mock_msg.content = "There are several Python jobs available."
    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock(message=mock_msg)]
    mock_completion.model = "llama-3.1-8b-instant"

    with patch("app.search.rag._Groq") as MockGroq:
        MockGroq.return_value.chat.completions.create.return_value = mock_completion

        from app.search.rag import answer_question
        result = answer_question(
            "What Python jobs are open?",
            db=db,
            mode="fts",
            redis_client=None,
            groq_api_key="test-key-abc",
        )

    assert result["answer"] == "There are several Python jobs available."
    assert result["cached"] is False
    assert result["model"] == "llama-3.1-8b-instant"
    MockGroq.assert_called_once_with(api_key="test-key-abc")


def test_answer_question_cache_hit(db):
    """A Redis hit returns the cached payload without calling Groq."""
    cached_body = {"answer": "Cached market answer.", "sources": [], "model": "cached-model"}
    mock_redis = MagicMock()
    mock_redis.get.return_value = json.dumps(cached_body)

    with patch("app.search.rag._Groq") as MockGroq:
        from app.search.rag import answer_question
        result = answer_question(
            "What jobs exist?",
            db=db,
            mode="fts",
            redis_client=mock_redis,
            groq_api_key="test-key",
        )

    assert result["answer"] == "Cached market answer."
    assert result["cached"] is True
    MockGroq.assert_not_called()


def test_answer_question_cache_miss_then_write(db):
    """On a cache miss, the result is written to Redis with the configured TTL."""
    mock_msg = MagicMock()
    mock_msg.content = "Fresh answer."
    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock(message=mock_msg)]
    mock_completion.model = "llama-3.1-8b-instant"

    mock_redis = MagicMock()
    mock_redis.get.return_value = None   # cache miss

    with patch("app.search.rag._Groq") as MockGroq:
        MockGroq.return_value.chat.completions.create.return_value = mock_completion

        from app.search.rag import answer_question
        result = answer_question(
            "What are the top skills demanded?",
            db=db,
            mode="fts",
            redis_client=mock_redis,
            groq_api_key="test-key",
        )

    assert result["cached"] is False
    assert mock_redis.setex.called
    # TTL must match the module constant
    from app.search.rag import CACHE_TTL
    call_args = mock_redis.setex.call_args
    assert call_args[0][1] == CACHE_TTL


def test_answer_question_redis_unavailable_does_not_crash(db):
    """If Redis raises on get(), we fall through to Groq without crashing."""
    mock_msg = MagicMock()
    mock_msg.content = "Answer despite Redis failure."
    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock(message=mock_msg)]
    mock_completion.model = "llama-3.1-8b-instant"

    mock_redis = MagicMock()
    mock_redis.get.side_effect = ConnectionError("Redis down")

    with patch("app.search.rag._Groq") as MockGroq:
        MockGroq.return_value.chat.completions.create.return_value = mock_completion

        from app.search.rag import answer_question
        result = answer_question(
            "Any remote jobs?",
            db=db,
            mode="fts",
            redis_client=mock_redis,
            groq_api_key="test-key",
        )

    assert result["answer"] == "Answer despite Redis failure."


# ---------------------------------------------------------------------------
# API-level tests  (use TestClient from conftest)
# ---------------------------------------------------------------------------

def test_ask_endpoint_503_when_no_groq_key(client, monkeypatch):
    """Endpoint must return 503 when GROQ_API_KEY is not configured."""
    monkeypatch.setattr("app.api.qa._get_redis", lambda: None)

    with patch("app.search.rag.answer_question") as mock_fn:
        mock_fn.return_value = {
            "answer":     None,
            "sources":    [],
            "cached":     False,
            "error":      "GROQ_API_KEY not configured",
            "latency_ms": 1,
        }
        resp = client.post("/qa/ask", json={"question": "What Python jobs are there?"})

    assert resp.status_code == 503
    assert "GROQ_API_KEY" in resp.json()["detail"]


def test_ask_endpoint_returns_answer(client, monkeypatch):
    """Endpoint forwards the rag answer and includes required response fields."""
    monkeypatch.setattr("app.api.qa._get_redis", lambda: None)

    with patch("app.search.rag.answer_question") as mock_fn:
        mock_fn.return_value = {
            "answer":     "Several backend roles available at Stripe and Datadog.",
            "sources":    [{"id": 1, "title": "Backend Engineer", "company_name": "Stripe"}],
            "cached":     False,
            "model":      "llama-3.1-8b-instant",
            "latency_ms": 310,
        }
        resp = client.post("/qa/ask", json={"question": "What backend jobs are available?"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"] == "Several backend roles available at Stripe and Datadog."
    assert len(data["sources"]) == 1
    assert data["cached"] is False
    assert data["latency_ms"] == 310


def test_ask_endpoint_question_too_short(client):
    """Pydantic rejects questions shorter than 3 characters."""
    resp = client.post("/qa/ask", json={"question": "hi"})
    assert resp.status_code == 422


def test_ask_endpoint_invalid_mode(client):
    """Pydantic rejects unknown mode values."""
    resp = client.post("/qa/ask", json={"question": "What jobs are there?", "mode": "magic"})
    assert resp.status_code == 422
