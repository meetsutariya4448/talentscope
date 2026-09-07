from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from typing import Literal, Optional
from sqlalchemy.orm import Session

from app.api.guards import require_model_ready
from app.database import get_db
from app.config import settings
from app.observability import (
    QA_BUDGET_REMAINING,
    QA_LLM_CALLS_TOTAL,
    QA_REQUESTS_TOTAL,
)
from app.search import budget as qa_budget

router = APIRouter()

# Module-level Redis singleton — initialised lazily on first request.
# Tests override _get_redis() via monkeypatch.
_redis_client = None
REDIS_TIMEOUT_SECONDS = 2


def _get_redis():
    global _redis_client
    if _redis_client is None:
        try:
            import redis
            client = redis.Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
                socket_timeout=REDIS_TIMEOUT_SECONDS,
            )
            client.ping()
            _redis_client = client
        except Exception:
            _redis_client = None
    return _redis_client


def _client_id(request: Request) -> str:
    """Identify the caller for rate limiting.

    Prefers the left-most X-Forwarded-For entry, which is the original client
    when the app sits behind the nginx front proxy
    (observability/nginx/nginx.conf sets it). Without that, every request
    behind the proxy would arrive wearing the proxy's container IP and share a
    single bucket, so one caller could exhaust everyone's allowance.

    This header is client-controllable when the app is exposed directly, so it
    is a fairness control, not a security boundary — the daily budget, which no
    header can influence, is the actual spend bound.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


class QARequest(BaseModel):
    model_config = {"str_strip_whitespace": True}

    question: str = Field(..., min_length=3, max_length=500)
    mode: Literal["fts", "vector", "hybrid"] = "hybrid"


class QAResponse(BaseModel):
    answer: Optional[str]
    sources: list[dict]
    cited_ids: list[int] = []
    cached: bool
    model: Optional[str] = None
    latency_ms: int
    # Set when retrieval succeeded but no generated answer is being returned:
    # "llm_disabled", "budget_exhausted" or "counter_unavailable". The request
    # is a 200 in that case — sources are genuinely useful on their own, and a
    # public demo that 503s once a budget runs out looks broken rather than
    # bounded.
    degraded: Optional[str] = None


@router.post("/ask", response_model=QAResponse)
def ask(
    req: QARequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    from app.search.rag import answer_question

    # vector/hybrid retrieval runs the encoder; fts mode does not.
    if req.mode in ("vector", "hybrid"):
        require_model_ready()

    redis_client = _get_redis()

    # Rate limiting happens before any work: it is a burst control, and there
    # is no reason to run retrieval for a request that will not be served.
    rate = qa_budget.check_rate_limit(_client_id(request), redis_client=redis_client)
    if not rate.allowed:
        QA_REQUESTS_TOTAL.labels(outcome=qa_budget.Outcome.RATE_LIMITED).inc()
        raise HTTPException(
            status_code=429,
            detail="Too many Q&A requests; retry shortly.",
            headers={"Retry-After": str(rate.retry_after or 60)},
        )

    def llm_gate():
        """Consulted by answer_question immediately before the billed call —
        after the cache lookup, so a cache hit costs nothing."""
        if not settings.qa_llm_enabled:
            return qa_budget.Decision(False, qa_budget.Outcome.LLM_DISABLED)
        decision = qa_budget.consume_budget(redis_client=redis_client)
        if decision.allowed:
            # Incremented here rather than after the response, so a provider
            # call that then fails still shows up as spend — it was billed.
            QA_LLM_CALLS_TOTAL.inc()
        return decision

    result = answer_question(
        question=req.question,
        db=db,
        mode=req.mode,
        redis_client=redis_client,
        groq_api_key=settings.groq_api_key,
        llm_gate=llm_gate,
    )

    if result.get("error") and result.get("answer") is None:
        raise HTTPException(status_code=503, detail=result["error"])

    if result.get("degraded"):
        outcome = result["degraded"]
    elif result.get("cached"):
        outcome = qa_budget.Outcome.CACHED
    else:
        outcome = qa_budget.Outcome.ALLOWED
    QA_REQUESTS_TOTAL.labels(outcome=outcome).inc()

    remaining = qa_budget.budget_remaining(redis_client=redis_client)
    if remaining is not None:
        QA_BUDGET_REMAINING.set(remaining)
        response.headers["X-QA-Budget-Remaining"] = str(remaining)

    return result
