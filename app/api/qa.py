from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Literal, Optional
from sqlalchemy.orm import Session

from app.database import get_db
from app.config import settings

router = APIRouter()

# Module-level Redis singleton — initialised lazily on first request.
# Tests override _get_redis() via monkeypatch.
_redis_client = None


def _get_redis():
    global _redis_client
    if _redis_client is None:
        try:
            import redis
            client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
            client.ping()
            _redis_client = client
        except Exception:
            _redis_client = None
    return _redis_client


class QARequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=500)
    mode: Literal["fts", "vector", "hybrid"] = "hybrid"


class QAResponse(BaseModel):
    answer: Optional[str]
    sources: list[dict]
    cited_ids: list[int] = []
    cached: bool
    model: Optional[str] = None
    latency_ms: int


@router.post("/ask", response_model=QAResponse)
def ask(req: QARequest, db: Session = Depends(get_db)):
    from app.search.rag import answer_question

    result = answer_question(
        question=req.question,
        db=db,
        mode=req.mode,
        redis_client=_get_redis(),
        groq_api_key=settings.groq_api_key,
    )

    if result.get("error") and result.get("answer") is None:
        raise HTTPException(status_code=503, detail=result["error"])

    return result
