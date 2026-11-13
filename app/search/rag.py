"""
RAG pipeline: hybrid retrieval → context assembly → Groq LLM → Redis-cached response.

No LangChain/LlamaIndex. Steps:
  1. Run hybrid (or fts/vector) search to fetch the N_SOURCES most-relevant postings.
  2. Format them into a numbered context block.
  3. Call Groq chat completions with a strict grounding prompt.
  4. Cache the result in Redis keyed on sha256(question|mode|N_SOURCES), TTL 1 hour.
"""

import hashlib
import json
import logging
import math
import time
from urllib.parse import urlsplit

from app.config import settings

logger = logging.getLogger(__name__)

# --- tuneable constants ---
N_SOURCES   = 8       # postings retrieved per question (each ~50 tokens)
CACHE_TTL   = 3600    # seconds; job market intel doesn't need sub-hour freshness
CACHE_PREFIX = "rag:v1:"
# Default lives in app/config.py (GROQ_MODEL env var). Kept as a module
# attribute so existing imports and tests still resolve it.
GROQ_MODEL  = settings.groq_model
MAX_TOKENS  = 400     # ~300 words; enough for a factual Q&A answer
MAX_CACHED_ANSWER_CHARS = 10_000
MAX_CACHED_PAYLOAD_CHARS = 65_536
MAX_CACHED_MODEL_CHARS = 200
MAX_CACHED_TITLE_CHARS = 512
MAX_CACHED_COMPANY_NAME_CHARS = 255
MAX_CACHED_LOCATION_CHARS = 255
MAX_CACHED_SOURCE_CHARS = 50
MAX_CACHED_URL_CHARS = 2048
MAX_CACHED_SALARY = 9_999_999_999.99
CACHED_SOURCE_FIELDS = {
    "id", "title", "company_name", "location", "salary_min", "salary_max",
    "source", "url",
}

SYSTEM_PROMPT = (
    "You are a job-market analyst with access to a set of real job postings. "
    "Answer the user's question using ONLY the postings provided as context below. "
    "Be specific: cite company names, titles, locations, and skill patterns you observe. "
    "If the context is insufficient to answer fully, say so rather than inventing details. "
    "Keep your answer under 200 words."
)

try:
    from groq import Groq as _Groq
except ImportError:
    _Groq = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_cited_ids(answer: str, postings: list[dict]) -> list[int]:
    """
    Extract [N] citation markers from the model's answer and map them to real
    posting IDs from the retrieved set.

    Any [N] where N is out of range (> len(postings) or < 1) is silently
    dropped — it has no corresponding retrieved document and cannot be
    validated.  The returned list preserves first-appearance order and is
    deduplicated.
    """
    import re
    seen: list[int] = []
    seen_set: set[int] = set()
    # Bound the marker width before converting to int. Untrusted model output
    # can otherwise exceed Python's integer-string conversion limit and abort
    # an otherwise valid Q&A response.
    for m in re.finditer(r'\[(\d{1,10})\]', answer):
        n = int(m.group(1))
        if 1 <= n <= len(postings):
            pid = postings[n - 1]["id"]
            if pid not in seen_set:
                seen.append(pid)
                seen_set.add(pid)
    return seen


def _cache_payload(
    answer: str, sources: list[dict], cited_ids: list[int], model: str | None
) -> dict:
    """Return only fields that are safe to persist and serve from Redis."""
    return {
        "answer": answer,
        "sources": sources,
        "cited_ids": cited_ids,
        "model": model,
    }


