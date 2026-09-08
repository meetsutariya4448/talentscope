"""Spend controls on the paid-LLM path (app/search/budget.py, app/api/qa.py).

The behaviour under test exists because POST /qa/ask was public,
unauthenticated and CORS-*, and every uncached request became a billed Groq
call. Its only brake was a Redis response cache that disabled itself whenever
Redis was unavailable.
"""
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.search import budget as qa_budget


class FakeRedis:
    """Minimal INCR/EXPIRE/GET Redis stand-in with a controllable failure mode."""

    def __init__(self, fail=False):
        self.store = {}
        self.expires = {}
        self.fail = fail

    def incr(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def expire(self, key, ttl):
        if self.fail:
            raise ConnectionError("redis down")
        self.expires[key] = ttl

    def get(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        v = self.store.get(key)
        return str(v) if v is not None else None


# ---------------------------------------------------------------------------
# Daily budget
# ---------------------------------------------------------------------------

def test_budget_allows_up_to_the_limit_then_refuses(monkeypatch):
    monkeypatch.setattr(settings, "qa_daily_budget", 3)
    rc = FakeRedis()

    outcomes = [qa_budget.consume_budget(redis_client=rc).allowed for _ in range(5)]

    assert outcomes == [True, True, True, False, False]


def test_budget_key_is_given_an_expiry_on_first_use(monkeypatch):
    """Without a TTL the daily counters accumulate in Redis forever."""
    monkeypatch.setattr(settings, "qa_daily_budget", 10)
    rc = FakeRedis()

    qa_budget.consume_budget(redis_client=rc)
    qa_budget.consume_budget(redis_client=rc)

    assert len(rc.expires) == 1
    assert list(rc.expires.values())[0] == qa_budget.BUDGET_TTL_SECONDS


def test_zero_budget_refuses_every_call(monkeypatch):
    """0 is a usable setting: retrieval stays up, generation is off for the day."""
    monkeypatch.setattr(settings, "qa_daily_budget", 0)

    decision = qa_budget.consume_budget(redis_client=FakeRedis())

    assert decision.allowed is False
    assert decision.outcome == qa_budget.Outcome.BUDGET_EXHAUSTED


def test_budget_fails_closed_when_redis_is_unavailable(monkeypatch):
    """The exact hole this module exists to close.

    The Redis response cache was the only spend brake, and it silently stopped
    working when Redis went away — so a Redis outage removed the one thing
    limiting spend. Refusing the call is the correct direction to fail.
    """
    monkeypatch.setattr(settings, "qa_daily_budget", 100)
    monkeypatch.setattr(settings, "qa_require_budget_counter", True)

    decision = qa_budget.consume_budget(redis_client=None)

    assert decision.allowed is False
    assert decision.outcome == qa_budget.Outcome.COUNTER_UNAVAILABLE


def test_budget_can_be_configured_to_fail_open(monkeypatch):
    monkeypatch.setattr(settings, "qa_daily_budget", 100)
    monkeypatch.setattr(settings, "qa_require_budget_counter", False)

    assert qa_budget.consume_budget(redis_client=None).allowed is True


def test_budget_counts_before_comparing(monkeypatch):
    """Increment-then-compare, so two concurrent requests cannot both see the
    last unit as available and both spend it."""
    monkeypatch.setattr(settings, "qa_daily_budget", 1)
    rc = FakeRedis()

    first = qa_budget.consume_budget(redis_client=rc)
    second = qa_budget.consume_budget(redis_client=rc)

    assert (first.allowed, second.allowed) == (True, False)
    assert rc.store[qa_budget._budget_key(__import__("datetime").datetime.now(
        __import__("datetime").timezone.utc))] == 2


@pytest.mark.parametrize("stored", ["not-a-number", "", -1])
def test_budget_remaining_rejects_malformed_counters(monkeypatch, stored):
    monkeypatch.setattr(settings, "qa_daily_budget", 10)
    rc = FakeRedis()
    rc.store[qa_budget._budget_key(__import__("datetime").datetime.now(
        __import__("datetime").timezone.utc))] = stored

    assert qa_budget.budget_remaining(redis_client=rc) is None


# ---------------------------------------------------------------------------
# Per-client rate limit
# ---------------------------------------------------------------------------

def test_rate_limit_refuses_a_burst_from_one_client(monkeypatch):
    monkeypatch.setattr(settings, "qa_rate_limit_per_min", 2)
    rc = FakeRedis()

    allowed = [qa_budget.check_rate_limit("1.2.3.4", redis_client=rc).allowed for _ in range(4)]

    assert allowed == [True, True, False, False]


def test_rate_limit_is_per_client(monkeypatch):
    monkeypatch.setattr(settings, "qa_rate_limit_per_min", 1)
    rc = FakeRedis()

    assert qa_budget.check_rate_limit("1.1.1.1", redis_client=rc).allowed is True
    assert qa_budget.check_rate_limit("2.2.2.2", redis_client=rc).allowed is True
    assert qa_budget.check_rate_limit("1.1.1.1", redis_client=rc).allowed is False


def test_rate_limit_supplies_a_retry_after(monkeypatch):
    monkeypatch.setattr(settings, "qa_rate_limit_per_min", 1)
    rc = FakeRedis()
    qa_budget.check_rate_limit("9.9.9.9", redis_client=rc)

    decision = qa_budget.check_rate_limit("9.9.9.9", redis_client=rc)

    assert decision.allowed is False
    assert 1 <= decision.retry_after <= qa_budget.RATE_WINDOW_SECONDS


def test_rate_limit_fails_open_when_redis_is_down(monkeypatch):
    """Unlike the budget, the rate limiter is a fairness control — the daily
    budget is the hard spend bound, so being unable to count here must not
    take the endpoint down."""
    monkeypatch.setattr(settings, "qa_rate_limit_per_min", 1)

    assert qa_budget.check_rate_limit("1.2.3.4", redis_client=FakeRedis(fail=True)).allowed is True


# ---------------------------------------------------------------------------
# The invariant that matters most: cache hits are free
# ---------------------------------------------------------------------------

def test_cache_hit_does_not_consume_budget(db, monkeypatch):
    """A popular question must not burn the day's allowance.

    The gate is passed into answer_question and consulted only on the path that
    actually calls the provider, which is after the cache lookup — so this is
    really a test that the gate sits in the right place.
    """
    import json as _json

    from app.search.rag import _cache_key, answer_question

    calls = []

    def gate():
        calls.append(1)
        return qa_budget.Decision(True, qa_budget.Outcome.ALLOWED)

    cached_payload = {
        "answer": "Cached answer [1]",
        "sources": [{"id": 1, "title": "Engineer"}],
        "cited_ids": [1],
        "cached": False,
        "latency_ms": 5,
    }
    redis_client = MagicMock()
    redis_client.get.return_value = _json.dumps(cached_payload)

    result = answer_question(
        "which companies hire rust engineers",
        db=db,
        mode="fts",
        redis_client=redis_client,
        groq_api_key="fake-key",
        llm_gate=gate,
    )

    assert result["cached"] is True
    assert calls == [], "budget gate ran on a cache hit — cached answers must be free"


def test_gate_refusal_degrades_instead_of_failing(db):
    """Retrieval results are still returned; the request is not an error."""
    from app.search.rag import answer_question

    result = answer_question(
        "which companies hire rust engineers",
        db=db,
        mode="fts",
        redis_client=None,
        groq_api_key="fake-key",
        llm_gate=lambda: qa_budget.Decision(False, qa_budget.Outcome.BUDGET_EXHAUSTED),
    )

    assert result["answer"] is None
    assert result["degraded"] == qa_budget.Outcome.BUDGET_EXHAUSTED
    assert "error" not in result
    assert isinstance(result["sources"], list)


# ---------------------------------------------------------------------------
# Endpoint behaviour
# ---------------------------------------------------------------------------

def test_ask_returns_429_once_the_client_rate_limit_is_hit(client, monkeypatch):
    monkeypatch.setattr(settings, "qa_rate_limit_per_min", 1)
    # Generation off so the allowed request cannot reach the real provider.
    monkeypatch.setattr(settings, "qa_llm_enabled", False)
    # One shared instance: `lambda: FakeRedis()` would hand every request its
    # own empty counter and the limit would never be reached.
    shared = FakeRedis()
    monkeypatch.setattr("app.api.qa._get_redis", lambda: shared)

    body = {"question": "which companies hire rust engineers", "mode": "fts"}
    first = client.post("/qa/ask", json=body)
    second = client.post("/qa/ask", json=body)

    assert second.status_code == 429
    assert "Retry-After" in second.headers
    assert first.status_code != 429


def test_ask_degrades_to_sources_with_http_200_when_budget_is_spent(client, monkeypatch):
    """A public demo that starts 503ing once a budget runs out looks broken.
    Bounded is not the same as broken, and the status code should say so."""
    monkeypatch.setattr(settings, "qa_daily_budget", 0)
    monkeypatch.setattr(settings, "qa_rate_limit_per_min", 0)
    monkeypatch.setattr(settings, "groq_api_key", "fake-key")
    shared = FakeRedis()
    monkeypatch.setattr("app.api.qa._get_redis", lambda: shared)

    resp = client.post("/qa/ask", json={"question": "rust engineers", "mode": "fts"})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["answer"] is None
    assert payload["degraded"] == qa_budget.Outcome.BUDGET_EXHAUSTED
    assert isinstance(payload["sources"], list)


def test_ask_reports_llm_disabled_without_calling_the_provider(client, monkeypatch):
    monkeypatch.setattr(settings, "qa_llm_enabled", False)
    monkeypatch.setattr(settings, "qa_rate_limit_per_min", 0)
    monkeypatch.setattr(settings, "groq_api_key", "fake-key")
    shared = FakeRedis()
    monkeypatch.setattr("app.api.qa._get_redis", lambda: shared)

    resp = client.post("/qa/ask", json={"question": "rust engineers", "mode": "fts"})

    assert resp.status_code == 200
    assert resp.json()["degraded"] == qa_budget.Outcome.LLM_DISABLED


def test_client_id_prefers_forwarded_for(monkeypatch):
    """Behind the nginx front proxy every request would otherwise share one
    bucket under the proxy's container IP."""
    from app.api.qa import _client_id

    request = MagicMock()
    request.headers = {"x-forwarded-for": "203.0.113.9, 10.0.0.1"}
    request.client.host = "172.18.0.5"

    assert _client_id(request) == "203.0.113.9"


def test_client_id_falls_back_to_peer_address():
    from app.api.qa import _client_id

    request = MagicMock()
    request.headers = {}
    request.client.host = "198.51.100.7"

    assert _client_id(request) == "198.51.100.7"
