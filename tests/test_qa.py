"""
Phase 3 tests: RAG Q&A endpoint.

All Groq and Redis calls are mocked — no network required.
"""
import json
import sys
from unittest.mock import MagicMock, patch

import pytest


def test_qa_request_strips_whitespace_and_rejects_blank_questions():
    from pydantic import ValidationError

    from app.api.qa import QARequest

    assert QARequest(question="  What jobs are open?  ").question == "What jobs are open?"
    with pytest.raises(ValidationError):
        QARequest(question="   ")


def test_redis_client_uses_bounded_socket_timeouts(monkeypatch):
    """A Redis outage must not leave a Q&A request on library-default timeouts."""
    import app.api.qa as qa_api

    mock_client = MagicMock()
    mock_redis = MagicMock()
    mock_redis.Redis.from_url.return_value = mock_client
    monkeypatch.setitem(sys.modules, "redis", mock_redis)
    monkeypatch.setattr(qa_api, "_redis_client", None)

    assert qa_api._get_redis() is mock_client
    mock_redis.Redis.from_url.assert_called_once_with(
        qa_api.settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=qa_api.REDIS_TIMEOUT_SECONDS,
        socket_timeout=qa_api.REDIS_TIMEOUT_SECONDS,
    )
    mock_client.ping.assert_called_once_with()


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


def test_answer_question_provider_failure_returns_service_error(db):
    with patch("app.search.rag._Groq", side_effect=ConnectionError("provider timeout")):
        from app.search.rag import answer_question

        result = answer_question(
            "What Python jobs are open?",
            db=db,
            mode="fts",
            redis_client=None,
            groq_api_key="test-key-abc",
        )

    assert result["answer"] is None
    assert result["error"] == "Q&A provider unavailable"
    assert result["cached"] is False
    assert "sources" in result
    assert "latency_ms" in result


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


# ---------------------------------------------------------------------------
# Citation filtering — the guarantee
# ---------------------------------------------------------------------------

def test_citation_filtering_strips_out_of_range_reference(db):
    """
    If the model returns [9] or [10] when only 8 sources were retrieved, those
    markers must not appear in cited_ids — they have no corresponding posting.
    This is the test that proves the guarantee, independent of whether a real
    Groq call happens to come back clean.
    """
    # Model response cites [1] (valid) and [9], [10] (out of range for 8 sources)
    hallucinated_answer = (
        "Based on the postings, [1] looks relevant. "
        "Also see [9] and [10] for additional context."
    )
    mock_msg = MagicMock()
    mock_msg.content = hallucinated_answer
    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock(message=mock_msg)]
    mock_completion.model = "llama-3.1-8b-instant"

    with patch("app.search.rag._Groq") as MockGroq:
        MockGroq.return_value.chat.completions.create.return_value = mock_completion

        from app.search.rag import answer_question
        result = answer_question(
            "What backend jobs are open?",
            db=db,
            mode="fts",
            redis_client=None,
            groq_api_key="test-key",
        )

    cited = result["cited_ids"]
    # [9] and [10] are out of range — must not appear
    sources = result["sources"]
    valid_ids = {s["id"] for s in sources}
    assert all(cid in valid_ids for cid in cited), (
        f"cited_ids contains IDs not in retrieved set: {set(cited) - valid_ids}"
    )
    # The answer text still contains [9]/[10] as-is (we don't rewrite prose),
    # but cited_ids must only list the validated ones
    assert 9 not in cited
    assert 10 not in cited


def test_citation_filtering_valid_references_preserved(db):
    """Valid [N] markers within range all appear in cited_ids, deduplicated."""
    answer_with_repeats = "See [1] for Python roles. [2] is also strong. [1] appears again."
    mock_msg = MagicMock()
    mock_msg.content = answer_with_repeats
    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock(message=mock_msg)]
    mock_completion.model = "llama-3.1-8b-instant"

    with patch("app.search.rag._Groq") as MockGroq:
        MockGroq.return_value.chat.completions.create.return_value = mock_completion

        from app.search.rag import answer_question
        result = answer_question(
            "What Python jobs?",
            db=db,
            mode="fts",
            redis_client=None,
            groq_api_key="test-key",
        )

    cited = result["cited_ids"]
    sources = result["sources"]
    valid_ids = {s["id"] for s in sources}

    # All cited IDs must be in retrieved set
    assert all(cid in valid_ids for cid in cited)
    # No duplicates
    assert len(cited) == len(set(cited))


def test_parse_cited_ids_unit():
    """Unit test for _parse_cited_ids — no DB, no model."""
    from app.search.rag import _parse_cited_ids

    postings = [{"id": 101}, {"id": 202}, {"id": 303}]

    # Normal case: [1] → 101, [3] → 303
    assert _parse_cited_ids("See [1] and [3].", postings) == [101, 303]

    # Out-of-range [4] with only 3 sources — dropped
    assert _parse_cited_ids("See [4].", postings) == []

    # [0] is not a valid 1-indexed citation — dropped
    assert _parse_cited_ids("See [0].", postings) == []

    # Deduplication: [2] appears twice — only one entry
    result = _parse_cited_ids("[2] is great. Also [2].", postings)
    assert result == [202]

    # Mixed valid and invalid
    result = _parse_cited_ids("[1] ok [5] bad [2] ok", postings)
    assert result == [101, 202]

    # Empty answer
    assert _parse_cited_ids("", postings) == []

    # No citation markers
    assert _parse_cited_ids("No citations here.", postings) == []


def test_parse_cited_ids_ignores_oversized_markers():
    from app.search.rag import _parse_cited_ids

    postings = [{"id": 101}]
    oversized = "9" * 5000

    assert _parse_cited_ids(f"Ignore [{oversized}], keep [1].", postings) == [101]


def test_rag_context_preserves_zero_salary_bounds():
    from app.search.rag import _build_context, _row_to_dict

    row = MagicMock()
    row._mapping = {
        "id": 1,
        "title": "Volunteer Engineer",
        "company_name": "Community Org",
        "location": "Remote",
        "salary_min": 0,
        "salary_max": 0,
        "source": "greenhouse",
        "url": "https://example.com/job/1",
    }

    posting = _row_to_dict(row)

    assert posting["salary_min"] == 0.0
    assert posting["salary_max"] == 0.0
    assert "SALARY: $0–$0" in _build_context([posting])