def _valid_cached_url(url) -> bool:
    """Accept absent URLs or the same public HTTP(S) shape ingestion stores."""
    if url is None or url == "":
        return True
    if (
        not isinstance(url, str)
        or len(url) > MAX_CACHED_URL_CHARS
        or not url.isprintable()
        or any(char.isspace() for char in url)
        or "\\" in url
    ):
        return False
    try:
        parsed = urlsplit(url)
        # Accessing ``port`` validates both its numeric form and range.
        _ = parsed.port
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def _valid_cached_payload(payload) -> bool:
    """Validate Redis data before returning it through the API contract."""
    if (
        not isinstance(payload, dict)
        or not payload.keys() <= {"answer", "sources", "cited_ids", "model"}
        or not isinstance(payload.get("answer"), str)
        or not payload["answer"].strip()
        or len(payload["answer"]) > MAX_CACHED_ANSWER_CHARS
        or not payload["answer"].isprintable()
    ):
        return False
    sources = payload.get("sources")
    if (
        not isinstance(sources, list)
        or len(sources) > N_SOURCES
        or not all(
            isinstance(source, dict)
            and source.keys() <= CACHED_SOURCE_FIELDS
            and isinstance(source.get("id"), int)
            and not isinstance(source.get("id"), bool)
            and source["id"] > 0
            and isinstance(source.get("title"), str)
            and bool(source["title"].strip())
            and len(source["title"]) <= MAX_CACHED_TITLE_CHARS
            and source["title"].isprintable()
            and (
                source.get("company_name") is None
                or (
                    isinstance(source["company_name"], str)
                    and bool(source["company_name"].strip())
                    and len(source["company_name"]) <= MAX_CACHED_COMPANY_NAME_CHARS
                    and source["company_name"].isprintable()
                )
            )
            and (
                source.get("location") is None
                or (
                    isinstance(source["location"], str)
                    and len(source["location"]) <= MAX_CACHED_LOCATION_CHARS
                    and (
                        not source["location"]
                        or source["location"].isprintable()
                    )
                )
            )
            and (
                source.get("source") is None
                or (
                    isinstance(source["source"], str)
                    and bool(source["source"].strip())
                    and len(source["source"]) <= MAX_CACHED_SOURCE_CHARS
                    and source["source"].isprintable()
                )
            )
            and _valid_cached_url(source.get("url"))
            for source in sources
        )
    ):
        return False
    source_ids = [source["id"] for source in sources]
    if len(source_ids) != len(set(source_ids)):
        return False
    for source in sources:
        salary_min = source.get("salary_min")
        salary_max = source.get("salary_max")
        for salary in (salary_min, salary_max):
            if salary is not None and (
                isinstance(salary, bool)
                or not isinstance(salary, (int, float))
                or not math.isfinite(salary)
                or not 0 <= salary <= MAX_CACHED_SALARY
            ):
                return False
        if (
            salary_min is not None
            and salary_max is not None
            and salary_min > salary_max
        ):
            return False
    if "cited_ids" in payload:
        cited_ids = payload["cited_ids"]
        if (
            not isinstance(cited_ids, list)
            or not all(isinstance(pid, int) and not isinstance(pid, bool) for pid in cited_ids)
            or cited_ids != _parse_cited_ids(payload["answer"], sources)
        ):
            return False
    model = payload.get("model")
    return model is None or (
        isinstance(model, str)
        and bool(model.strip())
        and len(model) <= MAX_CACHED_MODEL_CHARS
        and model.isprintable()
    )


def _cache_key(question: str, mode: str) -> str:
    # Model changes are behavior changes. Keeping the model out of the key
    # served answers generated by a retired model until their TTL expired,
    # even after an operator switched GROQ_MODEL and restarted the service.
    # Collapse insignificant whitespace so callers cannot bypass the cache
    # (and spend another budget unit) by varying spaces or line breaks.
    normalized_question = " ".join(question.split())
    digest = hashlib.sha256(
        f"{normalized_question}|{mode}|{N_SOURCES}|{GROQ_MODEL}".encode()
    ).hexdigest()
    return f"{CACHE_PREFIX}{digest}"


