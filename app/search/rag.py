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
import time

logger = logging.getLogger(__name__)

# --- tuneable constants ---
N_SOURCES   = 8       # postings retrieved per question (each ~50 tokens)
CACHE_TTL   = 3600    # seconds; job market intel doesn't need sub-hour freshness
CACHE_PREFIX = "rag:v1:"
GROQ_MODEL  = "llama-3.1-8b-instant"
MAX_TOKENS  = 400     # ~300 words; enough for a factual Q&A answer

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


def _valid_cached_payload(payload) -> bool:
    """Validate Redis data before returning it through the API contract."""
    if not isinstance(payload, dict) or not isinstance(payload.get("answer"), str):
        return False
    sources = payload.get("sources")
    if not isinstance(sources, list) or not all(
        isinstance(source, dict)
        and isinstance(source.get("id"), int)
        and not isinstance(source.get("id"), bool)
        for source in sources
    ):
        return False
    cited_ids = payload.get("cited_ids")
    if cited_ids is not None and (
        not isinstance(cited_ids, list)
        or not all(isinstance(pid, int) and not isinstance(pid, bool) for pid in cited_ids)
    ):
        return False
    model = payload.get("model")
    return model is None or isinstance(model, str)


def _cache_key(question: str, mode: str) -> str:
    digest = hashlib.sha256(f"{question}|{mode}|{N_SOURCES}".encode()).hexdigest()
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
) -> dict:
    """
    Return {answer, sources, cached, latency_ms} (and optionally model/error).

    - redis_client: a redis.Redis instance or None (cache is skipped when None).
    - groq_api_key: if empty, retrieval still runs but answer is None + error key set.
    """
    t0 = time.monotonic()

    # --- 1. Cache check ---
    cache_key = _cache_key(question, mode)
    if redis_client is not None:
        try:
            raw = redis_client.get(cache_key)
            if raw:
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

    # --- 4. LLM call ---
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
        answer = completion.choices[0].message.content.strip()
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
                json.dumps({
                    "answer":    answer,
                    "sources":   postings,
                    "cited_ids": cited_ids,
                    "model":     completion.model,
                }),
            )
        except Exception:
            logger.warning("Redis write failed; answer not cached")

    return payload