def _build_context(postings: list[dict]) -> str:
    if not postings:
        return "(no matching postings found in database)"
    lines = []
    for i, p in enumerate(postings, 1):
        parts = [
            f"[{i}] TITLE: {p['title']}",
            f"COMPANY: {p.get('company_name') or 'Unknown'}",
        ]
        if p.get("location"):
            parts.append(f"LOCATION: {p['location']}")
        if p.get("salary_min") is not None and p.get("salary_max") is not None:
            parts.append(f"SALARY: ${int(p['salary_min']):,}–${int(p['salary_max']):,}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _row_to_dict(row) -> dict:
    r = row._mapping
    return {
        "id":           r["id"],
        "title":        r["title"],
        "company_name": r.get("company_name"),
        "location":     r.get("location"),
        "salary_min":   float(r["salary_min"]) if r.get("salary_min") is not None else None,
        "salary_max":   float(r["salary_max"]) if r.get("salary_max") is not None else None,
        "source":       r["source"],
        "url":          r.get("url"),
    }


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def answer_question(
    question: str,
    db,
    mode: str = "hybrid",
    redis_client=None,
    groq_api_key: str = "",
    llm_gate=None,
) -> dict:
    """
    Return {answer, sources, cached, latency_ms} (and optionally model/error/degraded).

    - redis_client: a redis.Redis instance or None (cache is skipped when None).
    - groq_api_key: if empty, retrieval still runs but answer is None + error key set.
    - llm_gate: optional zero-arg callable consulted immediately before the
      billed provider call, returning an object with `.allowed` and `.outcome`.
      It is invoked *after* the cache lookup and *after* retrieval, and only on
      the path that actually spends money — so a cache hit never consumes
      budget, which it would if the check sat at the top of the request. When
      it declines, retrieval results are still returned, with `degraded` set to
      its outcome, rather than the request failing.
    """
    t0 = time.monotonic()

    # --- 1. Cache check ---
    cache_key = _cache_key(question, mode)
    if redis_client is not None:
        try:
            raw = redis_client.get(cache_key)
            if raw:
                if len(raw) > MAX_CACHED_PAYLOAD_CHARS:
                    logger.warning("Ignoring oversized Redis Q&A cache entry")
                else:
                    payload = json.loads(raw)
                    if not _valid_cached_payload(payload):
                        logger.warning("Ignoring malformed Redis Q&A cache entry")
                    else:
                        # cited_ids may be absent in entries written before this field was added
                        if "cited_ids" not in payload:
                            payload["cited_ids"] = _parse_cited_ids(
                                payload["answer"], payload["sources"]
                            )
                        payload["cached"] = True
                        payload["latency_ms"] = round((time.monotonic() - t0) * 1000)
                        return payload
        except Exception:
            logger.warning("Redis read failed; proceeding without cache")

    # --- 2. Retrieval ---
    from app.search.hybrid import (
        fts_search, vector_search, reciprocal_rank_fusion,
        fetch_postings_by_ids, TOP_K,
    )

    if mode == "hybrid":
        fts_ids = fts_search(db, question, limit=TOP_K)
        vec_ids = vector_search(db, question, limit=TOP_K)
        merged  = reciprocal_rank_fusion([fts_ids, vec_ids])
    elif mode == "vector":
        merged = vector_search(db, question, limit=TOP_K)
    else:
        merged = fts_search(db, question, limit=TOP_K)

    top_ids  = merged[:N_SOURCES]
    rows     = fetch_postings_by_ids(db, top_ids)
    postings = [_row_to_dict(r) for r in rows]

    # --- 3. Guard: key required ---
    if not groq_api_key:
        return {
            "answer":     None,
            "sources":    postings,
            "cached":     False,
            "error":      "GROQ_API_KEY not configured",
            "latency_ms": round((time.monotonic() - t0) * 1000),
        }

    if _Groq is None:
        return {
            "answer":     None,
            "sources":    postings,
            "cached":     False,
            "error":      "groq package not installed",
            "latency_ms": round((time.monotonic() - t0) * 1000),
        }

    # --- 4. Spend gate: the last point before this costs money ---
    if llm_gate is not None:
        decision = llm_gate()
        if not decision.allowed:
            return {
                "answer":     None,
                "sources":    postings,
                "cached":     False,
                "degraded":   decision.outcome,
                "latency_ms": round((time.monotonic() - t0) * 1000),
            }

    # --- 5. LLM call ---
    context  = _build_context(postings)
    user_msg = f"Context (job postings):\n{context}\n\nQuestion: {question}"

    try:
        client = _Groq(api_key=groq_api_key)
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=MAX_TOKENS,
        )
        content = completion.choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Q&A provider returned an empty answer")
        answer = content.strip()
    except Exception:
        # Authentication, rate-limit, timeout, and malformed provider
        # responses are upstream availability failures.  Return the same
        # structured error contract used by other Q&A guards so the API can
        # respond with 503 instead of leaking an uncaught 500.
        logger.exception("Groq answer generation failed")
        return {
            "answer": None,
            "sources": postings,
            "cached": False,
            "error": "Q&A provider unavailable",
            "latency_ms": round((time.monotonic() - t0) * 1000),
        }

    cited_ids  = _parse_cited_ids(answer, postings)

    payload = {
        "answer":     answer,
        "sources":    postings,
        "cited_ids":  cited_ids,
        "cached":     False,
        "model":      completion.model,
        "latency_ms": round((time.monotonic() - t0) * 1000),
    }

    # --- 5. Cache write ---
    if redis_client is not None:
        try:
            redis_client.setex(
                cache_key,
                CACHE_TTL,
                json.dumps(_cache_payload(answer, postings, cited_ids, completion.model)),
            )
        except Exception:
            logger.warning("Redis write failed; answer not cached")

    return payload
